# Growth License Scrapper

A supervised desktop utility for authorized searches on the Growth economic-license portal. The packaged app runs from a normal app shortcut, opens its local dashboard automatically, and stores its database and Excel exports in the operating system's application-data folder.

## Daily use

Install the macOS DMG or Windows setup executable, then launch **Growth License Scrapper** from Applications/Start Menu/Desktop. The dashboard opens automatically in your default browser. The scraper uses your already-running Chrome and will not open a new browser window or tab. Before a run, enable Chrome remote debugging for that browser session at `chrome://inspect/#remote-debugging` and approve Chrome's prompt. In the same Chrome window, keep an existing Growth portal tab open (or use an existing blank tab). Click **Start** when ready. Complete UAE PASS or CAPTCHA in that tab and press **Resume / I finished login** in the dashboard. The app does not solve or bypass challenges. Stop and closing the app leave Chrome open; closing the app does not remove saved history or exports.

## Build distributable installers

Builds must run on the operating system being packaged (macOS builds on macOS; Windows builds on Windows). This bundles Python, Playwright and the dashboard so recipient laptops do not need Python or terminal commands. Google Chrome must be installed on each computer, and remote debugging must be approved for each Chrome session that will be automated.

### macOS DMG

Install Python 3.10+ and Xcode command-line tools. From Terminal, run:

```sh
bash build_mac.sh
```

The installer is `dist/Growth-License-Scrapper-macOS.dmg`. For broad distribution, sign and notarize the app with an Apple Developer certificate.

### Windows installer

On Windows, install Python 3.10+, then run `build_windows.ps1` in PowerShell. Install Inno Setup and compile `installer.iss` to produce `dist/Growth-License-Scrapper-Setup-Windows.exe`.

## Developer run

```sh
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

For browser reuse, enable Chrome remote debugging at `chrome://inspect/#remote-debugging`, approve the prompt, and use an existing Growth portal or blank tab. Chrome's permission-based connection does not expose the legacy `/json/version` HTTP API; the app reads Chrome's `DevToolsActivePort` profile file and attaches to its approved WebSocket. If Chrome uses a custom profile directory, set `CHROME_USER_DATA_DIR` to that directory before launching the app. It will not create or commandeer tabs.

UAE PASS phone autofill is optional. Set `UAE_PASS_PHONE` in your local environment if you want it enabled; do not commit personal phone numbers or credentials.

Data lives at `~/Library/Application Support/Growth License Scrapper` on macOS, `%LOCALAPPDATA%\\Growth License Scrapper` on Windows, or the XDG data folder on Linux. Exports are under `exports/YYYY-MM-DD/Run N.xlsx`. To back up or transfer history, close the app and copy that data folder.

## Important

This project requires authorization for portal access and storage of requested personal information. Portal UI selectors and field extraction need validation with an authorized account before extended runs. Confirm the portal's terms, permitted purposes, retention rules and rate limits before collection.
