# Growth License Scrapper — Project Guide

A local, cross-platform dashboard and automation tool for authorized lookups on the
UAE **Growth** economic-license portal
(<https://www.growth.gov.ae/G2C/InquiryAboutEconomicLicenses>).

The app attaches to the user's **already-running Google Chrome** (via Chrome's
permission-based remote debugging), searches license numbers sequentially, opens
each result row's Details page, extracts the requested fields, and saves each run
to SQLite plus an Excel/CSV export.

> Human login (UAE PASS) and CAPTCHA are part of the flow and are never solved or
> bypassed by the app. It waits for you to complete them.

---

## 1. Features

- Sequential license-number search with **format-aware increments** (see §2).
- **All result rows** for a search are opened one by one (the portal caps results at 5).
- Detail extraction: license status, license number, English name, mobile number,
  mailbox 1 & 2, manager names in English.
- Runs are stored in SQLite with logs, progress, history and per-run exports.
- **Excel (.xlsx) and CSV** downloads from the dashboard.
- Exports are rebuilt from SQLite if a saved workbook is missing or corrupt.
- Recovers from silent portal searches (reload + retry, then skip instead of stalling).
- Drops stale Chrome debugging connections so a new run reconnects cleanly.
- Local-only: binds `127.0.0.1`, no external services.

---

## 2. License number formats (auto-detected)

Enter the starting number as **text** in the dashboard; the app detects the
incrementing part:

| Start value        | Next values                                             |
|--------------------|---------------------------------------------------------|
| `DMCC-064581`      | `DMCC-064582`, `DMCC-064583`, … (text prefix, 6-digit zero padding kept) |
| `CN_3645562`       | `CN_3645563`, `CN_3645564`, …                            |
| `2202345.01`       | `2202346.01`, `2202347.01`, … (only the part before the dot) |
| `2627017048888`    | `2627017058888`, `2627017068888`, … (`26` + middle 7 digits + `8888`) |
| `2427975`          | `2427976`, `2427977`, … (plain number)                   |

Rules (in `next_license_number()` in `app.py`):

1. `prefix + digits + .suffix` → increment the digits before the dot.
2. A plain 13-digit number → first 2 and last 4 digits fixed, middle 7 increment.
3. `text prefix + trailing digits` → increment the trailing digits, keeping width.
4. Any other plain number → increment by 1.

To support a new long-number series with a different fixed prefix/suffix, extend
`next_license_number()`.

---

## 3. Requirements

- macOS 11+ (arm64 tested) or Windows 10/11.
- **Google Chrome** installed and signed in to the Growth portal.
- Python 3.10+ only when running/building from source.
- Internet access to `growth.gov.ae` / `id.moec.gov.ae` / `ids.uaepass.ae`.

Python packages (`requirements.txt`):

```
playwright>=1.60,<2
pyinstaller>=6.10,<7
```

---

## 4. Chrome setup (once per browser session)

The app never launches its own browser; it attaches to yours.

1. Open Chrome in the profile that has your Growth/UAE PASS session.
2. Go to `chrome://inspect/#remote-debugging`, enable it, and **approve the prompt**.
3. Leave Chrome running. The app reads Chrome's `DevToolsActivePort` file to connect.

If Chrome uses a custom profile directory, set `CHROME_USER_DATA_DIR` to it before
launching the app.

---

## 5. Run from source

### macOS / Linux

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python app.py
```

### Windows (PowerShell)

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

The dashboard opens at **<http://127.0.0.1:8765/>** (set `PORT` to change).
`OPEN_DASHBOARD=0` prevents the automatic browser tab.

---

## 6. Using the dashboard

1. Open the dashboard and set:
   - **License number starting point (this run)** — text, any supported format.
   - **Maximum numbers to search** and **Interval between searches (seconds)**.
2. Click **Start**.
3. If the portal asks for UAE PASS/CAPTCHA, complete it in the visible Chrome tab;
   the app detects the signed-in portal and continues automatically.
4. Watch **Recent activity**, **Run history & exports**, and the progress bar.
5. **XLSX** / **CSV** links download a run's records.

Buttons: `Start`, `Pause`, `Resume / I finished login`, `Stop (keep browser open)`,
`Save settings`.

---

## 7. Data locations

| OS      | Path |
|---------|------|
| macOS   | `~/Library/Application Support/Growth License Scrapper` |
| Windows | `%LOCALAPPDATA%\Growth License Scrapper` |
| Linux   | `$XDG_DATA_HOME/growth-license-scrapper` |

Contents: `scraper.sqlite3` (history, searches, records, logs), `exports/YYYY-MM-DD/Run N.xlsx`,
and `diagnostics/` (saved page snapshots when a Details page fails to render).

Override with `LICENSE_SCRAPPER_DATA=/path`.

> To back up or move history, close the app and copy that folder.

---

## 8. Export endpoints

- `GET /api/export/<run_id>/<filename>.xlsx` — the run's Excel workbook.
- `GET /api/export/<run_id>/<filename>.csv?format=csv` — the same records as
  UTF-8 CSV (BOM for Excel).

The filename is part of the URL so browsers always name the download correctly.
If the saved workbook is missing or is not a real `.xlsx` (does not start with the
ZIP signature `PK`), the server rebuilds it from the SQLite records.
Every download is logged in Recent activity:
`Export download: run N -> Run N.xlsx (… bytes)`.

---

## 9. Build a Windows installer

Requires Windows and Python 3.10+.

1. (Once) Install [Inno Setup 6](https://jrsoftware.org/isdl.php).
2. From the project root in PowerShell:

```powershell
.\build_windows.ps1
```

The script creates `.build-venv`, installs dependencies, runs PyInstaller, and (if
Inno Setup is found) compiles `installer.iss`.

Output: `dist\Growth-License-Scrapper-Setup-Windows.exe`.

- The installer is unsigned; Windows SmartScreen may warn → **More info → Run anyway**.
- Upgrades force-close an already-running copy (avoids
  `DeleteFile failed; code 5 / Access is denied`).
- CI: `.github/workflows/build-windows.yml` builds the same installer on
  `windows-latest` for every push to `main` (download the artifact from Actions).

---

## 10. Build a macOS app

```sh
bash build_mac.sh
```

Produces `dist/Growth License Scrapper.app` and (where `hdiutil` is available)
`dist/Growth-License-Scrapper-macOS.dmg`. The app is ad-hoc signed; for distribution
outside your machine, sign and notarize it with an Apple Developer certificate.

---

## 11. HTTP API

| Method | Path | Purpose |
|--------|------|---------|
| GET  | `/` | Dashboard page |
| GET  | `/api/state` | Run state, settings, logs, history |
| POST | `/api/start` | `{start_number, max_records, interval_seconds}` |
| POST | `/api/pause`, `/api/resume`, `/api/stop` | Run control |
| POST | `/api/settings` | `{initial_number, max_records, interval_seconds}` |
| GET  | `/api/export/<run_id>/<filename>` | XLSX/CSV download |

---

## 12. Troubleshooting

**`Chrome's debugging connection succeeded, but Chrome exposed no regular browser context`**
A stale connection was reused after Chrome restarted. The app now drops stale
connections and reconnects. If it persists, quit the app, reopen Chrome (with a
window/tab), re-approve `chrome://inspect/#remote-debugging`, and Start again.

**`The portal did not answer the search request`**
The click did not produce a portal response (SPA hydration or a stuck page). The
app reloads and retries, then marks the number `skipped` and continues. Five
consecutive silent searches pause the run so the session can be checked.

**UAE PASS reCAPTCHA fails**
Disable the **Fake Filler** extension (it fills the login field with dummy data) and
any VPN. Reload the UAE PASS page to get a fresh token, type your real number, tick
the checkbox if shown, and click Login within about a minute. Repeated failures can
be rate-limited; wait 15–30 minutes.

**Installer: `DeleteFile failed; code 5 / Access is denied`**
The app was still running. Current installers force-close it. Otherwise end
**Growth License Scrapper** in Task Manager (or reboot) and click Try again.

**Download has a UUID name / opens as code**
Use a build from `main` (dashboard header shows `build 2026-11-17-2` or newer).
The download URL carries the filename and the server sends a proper
`Content-Disposition`. If it still happens, a browser extension is intercepting
downloads; try Edge or disable extensions for the page.

**Mobile numbers are blank**
The portal itself does not always display a mobile number on the Details page;
blank output reflects the portal.

---

## 13. Repository layout

```
app.py                     Dashboard server, SQLite, runner, Playwright automation, exports
static/index.html          Single-page dashboard (vanilla HTML/CSS/JS)
requirements.txt           playwright, pyinstaller
build_windows.ps1          PyInstaller + Inno Setup build (Windows)
build_mac.sh               PyInstaller + DMG build (macOS)
installer.iss              Inno Setup configuration
.github/workflows/         Windows installer CI
PROJECT.md                 This document
README.md                  Short quick-start
PROJECT_HANDOFF.md         Earlier handoff notes (historical)
```

---

## 14. Known limitations

- The portal returns at most **5 records per search**; the app processes all of them.
- Login and CAPTCHA are manual by design.
- The detail selectors depend on the portal's OutSystems markup; if the portal
  changes, extraction may need updating (diagnostics are saved to `diagnostics/`).
- The 13-digit increment rule is tuned to the `26…8888` series; other long-number
  formats need a rule in `next_license_number()`.
- Builds are unsigned; sign them before distributing broadly.

---

## 15. Authorized use

Use only with explicit authorization for portal access and storage of the requested
personal information. Confirm the portal's terms, permitted purpose, retention
rules and rate limits. Protect the local database and exports.
