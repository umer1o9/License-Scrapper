from __future__ import annotations

import json
import os
import queue
import re
import shutil
import socket
import sqlite3
import sys
import threading
import time
import webbrowser
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse
from xml.sax.saxutils import escape

ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
if sys.platform == "darwin":
    USER_DATA = Path.home() / "Library" / "Application Support" / "Growth License Scrapper"
elif os.name == "nt":
    USER_DATA = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Growth License Scrapper"
else:
    USER_DATA = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "growth-license-scrapper"
DATA = Path(os.environ.get("LICENSE_SCRAPPER_DATA", USER_DATA))
EXPORTS = DATA / "exports"
DB = DATA / "scraper.sqlite3"
PORT = int(os.environ.get("PORT", "8765"))
STATIC = ROOT / "static"
LEGACY_DATA = Path(__file__).resolve().parent / "data"
FIELDS = ["license_status", "license_number", "english_name", "mobile_number", "mailbox_1", "mailbox_2", "manager_names_english"]
UAE_PASS_PHONE = os.environ.get("UAE_PASS_PHONE", "")


def next_license_number(value):
    """Increment the variable part of a license number, keeping its format.

    Supported shapes (auto-detected):
      DMCC-064581    -> DMCC-064582     (text prefix, digits keep their width)
      CN_3645562     -> CN_3645563      (text prefix)
      2202345.01     -> 2202346.01      (digits before a dot suffix)
      2427975        -> 2427976         (plain number)
      2627017048888  -> 2627017058888   (26 + 7 middle digits + 8888)
    """
    s = str(value).strip()
    if not s:
        raise ValueError("license number is empty")
    # Digits before a dot suffix, e.g. 2202345.01 -> 2202346.01
    match = re.match(r"^(.*?)(\d+)(\.[^.]*)$", s)
    if match:
        prefix, digits, suffix = match.groups()
        return f"{prefix}{str(int(digits)+1).zfill(len(digits))}{suffix}"
    # Plain number
    if s.isdigit():
        if len(s) == 13:
            # Series such as 2627017048888: first 2 and last 4 stay fixed.
            prefix, middle, suffix = s[:2], s[2:-4], s[-4:]
            return f"{prefix}{str(int(middle)+1).zfill(len(middle))}{suffix}"
        return str(int(s) + 1)
    # Text prefix followed by digits, e.g. DMCC-064581 or CN_3645562
    match = re.match(r"^(.*?)(\d+)$", s)
    if match:
        prefix, digits = match.groups()
        return f"{prefix}{str(int(digits)+1).zfill(len(digits))}"
    raise ValueError(f"cannot find a number to increment in {s!r}")


def advance_license_number(value, steps):
    """Apply next_license_number() the given number of times."""
    out = str(value)
    for _ in range(max(0, int(steps or 0))):
        out = next_license_number(out)
    return out



def chrome_devtools_active_port_files():
    """Return likely Chrome profile DevToolsActivePort files for this OS."""
    override = os.environ.get("CHROME_USER_DATA_DIR")
    if override:
        return [Path(override).expanduser() / "DevToolsActivePort"]

    home = Path.home()
    if sys.platform == "darwin":
        roots = [home / "Library" / "Application Support" / "Google" / "Chrome"]
        roots += [home / "Library" / "Application Support" / "Google" / f"Chrome {channel}"
                  for channel in ("Beta", "Dev", "Canary")]
    elif os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        roots = [local / "Google" / "Chrome" / "User Data"]
        roots += [local / "Google" / f"Chrome {channel}" / "User Data"
                  for channel in ("Beta", "Dev", "SxS")]
    else:
        roots = [home / ".config" / "google-chrome", home / ".config" / "chromium"]
        roots += [home / ".config" / f"google-chrome-{channel}"
                  for channel in ("beta", "dev", "unstable")]
    return [root / "DevToolsActivePort" for root in roots]


def chrome_debugging_websocket_url():
    """Read Chrome's permission-based local debugging WebSocket endpoint.

    Chrome's chrome://inspect/#remote-debugging bridge intentionally does not
    expose the legacy /json/version HTTP discovery endpoint. Instead, it writes
    the port and browser WebSocket path into DevToolsActivePort.
    """
    for active_port in chrome_devtools_active_port_files():
        try:
            lines = active_port.read_text(encoding="utf-8").splitlines()
            port = int(lines[0].strip())
            path = lines[1].strip()
            if not (1 <= port <= 65535) or not path.startswith("/devtools/browser/"):
                continue
            return f"ws://127.0.0.1:{port}{path}"
        except (OSError, ValueError, IndexError):
            continue
    return None


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def db():
    DATA.mkdir(parents=True, exist_ok=True)
    legacy_db = LEGACY_DATA / "scraper.sqlite3"
    if not DB.exists() and legacy_db.exists() and legacy_db.resolve() != DB.resolve():
        source = sqlite3.connect(legacy_db)
        target = sqlite3.connect(DB)
        source.backup(target)
        source.close()
        target.close()
        legacy_exports = LEGACY_DATA / "exports"
        if legacy_exports.exists():
            shutil.copytree(legacy_exports, EXPORTS, dirs_exist_ok=True)
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript("""
    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, ended_at TEXT, status TEXT NOT NULL, start_number TEXT NOT NULL, max_records INTEGER NOT NULL, interval_seconds REAL NOT NULL, searched INTEGER DEFAULT 0, found INTEGER DEFAULT 0, error TEXT, export_path TEXT);
    CREATE TABLE IF NOT EXISTS records (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL, license_number TEXT NOT NULL, searched_at TEXT NOT NULL, data_json TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS searches (license_number TEXT PRIMARY KEY, searched_at TEXT NOT NULL, run_id INTEGER NOT NULL, outcome TEXT NOT NULL, error TEXT);
    CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, created_at TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL);
    """)
    # One search can legitimately return several businesses with the same
    # displayed license number. Older databases had UNIQUE(license_number,
    # run_id), which silently discarded all but the first result row.
    unique_indexes = c.execute("PRAGMA index_list(records)").fetchall()
    if any(index[2] and any(col[2] in ("license_number", "run_id") for col in c.execute(f"PRAGMA index_info('{index[1]}')").fetchall()) for index in unique_indexes):
        c.executescript("""
        CREATE TABLE records_migrated (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL, license_number TEXT NOT NULL, searched_at TEXT NOT NULL, data_json TEXT NOT NULL);
        INSERT INTO records_migrated(id,run_id,license_number,searched_at,data_json) SELECT id,run_id,license_number,searched_at,data_json FROM records;
        DROP TABLE records;
        ALTER TABLE records_migrated RENAME TO records;
        """)
    # Older databases used INTEGER PRIMARY KEY for searches.license_number.
    # A rowid alias cannot store formatted numbers (DMCC-064581), so rebuild
    # the table with a TEXT key.
    search_cols = c.execute("PRAGMA table_info(searches)").fetchall()
    if any(row[1] == "license_number" and (row[2] or "").upper() == "INTEGER" for row in search_cols):
        c.executescript("""
        CREATE TABLE searches_migrated (license_number TEXT PRIMARY KEY, searched_at TEXT NOT NULL, run_id INTEGER NOT NULL, outcome TEXT NOT NULL, error TEXT);
        INSERT INTO searches_migrated(license_number,searched_at,run_id,outcome,error)
            SELECT CAST(license_number AS TEXT),searched_at,run_id,outcome,error FROM searches;
        DROP TABLE searches;
        ALTER TABLE searches_migrated RENAME TO searches;
        """)
    for k, v in {"initial_number": "10000", "max_records": "100", "interval_seconds": "2"}.items():
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)", (k,v))
    c.commit()
    return c


def log(c, run_id, level, msg):
    c.execute("INSERT INTO logs(run_id,created_at,level,message) VALUES (?,?,?,?)", (run_id, now(), level, msg))
    c.commit()


def xlsx_bytes(headers, rows):
    def col(n):
        s = ""
        while n:
            n, r = divmod(n-1, 26); s = chr(65+r)+s
        return s
    allrows = [headers] + rows
    cells = []
    for ri, row in enumerate(allrows, 1):
        cc = []
        for ci, value in enumerate(row, 1):
            ref = f"{col(ci)}{ri}"
            if isinstance(value, (int,float)):
                cc.append(f'<c r="{ref}"><v>{value}</v></c>')
            else:
                val = escape(str(value or ""))
                cc.append(f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{val}</t></is></c>')
        cells.append(f'<row r="{ri}">{"".join(cc)}</row>')
    sheet = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'+"".join(cells)+"</sheetData></worksheet>"
    files = {
      "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
      "_rels/.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
      "xl/workbook.xml": '<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Licenses" sheetId="1" r:id="rId1"/></sheets></workbook>',
      "xl/_rels/workbook.xml.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
      "xl/worksheets/sheet1.xml": sheet,
    }
    import io
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,"w",zipfile.ZIP_DEFLATED) as z:
        for n,v in files.items(): z.writestr(n,v)
    return buf.getvalue()


class PortalAuthRequired(RuntimeError):
    """The portal lost its signed-in session; the caller must re-authenticate."""


class PortalSearchError(RuntimeError):
    """The portal never answered a search request after retries."""


class Runner:
    def __init__(self):
        self.lock = threading.RLock()
        self.thread = None
        self.run_queue = queue.Queue()
        self.pause_event = threading.Event()
        self.pause_event.set()
        self.stop_event = threading.Event()
        self.human_event = threading.Event()
        self.active_run = None
        self.browser = None
        self.page = None

    def state(self):
        c=db()
        latest=c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        last_row=c.execute("SELECT license_number FROM searches ORDER BY searched_at DESC, rowid DESC LIMIT 1").fetchone()
        last=last_row["license_number"] if last_row else None
        settings={r["key"]:r["value"] for r in c.execute("SELECT * FROM settings")}
        if latest:
            pending=latest["start_number"]
            if int(latest["searched"] or 0)>0 and last is not None:
                pending=next_license_number(last)
            first_error=c.execute(
                "SELECT license_number FROM searches WHERE run_id=? AND outcome='error' ORDER BY searched_at, rowid LIMIT 1",
                (latest["id"],),
            ).fetchone()
            if first_error is not None:
                pending=first_error["license_number"]
        else:
            pending=next_license_number(last) if last is not None else settings["initial_number"]
        count=c.execute("SELECT COUNT(*) FROM searches WHERE substr(searched_at,1,10)=?",(datetime.now().astimezone().date().isoformat(),)).fetchone()[0]
        logs=[dict(r) for r in c.execute("SELECT created_at,level,message FROM logs ORDER BY id DESC LIMIT 30")]
        runs=[dict(r) for r in c.execute("SELECT id,started_at,ended_at,status,start_number,max_records,interval_seconds,searched,found,error,export_path FROM runs ORDER BY id DESC LIMIT 20")]
        c.close()
        return {"running": self.active_run is not None,"stopping":self.stop_event.is_set() and self.active_run is not None,"paused":not self.pause_event.is_set(),"waiting_for_human":not self.human_event.is_set() and bool(self.active_run),"next_number":pending,"searched_today":count,"latest_run":dict(latest) if latest else None,"settings":settings,"logs":logs,"runs":runs}

    def start(self, cfg):
        with self.lock:
            if self.active_run is not None: raise ValueError("A run is already active")
            self._require_chrome_remote_debugging()
            c=db(); st={r["key"]:r["value"] for r in c.execute("SELECT * FROM settings")}
            last_row=c.execute("SELECT license_number FROM searches ORDER BY searched_at DESC, rowid DESC LIMIT 1").fetchone()
            last=last_row[0] if last_row else None
            latest=c.execute("SELECT start_number,searched FROM runs ORDER BY id DESC LIMIT 1").fetchone()
            if latest:
                automatic=latest["start_number"]
                if int(latest["searched"] or 0)>0 and last is not None:
                    automatic=next_license_number(last)
                first_error=c.execute(
                    "SELECT license_number FROM searches WHERE run_id=(SELECT MAX(id) FROM runs) AND outcome='error' ORDER BY searched_at, rowid LIMIT 1"
                ).fetchone()
                if first_error is not None:
                    automatic=first_error[0]
            else:
                automatic=next_license_number(last) if last is not None else st["initial_number"]
            start=str(cfg["start_number"]).strip() if cfg.get("start_number") not in (None,"") else automatic
            try: next_license_number(start)
            except ValueError: raise ValueError("Starting license number must contain a number to increment")
            maxn=int(cfg.get("max_records",st["max_records"])); interval=float(cfg.get("interval_seconds",st["interval_seconds"]))
            if maxn < 1 or maxn > 100000: raise ValueError("Maximum records must be between 1 and 100000")
            if interval < 0.5 or interval > 3600: raise ValueError("Interval must be between 0.5 and 3600 seconds")
            c.execute("UPDATE settings SET value=? WHERE key='max_records'",(str(maxn),)); c.execute("UPDATE settings SET value=? WHERE key='interval_seconds'",(str(interval),))
            cur=c.execute("INSERT INTO runs(started_at,status,start_number,max_records,interval_seconds) VALUES (?, 'starting',?,?,?)",(now(),start,maxn,interval)); rid=cur.lastrowid; log(c,rid,"info",f"Run started at license number {start}"); c.close()
            self.stop_event.clear(); self.pause_event.set(); self.human_event.clear(); self.active_run=rid
            if not self.thread or not self.thread.is_alive():
                self.thread=threading.Thread(target=self._worker_loop,daemon=True); self.thread.start()
            self.run_queue.put((rid,start,maxn,interval))

    def _require_chrome_remote_debugging(self):
        endpoint = chrome_debugging_websocket_url()
        if endpoint:
            parsed = urlparse(endpoint)
            try:
                with socket.create_connection((parsed.hostname, parsed.port), timeout=1.5):
                    return
            except OSError:
                pass
        raise ValueError(
            "Chrome's approved debugging connection was not found, so no run was "
            "started. The in-browser Chrome setting does not provide /json/version; "
            "the app looks for the DevToolsActivePort file in the default Chrome "
            "profile. In the same Chrome profile, enable chrome://inspect/#remote-debugging "
            "and approve the prompt. If Chrome uses a custom profile folder, set "
            "CHROME_USER_DATA_DIR to that folder before launching the app."
        )

    def _worker_loop(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            while True:
                task=self.run_queue.get()
                if task is None: return
                rid=task[0]; c=db()
                c.execute("UPDATE runs SET status='setup_required',ended_at=?,error=? WHERE id=?",(now(),"Playwright is not installed. Install the application requirements.",rid))
                log(c,rid,"error",f"Browser automation unavailable: {e}"); c.close(); self.active_run=None
        try:
            with sync_playwright() as p:
                while True:
                    task=self.run_queue.get()
                    if task is None: break
                    rid,start,maxn,interval=task; c=db()
                    try:
                        self._portal_worker(c,rid,start,maxn,interval,p)
                    except Exception as e:
                        if self.stop_event.is_set():
                            self._export(c,rid)
                            c.execute("UPDATE runs SET status='stopped',ended_at=? WHERE id=?",(now(),rid))
                            log(c,rid,"info","Run stopped; browser session remains open.")
                        else:
                            c.execute("UPDATE runs SET status='error',ended_at=?,error=? WHERE id=?",(now(),str(e),rid))
                            log(c,rid,"error",f"Run failed: {e}")
                    finally:
                        c.close(); self.active_run=None
        finally:
            # The Playwright driver exits here and drops its connection. Do not
            # call browser.close(): this is the user's daily Chrome instance.
            self.browser=self.page=None

    def shutdown(self):
        self.stop(); self.run_queue.put(None)
        if self.thread and self.thread.is_alive(): self.thread.join(timeout=10)

    def _ensure_browser(self,p):
        # A connection kept from an earlier run becomes stale when Chrome is
        # restarted or all its windows are closed; it then exposes no browser
        # contexts. Drop it so this run reconnects instead of failing.
        try:
            if self.browser is not None and not self.browser.contexts:
                self.browser=None
        except Exception:
            self.browser=None
        try:
            pages=[page for context in self.browser.contexts for page in context.pages] if self.browser else []
        except Exception:
            self.browser=None; pages=[]
        if self.browser is None:
            try:
                # Attach to the already-running Chrome instance. This requires
                # the user to enable Chrome's per-session remote-debugging
                # permission at chrome://inspect/#remote-debugging.
                endpoint = chrome_debugging_websocket_url()
                if not endpoint:
                    raise RuntimeError("Chrome DevToolsActivePort was not found")
                # no_defaults=False is required: with True, Playwright does not
                # expose the existing browser context, so contexts is empty and
                # the app cannot open/reuse a portal tab.
                self.browser=p.chromium.connect_over_cdp(endpoint, timeout=20000, no_defaults=False)
            except Exception as e:
                raise RuntimeError(
                    "Chrome's approved DevTools WebSocket did not complete its "
                    "connection. If Chrome is showing a permission prompt, allow "
                    "this app to connect, then try Start again. Confirm remote "
                    "debugging is enabled in the same Chrome profile. The scraper "
                    "will reuse an existing Growth tab or open a tab in that same "
                    "Chrome window; it will not launch another Chrome process."
                ) from e
            contexts=self.browser.contexts
            if not contexts:
                # Chrome can briefly expose no context right after it starts.
                time.sleep(1.5)
                contexts=self.browser.contexts
            pages=[page for context in contexts for page in context.pages]
        else:
            contexts=self.browser.contexts
        open_pages=[]
        for page in pages:
            try:
                if not page.is_closed(): open_pages.append(page)
            except Exception: pass
        # Prefer an existing portal tab to preserve the user's session. If
        # there isn't one, create a visible tab inside the already attached
        # regular Chrome context; this never launches another browser process.
        portal_pages=[]
        for page in open_pages:
            try:
                url=page.url.lower()
                if any(host in url for host in ("growth.gov.ae","id.moec.gov.ae","uaepass.ae")):
                    portal_pages.append(page)
            except Exception: pass
        if portal_pages:
            self.page=min(portal_pages,key=lambda page: 0 if "inquiryabouteconomiclicenses" in page.url.lower() else 1 if "economiclicensedetails" in page.url.lower() else 2)
        elif contexts:
            try:
                self.page=contexts[0].new_page()
            except Exception as e:
                raise RuntimeError(
                    "Connected to the existing Chrome browser, but could not "
                    "open a portal tab in its regular window. Keep Chrome open, "
                    "confirm remote debugging is enabled, and try Start again."
                ) from e
        else:
            raise RuntimeError(
                "Chrome's debugging connection succeeded, but Chrome exposed no "
                "regular browser context in which to open the portal tab. Confirm "
                "you enabled remote debugging in your normal Chrome profile."
            )
        self.page.bring_to_front()

    def _portal_worker(self,c,rid,start,maxn,interval,p):
        self._ensure_browser(p)
        self._check_stopped()
        try: current=self.page.url.lower()
        except Exception: current=""
        if "growth.gov.ae/g2c/inquiryabouteconomiclicenses" not in current and not any(host in current for host in ("id.moec.gov.ae", "uaepass.ae")):
            self.page.goto("https://www.growth.gov.ae/G2C/InquiryAboutEconomicLicenses",wait_until="domcontentloaded")
            try: self.page.wait_for_load_state("networkidle",timeout=8000)
            except Exception: pass
            log(c,rid,"info",f"Opened Growth portal in the existing Chrome window: {self.page.url}")
        self._check_stopped()
        self._wait_for_login_page()
        self._ensure_authenticated(c,rid)
        self._check_stopped()
        self._ensure_english_portal()
        self._check_stopped()
        num=start; done=0; consecutive_search_failures=0
        while done < maxn and not self.stop_event.is_set():
            self.pause_event.wait()
            if self.stop_event.is_set(): break
            if not self._portal_ready():
                self._ensure_authenticated(c,rid)
                if self.stop_event.is_set(): break
                self._ensure_english_portal()
            try:
                found,recs=self._search_license(num)
                c.execute("INSERT OR REPLACE INTO searches(license_number,searched_at,run_id,outcome,error) VALUES (?,?,?,?,NULL)",(num,now(),rid,"found" if found else "not_found"))
                if found:
                    for record in recs:
                        c.execute("INSERT INTO records(run_id,license_number,searched_at,data_json) VALUES (?,?,?,?)",(rid,str(record.get("license_number",num)),now(),json.dumps(record,ensure_ascii=False)))
                c.execute("UPDATE runs SET searched=searched+1,found=found+? WHERE id=?",(len(recs),rid))
                log(c,rid,"info",f"License {num}: {'found '+str(len(recs))+' record(s)' if found else 'no result'}")
                c.commit(); done+=1; num=next_license_number(num); consecutive_search_failures=0; self.stop_event.wait(interval)
            except PortalAuthRequired:
                if self.stop_event.is_set(): break
                log(c,rid,"warning",f"License {num}: the portal session needs signing in again; waiting for login.")
                c.commit()
                self._ensure_authenticated(c,rid)
                if self.stop_event.is_set(): break
                continue
            except PortalSearchError as e:
                if self.stop_event.is_set(): break
                # Do not stall the whole run on one silent portal search: skip
                # this number and keep going. Five consecutive failures point
                # to a systemic problem, so pause for the user to check.
                c.execute("INSERT OR REPLACE INTO searches(license_number,searched_at,run_id,outcome,error) VALUES (?,?,?,?,?)",(num,now(),rid,"skipped",str(e)))
                c.execute("UPDATE runs SET searched=searched+1 WHERE id=?",(rid,))
                log(c,rid,"warning",f"License {num}: {e}. Skipped; continuing with the next number.")
                c.commit(); done+=1; num=next_license_number(num); consecutive_search_failures+=1
                if consecutive_search_failures>=5:
                    log(c,rid,"error","Five consecutive searches got no answer from the portal; pausing so the session can be checked.")
                    c.execute("UPDATE runs SET status='paused' WHERE id=?",(rid,)); c.commit()
                    self.pause_event.clear()
                    while not self.stop_event.is_set() and not self.pause_event.wait(1):
                        pass
                    if self.stop_event.is_set(): break
                    c.execute("UPDATE runs SET status='running' WHERE id=?",(rid,)); c.commit()
                    consecutive_search_failures=0
                continue
            except Exception as e:
                if self.stop_event.is_set(): break
                c.execute("INSERT OR REPLACE INTO searches(license_number,searched_at,run_id,outcome,error) VALUES (?,?,?,?,?)",(num,now(),rid,"error",str(e)))
                log(c,rid,"error",f"License {num} could not be verified: {e}. The run has paused on this number.")
                c.execute("UPDATE runs SET status='paused' WHERE id=?",(rid,)); c.commit()
                self.pause_event.clear()
                while not self.stop_event.is_set() and not self.pause_event.wait(1):
                    pass
                if not self.stop_event.is_set():
                    c.execute("UPDATE runs SET status='running' WHERE id=?",(rid,)); c.commit()
        self._export(c,rid)
        status="stopped" if self.stop_event.is_set() else "completed"
        c.execute("UPDATE runs SET status=?,ended_at=? WHERE id=?",(status,now(),rid))
        log(c,rid,"info",f"Run {status}; browser session remains open."); c.commit()

    def _pause_for_human(self,c,rid,status,message):
        self.human_event.clear()
        self.pause_event.clear()
        c.execute("UPDATE runs SET status=? WHERE id=?", (status, rid))
        log(c, rid, "warning", message)
        c.commit()
        while not self.stop_event.is_set():
            if status == "awaiting_login" and self._find_ready_portal():
                self.resume()
                return
            if self.human_event.is_set():
                return
            if status == "awaiting_login" and self.browser:
                # Pump Playwright events so redirects and newly opened tabs
                # are delivered while the worker waits for manual sign-in.
                try:
                    pages=self.page.context.pages if self.page else []
                    (pages[0] if pages else self.page).wait_for_timeout(500)
                except Exception:
                    self.human_event.wait(0.5)
            else:
                self.human_event.wait(0.5)

    def _find_ready_portal(self):
        if self._portal_ready():
            return True
        # The sign-in flow can return to another tab in the same Chrome profile.
        if self.page is None:
            return False
        for page in self.page.context.pages:
            if page is not self.page and self._portal_ready(page):
                self.page = page
                return True
        return False

    def _ensure_authenticated(self,c,rid):
        while not self.stop_event.is_set():
            if self._find_ready_portal():
                c.execute("UPDATE runs SET status='running' WHERE id=?",(rid,)); c.commit()
                return
            page=self.page
            if not page: return
            url=page.url.lower()
            if "id.moec.gov.ae" in url and "/account/uaepass" in url:
                page.wait_for_timeout(1500)
                continue
            if ("id.moec.gov.ae" in url and "/account/login" in url) or ("growth.gov.ae" in url and not self._portal_ready()):
                try:
                    login=page.locator("a.UAEPassBtn")
                    if login.count()==0: login=page.get_by_role("link",name="Sign in with UAE PASS")
                    if login.count()==0: login=page.get_by_text("تسجيل الدخول بالهوية الرقمية",exact=False)
                    if login.count()==0: login=page.get_by_text("Sign in with UAE PASS",exact=False)
                    if login.count():
                        login.first.click(timeout=5000)
                        try: page.wait_for_url("**/Account/UAEPass**",timeout=10000)
                        except Exception: page.wait_for_timeout(2500)
                        continue
                    log(c,rid,"warning",f"Could not locate the UAE PASS login button on {page.url}; leaving the visible page for manual login.")
                except Exception as e:
                    log(c,rid,"warning",f"Unable to activate UAE PASS login button: {e}")
            if "uaepass" in url or "ids.uaepass" in url:
                if self._is_uae_pass_approval_screen():
                    self._await_uae_pass_approval(c,rid)
                    continue
                if self._is_challenge():
                    self._wait_for_manual_uae_pass_login(c,rid,"A CAPTCHA or reCAPTCHA error is shown. Retry the CAPTCHA if needed, then submit Login in the visible browser; the scraper will continue after the portal redirect.")
                    continue
                if self._submit_uae_pass_phone():
                    self._wait_for_manual_uae_pass_login(c,rid,"Your UAE PASS number is filled. Complete any CAPTCHA and submit Login in the visible browser; the scraper will continue after the portal redirect.")
                    continue
            if self._is_challenge():
                self._pause_for_human(c,rid,"awaiting_human","A CAPTCHA or verification step is open. Complete it in the visible browser, then click Resume.")
                continue
            self._pause_for_human(c,rid,"awaiting_login","Waiting for the Growth license search form. Finish signing in in Chrome or switch to the signed-in license search tab.")
            if self._portal_ready():
                c.execute("UPDATE runs SET status='running' WHERE id=?",(rid,)); c.commit(); return

    def _wait_for_login_page(self, timeout_seconds=20):
        deadline=time.monotonic()+timeout_seconds
        while not self.stop_event.is_set() and time.monotonic()<deadline:
            try:
                if self._portal_ready(): return
                url=self.page.url.lower()
                if "uaepass" in url or "ids.uaepass" in url: return
                if self.page.locator("a.UAEPassBtn").count(): return
            except Exception:
                pass
            self.page.wait_for_timeout(500)

    def _await_uae_pass_approval(self,c,rid):
        c.execute("UPDATE runs SET status='awaiting_approval' WHERE id=?",(rid,))
        log(c,rid,"warning","UAE PASS login request sent. Approve it in the UAE PASS app; the scraper is watching for the portal redirect.")
        c.commit(); self.human_event.clear(); self.pause_event.clear()
        while not self.stop_event.is_set():
            try:
                self.page.wait_for_timeout(800)
            except Exception:
                pass
            if self._portal_ready():
                self.human_event.set(); self.pause_event.set()
                c.execute("UPDATE runs SET status='running' WHERE id=?",(rid,))
                log(c,rid,"info","UAE PASS approval detected; continuing the run."); c.commit()
                return
            if self._is_auth_error():
                self._pause_for_human(c,rid,"awaiting_human","UAE PASS returned an error page. Click Back to Home in the visible browser, wait for the login page, then click Resume.")
                if not self._is_auth_error(): return
            if self._is_challenge():
                c.execute("UPDATE runs SET status='awaiting_human' WHERE id=?",(rid,))
                log(c,rid,"warning","UAE PASS reCAPTCHA verification failed. Complete or retry the verification manually in the visible browser, then click Resume.")
                c.commit()
                self.human_event.clear(); self.pause_event.clear()
                self.human_event.wait(); self.pause_event.wait()
                if self._portal_ready(): return

    def _wait_for_manual_uae_pass_login(self,c,rid,message):
        c.execute("UPDATE runs SET status='awaiting_human' WHERE id=?",(rid,))
        log(c,rid,"warning",message)
        c.commit(); self.human_event.clear(); self.pause_event.clear()
        while not self.stop_event.is_set():
            try:
                self.page.wait_for_timeout(800)
            except Exception:
                pass
            if self._portal_ready():
                self.human_event.set(); self.pause_event.set()
                c.execute("UPDATE runs SET status='running' WHERE id=?",(rid,))
                log(c,rid,"info","Manual UAE PASS login detected; continuing the run."); c.commit()
                return
            if self._is_uae_pass_approval_screen():
                self._await_uae_pass_approval(c,rid)
                return

    def _submit_uae_pass_phone(self):
        # Never publish or assume a user's personal UAE PASS number. Autofill
        # is optional and only enabled when configured on the local machine.
        if not UAE_PASS_PHONE:
            return False
        page=self.page
        try:
            fields=page.locator("input").all()
            phone=None
            for field in fields:
                typ=(field.get_attribute("type") or "").lower()
                ph=(field.get_attribute("placeholder") or "").lower()
                aria=(field.get_attribute("aria-label") or "").lower()
                if typ in ("tel","number") or any(x in ph+" "+aria for x in ("mobile","phone","رقم الهاتف","رقم الموبايل")):
                    phone=field; break
            if phone is None:
                visible=[field for field in fields if field.is_visible()]
                if len(visible)==1: phone=visible[0]
            if phone is None: return False
            phone.fill(UAE_PASS_PHONE)
            button=page.get_by_role("button",name="Login",exact=True)
            if button.count()==0: button=page.get_by_text("Login",exact=True)
            if button.count()==0: button=page.get_by_text("تسجيل الدخول",exact=False)
            if button.count()==0: return False
            # reCAPTCHA is intentionally human-controlled: submit only after
            # the user has completed any CAPTCHA in the visible UAE PASS page.
            return True
        except Exception:
            return False

    def _is_challenge(self):
        try:
            text=self.page.locator("body").inner_text(timeout=1500).lower()
            return any(term in text for term in ("captcha", "verify you are human", "security check", "تعذر التحقق", "يرجى محاولة تحميل الدخول مرة أخرى", "التحقق من أنك إنسان"))
        except Exception:
            return False

    def _is_auth_error(self):
        try:
            if not self.page or "ids.uaepass.ae" not in self.page.url.lower(): return False
            text=self.page.locator("body").inner_text(timeout=1500).lower()
            return any(term in text for term in ("something went wrong at our end", "sorry!", "back to home", "حدث خطأ"))
        except Exception:
            return False

    def _is_uae_pass_approval_screen(self):
        try:
            if not self.page or "uaepass" not in self.page.url.lower(): return False
            text=self.page.locator("body").inner_text(timeout=1500).lower()
            return any(term in text for term in ("approve login request", "waiting for your confirmation", "confirm to login", "approve the login"))
        except Exception:
            return False

    def _license_search_field(self, page):
        terms = ("license", "economic", "الرخص", "الاقتصادي")
        selector = ", ".join(
            f'input[{attribute}*="{term}" i]'
            for attribute in ("placeholder", "aria-label")
            for term in terms
        )
        return page.locator(selector).filter(visible=True).first

    def _portal_ready(self, page=None):
        page = self.page if page is None else page
        if page is None:
            return False
        try:
            url = urlparse(page.url)
            if url.hostname not in ("growth.gov.ae", "www.growth.gov.ae"):
                return False
            if url.path.rstrip("/").lower() != "/g2c/inquiryabouteconomiclicenses":
                return False
            return self._license_search_field(page).is_visible()
        except Exception:
            return False

    def _ensure_english_portal(self):
        """Switch the portal to English when the language toggle offers it."""
        if not self.page or not self._portal_ready(): return
        try:
            english=self.page.get_by_text("English",exact=True)
            if english.count() and english.first.is_visible():
                english.first.click(timeout=3000)
                try: self.page.wait_for_load_state("domcontentloaded",timeout=10000)
                except Exception: pass
                self.page.wait_for_timeout(700)
        except Exception:
            pass

    def _reload_portal(self,page):
        """Reset the OutSystems page; returns False when it lands on login."""
        try:
            page.goto("https://www.growth.gov.ae/G2C/InquiryAboutEconomicLicenses",wait_until="domcontentloaded",timeout=30000)
            self._license_search_field(page).wait_for(state="visible",timeout=20000)
            return True
        except Exception:
            return False

    def _search_license(self,num):
        self._check_stopped()
        page=self.page
        # Drive the search by the portal's own results request. Reading the
        # rendered table is unreliable: a click can be swallowed while the
        # OutSystems app is still hydrating after a navigation, and a search
        # with no matches renders no table at all.
        payload=None; last_error=None
        for attempt in range(6):
            self._check_stopped()
            if not self._portal_ready():
                raise PortalAuthRequired()
            try:
                target=self._license_search_field(page)
                target.fill(str(num))
                search=page.get_by_role("button",name="Search",exact=True)
                if search.count()==0: search=page.get_by_role("button",name="بحث",exact=True)
                with page.expect_response(
                    lambda r: "DataActionGetEconomicLicences2" in r.url,
                    timeout=8000,
                ) as info:
                    search.click()
                payload=info.value.json()
                break
            except Exception as e:
                last_error=e
                # The OutSystems page can stop firing search requests after a
                # long session; reset it, then try again.
                if attempt >= 1:
                    if not self._reload_portal(page):
                        raise PortalAuthRequired()
                else:
                    try: page.wait_for_timeout(1000)
                    except Exception: time.sleep(1.0)
        if payload is None:
            raise PortalSearchError(f"the portal did not answer the search request ({last_error})")
        records=(((payload or {}).get("data") or {}).get("List") or {}).get("List") or []
        if not records:
            return False,[]
        out=[]; results_url=page.url
        try:
            for record in records:
                self._check_stopped()
                ern=str(record.get("UnifiedNumber") or "").strip()
                if not ern:
                    continue
                href=urljoin(page.url,"/G2C/EconomicLicenseDetails?LicenceNumber="+ern)
                page.goto(href,wait_until="domcontentloaded",timeout=20000)
                self._check_stopped()
                if "economiclicensedetails" not in page.url.lower():
                    raise RuntimeError(f"License {num}: Details URL did not open a license detail page ({href})")
                # The portal renders the detail fields after the initial
                # document load; wait for them before extracting.
                try: page.wait_for_load_state("networkidle",timeout=8000)
                except Exception: pass
                if not self._wait_for_detail_ready(num):
                    saved=self._save_detail_diagnostic(num)
                    raise RuntimeError(
                        f"License {num}: the Details page opened but its fields never rendered"
                        + (f" (diagnostic saved to {saved})" if saved else "")
                    )
                out.append(self._extract_detail_record(num))
        finally:
            # Keep the next sequential search on the portal search form.
            if not self.stop_event.is_set() and "economiclicensedetails" in page.url.lower():
                page.goto(results_url,wait_until="domcontentloaded",timeout=20000)
                try: self._license_search_field(page).wait_for(state="visible",timeout=10000)
                except Exception: pass
        return bool(out),out

    def _check_stopped(self):
        if self.stop_event.is_set():
            raise RuntimeError("Run stopped")

    def _wait_for_portal_result(self, expression, seconds):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            self._check_stopped()
            try:
                self.page.wait_for_function(expression,timeout=1000)
                return
            except Exception as error:
                if "Timeout" not in type(error).__name__:
                    raise
        raise TimeoutError("Portal result did not appear before the timeout")

    def _wait_for_detail_ready(self, num, timeout_seconds=20):
        """Wait until the portal has rendered labelled detail fields."""
        deadline=time.monotonic()+timeout_seconds
        while time.monotonic()<deadline:
            self._check_stopped()
            try:
                ready=self.page.evaluate("""() => {
                    const items=[...document.querySelectorAll('.basic-info .item')];
                    const labelled=items.some(node => {
                        const title=(node.querySelector(':scope > .title')?.textContent||'').trim();
                        const value=(node.querySelector('.value')?.textContent||'').trim();
                        return title && value;
                    });
                    if (labelled) return true;
                    const text=(document.body?.innerText||'').toLowerCase();
                    return /license number|license status|رقم الرخصة|حالة الرخصة/.test(text);
                }""")
                if ready: return True
            except Exception:
                # The document may still be navigating; retry until deadline.
                pass
            try: self.page.wait_for_timeout(500)
            except Exception: time.sleep(0.5)
        return False

    def _save_detail_diagnostic(self, num):
        """Save the unrendered Details page for offline selector work."""
        try:
            folder=DATA/"diagnostics"; folder.mkdir(parents=True,exist_ok=True)
            stamp=datetime.now().strftime("%Y%m%d-%H%M%S")
            base=folder/f"detail-{num}-{stamp}"
            base.with_suffix(".html").write_text(self.page.content(),encoding="utf-8")
            base.with_suffix(".txt").write_text(
                f"URL: {self.page.url}\n\n"+self.page.locator("body").inner_text()[:8000],
                encoding="utf-8",
            )
            return base.with_suffix(".html").relative_to(DATA)
        except Exception:
            return None

    def _extract_detail_record(self, searched_number):
        import re

        # OutSystems renders each label and value as sibling spans. Reading
        # body text loses empty mailboxes and multi-value manager lists.
        def read_items(selector):
            return self.page.locator(selector).evaluate_all("""nodes => nodes.map(node => ({
            label: (node.querySelector(':scope > .title')?.textContent || '').replace(/\\s+/g, ' ').trim().replace(/\\s*[:：]$/, '').toLowerCase(),
            values: [...node.querySelectorAll(':scope > .value, :scope > ul .value')].map(el => el.textContent.trim())
        }))""")
        items=read_items(".tabs-content-tab.open .basic-info .item")
        if not items:
            # The Details page can open on a different tab; read every rendered
            # basic-info block rather than only the currently active one.
            items=read_items(".basic-info .item")
        if items:
            record={field:"" for field in FIELDS}
            mailboxes=[]
            for item in items:
                label=item["label"]
                value="; ".join(v for v in item["values"] if v)
                if label=="license status": record["license_status"]=value
                elif label=="license number": record["license_number"]=value
                elif label=="english name": record["english_name"]=value
                elif label=="mobile number": record["mobile_number"]=value
                elif label=="mail box": mailboxes.append(value)
                elif label=="manager names in english": record["manager_names_english"]=value
            record["mailbox_1"]=(mailboxes+[""])[0]
            record["mailbox_2"]=(mailboxes+["",""])[1]
            if record["license_number"] and record["english_name"]:
                return record

        body=self.page.locator("body").inner_text()
        lines=[re.sub(r"\s+"," ",line).strip() for line in body.splitlines()]
        lines=[line for line in lines if line]
        record={field:"" for field in FIELDS}
        labels_by_field=[
            ("license_status",("License status","License Status","حالة الرخصة","حالة الترخيص")),
            ("license_number",("License number","رقم الرخصة","رقم الترخيص")),
            ("english_name",("English Name","Economic Name in English","الاسم باللغة الإنجليزية","الاسم الاقتصادي باللغة الإنجليزية")),
            ("mobile_number",("Mobile number","Mobile","Phone number","رقم الهاتف المتحرك","رقم الهاتف")),
            ("mailbox_1",("Mailbox 1","Mailbox","Email","البريد الإلكتروني","البريد الالكتروني")),
            ("mailbox_2",("Mailbox 2","Secondary Email","البريد الإلكتروني 2","البريد الالكتروني 2")),
            ("manager_names_english",("Manager Names In English","Manager Names in English","Managers in English","أسماء المديرين باللغة الإنجليزية","أسماء المديرين بالإنجليزية")),
        ]
        for field,labels in labels_by_field:
            for label in labels:
                pattern=re.compile(r"^\s*"+re.escape(label)+r"\s*[:：]?\s*(.*?)\s*$",re.I)
                for index,line in enumerate(lines):
                    match=pattern.match(line)
                    if not match:
                        continue
                    value=match.group(1).strip()
                    if not value and index+1<len(lines):
                        value=lines[index+1]
                    if value and value.lower() not in ("license status","license number","english name","mobile number","mailbox","manager names in english"):
                        record[field]=value
                        break
                if record[field]:
                    break
        if not record["license_number"]:
            record["license_number"]=str(searched_number)
        if not any(record[k] for k in ("license_status","english_name","mobile_number","mailbox_1","mailbox_2","manager_names_english")):
            raise RuntimeError(f"Opened Details for search {searched_number}, but none of the requested fields were recognized")
        return record

    def _export(self,c,rid):
        row=c.execute("SELECT * FROM runs WHERE id=?",(rid,)).fetchone(); records=c.execute("SELECT data_json FROM records WHERE run_id=? ORDER BY id",(rid,)).fetchall()
        date=datetime.fromisoformat(row["started_at"]).astimezone().strftime("%Y-%m-%d")
        folder=EXPORTS/date; folder.mkdir(parents=True,exist_ok=True)
        runno=max((int(p.stem.split()[-1]) for p in folder.glob("Run *.xlsx") if p.stem.split()[-1].isdigit()), default=0)+1; path=folder/f"Run {runno}.xlsx"
        rows=[]
        for r in records:
            d=json.loads(r["data_json"]); rows.append([d.get(k,"") for k in FIELDS])
        path.write_bytes(xlsx_bytes(FIELDS,rows)); c.execute("UPDATE runs SET export_path=? WHERE id=?",(str(path.relative_to(EXPORTS)),rid)); c.commit()

    def pause(self): self.pause_event.clear()
    def resume(self):
        self.pause_event.set()
        self.human_event.set()
    def stop(self):
        self.stop_event.set(); self.human_event.set(); self.pause_event.set()
        with self.lock:
            if self.active_run is not None:
                c=db()
                changed=c.execute("UPDATE runs SET status='stopping' WHERE id=? AND status NOT IN ('stopped','completed','error')",(self.active_run,)).rowcount
                if changed:
                    log(c,self.active_run,"info","Stop requested; finishing the current browser step and saving collected records.")
                c.close()

RUNNER=Runner()

class Handler(BaseHTTPRequestHandler):
    def _json(self,obj,status=200):
        raw=json.dumps(obj,ensure_ascii=False).encode(); self.send_response(status); self.send_header("Content-Type","application/json; charset=utf-8"); self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_GET(self):
        path=urlparse(self.path).path
        if path=="/api/state": return self._json(RUNNER.state())
        if path=="/":
            raw=(STATIC/"index.html").read_bytes(); self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8"); self.send_header("Cache-Control","no-store"); self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw); return
        if path.startswith("/exports/"):
            relative_path=unquote(path.removeprefix("/exports/"))
            filepath=(EXPORTS/relative_path).resolve()
            if not filepath.is_relative_to(EXPORTS.resolve()) or not filepath.is_file(): return self.send_error(404)
            raw=filepath.read_bytes(); self.send_response(200); self.send_header("Content-Type","application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"); self.send_header("Content-Disposition",f'attachment; filename="{filepath.name}"'); self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw); return
        return self.send_error(404)
    def do_POST(self):
        path=urlparse(self.path).path; length=int(self.headers.get("Content-Length",0)); payload=json.loads(self.rfile.read(length) or b"{}")
        try:
            if path=="/api/start": RUNNER.start(payload); return self._json({"ok":True})
            if path=="/api/pause": RUNNER.pause(); return self._json({"ok":True})
            if path=="/api/resume": RUNNER.resume(); return self._json({"ok":True})
            if path=="/api/stop": RUNNER.stop(); return self._json({"ok":True})
            if path=="/api/settings":
                c=db()
                try:
                    current={r["key"]:r["value"] for r in c.execute("SELECT key,value FROM settings")}
                    initial=str(payload.get("initial_number",current["initial_number"])).strip()
                    max_records=int(payload.get("max_records",current["max_records"]))
                    interval=float(payload.get("interval_seconds",current["interval_seconds"]))
                    try: next_license_number(initial)
                    except ValueError: raise ValueError("Starting number must contain a number to increment")
                    if max_records < 1 or max_records > 100000: raise ValueError("Maximum numbers must be between 1 and 100000")
                    if interval < 0.5 or interval > 3600: raise ValueError("Interval must be between 0.5 and 3600 seconds")
                    c.executemany("UPDATE settings SET value=? WHERE key=?",[(initial,"initial_number"),(str(max_records),"max_records"),(str(interval),"interval_seconds")])
                    c.commit()
                finally:
                    c.close()
                return self._json({"ok":True})
            return self.send_error(404)
        except Exception as e: return self._json({"error":str(e)},400)
    def log_message(self,*args): pass

if __name__=="__main__":
    url=f"http://127.0.0.1:{PORT}"
    try:
        server=ThreadingHTTPServer(("127.0.0.1",PORT),Handler)
    except OSError:
        # Another copy already serves the dashboard. Open that one instead of
        # exiting silently (a windowed build shows no error).
        print(f"A copy of Growth License Scrapper is already running at {url}")
        if os.environ.get("OPEN_DASHBOARD", "1") != "0":
            webbrowser.open(url)
        raise SystemExit(0)
    print(f"Growth License Scrapper is running at {url}")
    if os.environ.get("OPEN_DASHBOARD", "1") != "0":
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        RUNNER.shutdown()
        server.server_close()
