from __future__ import annotations

import json
import os
import queue
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
    CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, ended_at TEXT, status TEXT NOT NULL, start_number INTEGER NOT NULL, max_records INTEGER NOT NULL, interval_seconds REAL NOT NULL, searched INTEGER DEFAULT 0, found INTEGER DEFAULT 0, error TEXT, export_path TEXT);
    CREATE TABLE IF NOT EXISTS records (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL, license_number TEXT NOT NULL, searched_at TEXT NOT NULL, data_json TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS searches (license_number INTEGER PRIMARY KEY, searched_at TEXT NOT NULL, run_id INTEGER NOT NULL, outcome TEXT NOT NULL, error TEXT);
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
        last=c.execute("SELECT MAX(license_number) AS n FROM searches").fetchone()["n"]
        settings={r["key"]:r["value"] for r in c.execute("SELECT * FROM settings")}
        if latest:
            pending=int(latest["start_number"])+int(latest["searched"] or 0)
            first_error=c.execute(
                "SELECT MIN(license_number) FROM searches WHERE run_id=? AND outcome='error'",
                (latest["id"],),
            ).fetchone()[0]
            if first_error is not None:
                pending=min(pending,int(first_error))
        else:
            pending=(int(last)+1) if last is not None else int(settings["initial_number"])
        count=c.execute("SELECT COUNT(*) FROM searches WHERE substr(searched_at,1,10)=?",(datetime.now().astimezone().date().isoformat(),)).fetchone()[0]
        logs=[dict(r) for r in c.execute("SELECT created_at,level,message FROM logs ORDER BY id DESC LIMIT 30")]
        runs=[dict(r) for r in c.execute("SELECT id,started_at,ended_at,status,start_number,max_records,interval_seconds,searched,found,error,export_path FROM runs ORDER BY id DESC LIMIT 20")]
        c.close()
        return {"running": self.active_run is not None,"paused":not self.pause_event.is_set(),"waiting_for_human":not self.human_event.is_set() and bool(self.active_run),"next_number":pending,"searched_today":count,"latest_run":dict(latest) if latest else None,"settings":settings,"logs":logs,"runs":runs}

    def start(self, cfg):
        with self.lock:
            if self.active_run is not None: raise ValueError("A run is already active")
            self._require_chrome_remote_debugging()
            c=db(); st={r["key"]:r["value"] for r in c.execute("SELECT * FROM settings")}
            last=c.execute("SELECT MAX(license_number) FROM searches").fetchone()[0]
            latest=c.execute("SELECT start_number,searched FROM runs ORDER BY id DESC LIMIT 1").fetchone()
            automatic=(int(latest["start_number"])+int(latest["searched"] or 0)) if latest else ((int(last)+1) if last is not None else int(st["initial_number"]))
            if latest:
                first_error=c.execute(
                    "SELECT MIN(license_number) FROM searches WHERE run_id=(SELECT MAX(id) FROM runs) AND outcome='error'"
                ).fetchone()[0]
                if first_error is not None:
                    automatic=min(automatic,int(first_error))
            start=int(cfg["start_number"]) if cfg.get("start_number") not in (None,"") else automatic
            if start < 1: raise ValueError("Starting license number must be at least 1")
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
        try:
            pages=self.browser.pages if self.browser else []
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
                self.browser=p.chromium.connect_over_cdp(endpoint, timeout=10000, no_defaults=True)
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
            self.page=portal_pages[0]
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

    def _portal_worker(self,c,rid,start,maxn,interval,p):
        self._ensure_browser(p)
        try: current=self.page.url.lower()
        except Exception: current=""
        if current in ("about:blank", "chrome://newtab/") or "economiclicensedetails" in current:
            self.page.goto("https://www.growth.gov.ae/G2C/InquiryAboutEconomicLicenses",wait_until="domcontentloaded")
            try: self.page.wait_for_load_state("networkidle",timeout=8000)
            except Exception: pass
            log(c,rid,"info",f"Opened Growth portal in the existing Chrome window: {self.page.url}")
        self._wait_for_login_page()
        self._ensure_authenticated(c,rid)
        self._ensure_english_portal()
        num=start; done=0
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
                c.commit(); done+=1; num+=1; time.sleep(interval)
            except Exception as e:
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
        c.execute("UPDATE runs SET status=? WHERE id=?",(status,rid)); log(c,rid,"warning",message); c.commit()
        self.human_event.clear(); self.pause_event.clear()
        while not self.stop_event.is_set():
            self.human_event.wait()
            self.pause_event.wait()
            if self.stop_event.is_set(): return
            return

    def _ensure_authenticated(self,c,rid):
        while not self.stop_event.is_set():
            if self._portal_ready():
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
            self._pause_for_human(c,rid,"awaiting_login","Login is required. Complete the login in the visible browser, then click Resume.")
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

    def _portal_ready(self):
        if not self.page: return False
        try:
            url=self.page.url.lower()
            if "uaepass" in url or "login" in url or "moec.gov.ae/account" in url: return False
            if "growth.gov.ae" not in url: return False
            for field in self.page.locator("input").all():
                ph=(field.get_attribute("placeholder") or "").lower()
                aria=(field.get_attribute("aria-label") or "").lower()
                searchable=ph+" "+aria
                if any(term in searchable for term in ("license", "economic", "الرخصة", "الاقتصادي", "السجل الاقتصادي")): return True
            return False
        except Exception: return False

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

    def _search_license(self,num):
        page=self.page
        boxes=page.locator("input").all()
        target=None
        for b in boxes:
            try:
                ph=(b.get_attribute("placeholder") or "").lower()
                aria=(b.get_attribute("aria-label") or "").lower()
                if any(term in ph+" "+aria for term in ("license", "economic", "الرخصة", "الاقتصادي", "السجل الاقتصادي")): target=b; break
            except Exception: pass
        if target is None: raise RuntimeError("Could not identify portal license search field")
        target.fill(str(num))
        search=page.get_by_role("button",name="Search",exact=True)
        if search.count()==0: search=page.get_by_role("button",name="بحث",exact=True)
        search.click()
        try:
            page.wait_for_function("""() => {
                const nodes = [...document.querySelectorAll('[aria-busy="true"], [role="progressbar"], [class*="spinner" i], [class*="loading" i], [class*="loader" i]')];
                return !nodes.some(el => {
                    const s = getComputedStyle(el), r = el.getBoundingClientRect();
                    return s.display !== 'none' && s.visibility !== 'hidden' && Number(s.opacity || 1) > 0 && r.width > 0 && r.height > 0;
                });
            }""", timeout=30000)
        except Exception as e:
            raise RuntimeError(f"Portal results remained in a loading state: {e}") from e
        try: page.wait_for_load_state("networkidle", timeout=8000)
        except Exception: pass
        # OutSystems updates the table asynchronously after the overlay goes
        # away. Wait for a settled row or a real empty-results message.
        try:
            page.wait_for_function("""() => {
                const rows=[...document.querySelectorAll('table tbody tr')];
                return rows.some(row => /\\b\\d{15,}\\b/.test(row.innerText))
                    || rows.some(row => /no records|no data|no matching|لا توجد|لم يتم العثور|لا يوجد بيانات/i.test(row.innerText));
            }""", timeout=30000)
        except Exception as e:
            raise RuntimeError(f"License {num}: results did not settle into a record or an empty result: {e}") from e
        rows=page.locator("table tbody tr"); n=rows.count(); out=[]; visible_result_rows=0; empty_result_rows=0; detail_urls=[]
        for i in range(n):
            row=rows.nth(i); text=row.inner_text()
            lower=text.lower().strip()
            if not lower: continue
            if any(marker in lower for marker in ("no records", "no data", "no matching", "لا توجد", "لم يتم العثور", "لا يوجد بيانات")):
                empty_result_rows+=1
                continue
            visible_result_rows+=1
            import re
            links=row.locator("a")
            row_hrefs=[]
            detail_text=row.get_by_text(re.compile(r"details|التفاصيل|تفاصيل",re.I))
            for link in links.all():
                try:
                    href=link.evaluate("element => element.href")
                    if href and "economiclicensedetails" in href.lower():
                        row_hrefs.append(href)
                except Exception:
                    continue
            if not row_hrefs:
                if detail_text.count():
                    # The visible text can be nested in a span inside the link.
                    anchor=detail_text.first.locator("xpath=ancestor-or-self::a[1]")
                    if anchor.count():
                        try:
                            href=anchor.first.evaluate("element => element.href")
                            if href and "economiclicensedetails" in href.lower():
                                row_hrefs.append(href)
                        except Exception:
                            pass
            if not row_hrefs:
                # The portal can render Details as a scripted control. Its
                # detail URL is built from the ERN shown in the result row.
                ern=re.search(r"\b\d{15,}\b",text)
                if ern:
                    row_hrefs.append(urljoin(page.url,"/G2C/EconomicLicenseDetails?LicenceNumber="+ern.group()))
            for href in row_hrefs:
                href=urljoin(page.url,href)
                if href not in detail_urls:
                    detail_urls.append(href)

        # Capture the result links before navigating. The previous loop clicked
        # the first Details link, then looked for more rows on the detail page;
        # consequently only the first result could ever be considered.
        if detail_urls:
            results_url=page.url
            try:
                for href in detail_urls:
                    page.goto(href,wait_until="domcontentloaded",timeout=20000)
                    if "economiclicensedetails" not in page.url.lower():
                        raise RuntimeError(f"License {num}: Details URL did not open a license detail page ({href})")
                    try: page.wait_for_load_state("networkidle",timeout=5000)
                    except Exception: pass
                    record=self._extract_detail_record(num)
                    out.append(record)
            finally:
                # Keep the next sequential search on the portal search form,
                # even if a detail page is malformed or times out.
                if "economiclicensedetails" in page.url.lower():
                    page.goto(results_url,wait_until="domcontentloaded",timeout=20000)
        if visible_result_rows and not detail_urls:
            raise RuntimeError(f"License {num}: {visible_result_rows} result row(s) appeared, but no clickable Details links were found")
        if visible_result_rows and not out:
            raise RuntimeError(f"License {num}: {visible_result_rows} result row(s) appeared, but no details were extracted")
        if not visible_result_rows and not empty_result_rows:
            raise RuntimeError(f"License {num}: search results did not finish loading; retry this number")
        return bool(out),out

    def _extract_detail_record(self, searched_number):
        import re

        # OutSystems renders each label and value as sibling spans. Reading
        # body text loses empty mailboxes and multi-value manager lists.
        items=self.page.locator(".tabs-content-tab.open .basic-info .item").evaluate_all("""nodes => nodes.map(node => ({
            label: (node.querySelector(':scope > .title')?.textContent || '').replace(/\\s+/g, ' ').trim().replace(/\\s*[:：]$/, '').toLowerCase(),
            values: [...node.querySelectorAll(':scope > .value, :scope > ul .value')].map(el => el.textContent.trim())
        }))""")
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
    def resume(self): self.human_event.set(); self.pause_event.set()
    def stop(self): self.stop_event.set(); self.human_event.set(); self.pause_event.set()

RUNNER=Runner()

class Handler(BaseHTTPRequestHandler):
    def _json(self,obj,status=200):
        raw=json.dumps(obj,ensure_ascii=False).encode(); self.send_response(status); self.send_header("Content-Type","application/json; charset=utf-8"); self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_GET(self):
        path=urlparse(self.path).path
        if path=="/api/state": return self._json(RUNNER.state())
        if path=="/":
            raw=(STATIC/"index.html").read_bytes(); self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8"); self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw); return
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
                c=db(); initial=int(payload.get("initial_number",10000)); c.execute("UPDATE settings SET value=? WHERE key='initial_number'",(str(initial),)); c.commit(); c.close(); return self._json({"ok":True})
            return self.send_error(404)
        except Exception as e: return self._json({"error":str(e)},400)
    def log_message(self,*args): pass

if __name__=="__main__":
    server=ThreadingHTTPServer(("127.0.0.1",PORT),Handler)
    url=f"http://127.0.0.1:{PORT}"
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
