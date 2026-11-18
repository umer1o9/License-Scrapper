# Growth License Scrapper

Local dashboard and automation for authorized lookups on the UAE **Growth**
economic-license portal. It attaches to your already-running Chrome, searches
license numbers sequentially, opens every result row's Details page, and saves
each run to SQLite plus an Excel/CSV export.

> Full documentation: **[PROJECT.md](PROJECT.md)**.
> Human login (UAE PASS) and CAPTCHA are never automated — the app waits for you.

## Quick start (from source)

**Windows (PowerShell)**

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

**macOS / Linux**

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python app.py
```

The dashboard opens at <http://127.0.0.1:8765/> (`PORT` overrides;
`OPEN_DASHBOARD=0` skips the automatic tab).

## Chrome setup

In the Chrome profile that has your Growth/UAE PASS session:

1. Open `chrome://inspect/#remote-debugging` and enable it.
2. Approve Chrome's prompt.
3. Leave Chrome running; the app attaches via Chrome's `DevToolsActivePort`.

The app never launches a second Chrome or closes your browser.

## License numbers

The start number is text and the incrementing part is auto-detected:

| Start | Next |
|---|---|
| `DMCC-064581` | `DMCC-064582` … |
| `CN_3645562` | `CN_3645563` … |
| `2202345.01` | `2202346.01` … |
| `2627017048888` | `2627017058888` … |
| `2427975` | `2427976` … |

## Exports

Each run has **XLSX** and **CSV** links on the dashboard. Files are served from
`/api/export/<run_id>/<filename>` and are regenerated from SQLite if a saved
workbook is missing or corrupt.

## Build installers

- **Windows:** run `build_windows.ps1` on Windows (needs Python 3.10+ and
  [Inno Setup 6](https://jrsoftware.org/isdl.php)). Output:
  `dist\Growth-License-Scrapper-Setup-Windows.exe`. CI also builds it via
  `.github/workflows/build-windows.yml`.
- **macOS:** `bash build_mac.sh` → `dist/Growth License Scrapper.app` (+ DMG where
  `hdiutil` is available).

Builds are unsigned; on Windows SmartScreen may ask for **More info → Run anyway**.

## Data

| OS | Path |
|---|---|
| macOS | `~/Library/Application Support/Growth License Scrapper` |
| Windows | `%LOCALAPPDATA%\Growth License Scrapper` |
| Linux | `$XDG_DATA_HOME/growth-license-scrapper` |

`scraper.sqlite3` holds runs/searches/records/logs; exports live in
`exports/YYYY-MM-DD/Run N.xlsx`. Override with `LICENSE_SCRAPPER_DATA`.

## Important

Use only with explicit authorization for portal access and storage of the requested
personal information. Confirm the portal's terms, permitted purpose, retention rules
and rate limits. CAPTCHA and UAE PASS stay under your control.
