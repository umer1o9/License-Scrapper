# Growth License Scrapper — Project Handoff

Use this document as context when continuing development in another chat. It records the intended product, current implementation, decisions, and unresolved work. Treat the repository code as the source of truth if anything here differs.

## Goal

Build a local, cross-platform (macOS and Windows) desktop utility for authorized research using the UAE Growth National Economic Registry economic-license inquiry portal:

- Portal: <https://www.growth.gov.ae/G2C/InquiryAboutEconomicLicenses>
- Search license numbers sequentially, starting from a user-editable number (initial default `10000`).
- For each license number, portal search may return multiple table rows. Open each row's Details page and extract the requested fields.
- Save each run to a dated Excel file, with sequential run filenames such as `Run 1.xlsx`, `Run 2.xlsx`.
- Provide a local dashboard for starting/stopping runs, selecting the start number, setting max search count and interval, reviewing progress/history/errors, and downloading exports.
- The user controls when a run starts. Human login, UAE PASS approval, and CAPTCHA are required when prompted.

Requested Excel fields:

1. License status
2. License number
3. English name
4. Mobile number
5. Mailbox 1
6. Mailbox 2
7. Manager names in English

## User decisions and interaction requirements

- Search **License Number**, not Economic Register Number (ERN), incrementing by `+1`.
- Starting number must remain editable for every run. The dashboard's automatic next number is a convenience, not a locked field.
- Multiple results for one license number are valid. A non-empty results table means open every relevant Details row and extract records; don't classify results as absent just because a direct one-row assumption failed.
- Human verification is part of the supported flow. Never bypass, solve, or automate CAPTCHA. Wait for the human to complete CAPTCHA/UAE PASS, then detect successful portal navigation and resume.
- Use the user's already-running Chrome process and normal profile/session. Reuse an existing Growth/UAE PASS tab when available; otherwise open a new portal tab inside that same Chrome window. Never launch a separate Chrome process or close Chrome when stopping.
- User asked for an installer for other Mac/Windows laptops, but explicitly deferred installer packaging until development is complete.
- The user has been repeatedly testing and is frustrated by false assurances. Be precise: don't claim a flow is fixed or verified without actually verifying it.

## Current repository layout

- `app.py` — Python local HTTP dashboard server, SQLite persistence, Playwright browser automation, record extraction, and XLSX generation.
- `static/index.html` — Single-page dashboard.
- `requirements.txt` — Playwright and PyInstaller.
- `build_mac.sh`, `build_windows.ps1`, `installer.iss` — installer build scripts/configuration, not yet verified end to end.
- `README.md` — install/use/build notes.
- `.gitignore` — excludes virtual environments, local `data/`, browser bundles, build outputs, and macOS `.DS_Store`.

The project has been initialized as a Git repository on `main`, with `origin` set to `https://github.com/umer1o9/License-Scrapper.git`. The user subsequently reported that the code was pushed. Do not overwrite or force-push remote history; inspect `git status` and remote state before future Git operations.

## Data and privacy

- Runtime data is stored outside the repository in the OS application-data directory. `LICENSE_SCRAPPER_DATA` can override this.
- Local development artifacts under `data/`, exports, virtual environments, and bundled browser builds must stay out of Git.
- UAE PASS phone autofill is optional via local environment variable `UAE_PASS_PHONE`. The previously hardcoded number was removed from source before the initial commit. Do not add personal phone numbers, credentials, cookies, or tokens to source control.
- The portal includes personal contact details. Use only with explicit authorization and follow portal terms, permitted purpose, retention, and access controls.

## Browser attachment design and open issue

The intended approach is to attach Playwright to the existing Chrome instance using Chrome's permission-based remote debugging. `app.py` reads Chrome's `DevToolsActivePort` and calls `connect_over_cdp`. It prefers an existing Growth / UAE PASS page; otherwise, it should create a visible page in the attached regular Chrome context and navigate it to the portal. This is a new tab in the already-running Chrome process, never a separate browser process.

The user later confirmed that the Chrome attachment and visible portal tab worked. The latest saved local run (#45, 2026-11-10) searched four license numbers and found four detail records before it was stopped. Chrome still requires its remote-debugging permission for each browser session. A longer uninterrupted run and Windows installer have not been verified.

Investigate and fix before asking the user to test again:

1. Read the newest run's `runs.error` and `logs` from the app's local SQLite DB using the app's configured `DATA` path. Do not infer from screenshot/status alone.
2. Confirm which Chrome profile has remote debugging enabled and whether `DevToolsActivePort` is written there. Support a reliable configuration path if multiple profiles exist; avoid leaking WebSocket tokens.
3. Confirm `connect_over_cdp` actually connects and enumerate tabs with URL/title. Log useful, non-sensitive diagnostics.
4. If no Growth/UAE PASS tab is present, create a new tab in the already attached normal Chrome context; never start a second Chrome process. If tab creation fails, provide a clear actionable dashboard error. Do not report `awaiting_login` while no visible portal login page exists.
5. Navigate the new same-window tab to the portal and verify the URL/title/login UI actually loaded before switching to awaiting login.
6. On successful UAE PASS redirect to the portal, automatically transition to searching; do not require an unnecessary Resume click after login if the browser confirms success.
7. Test portal login page and logged-in page detection using mock pages or saved HTML where possible. Don't try to solve CAPTCHA.

## Search and extraction caveats

The live portal UI may be dynamic, translated, and may return several results rows per license number. Validate selectors with observed portal HTML / DOM, then:

- Fill the inquiry field with exact license number.
- Trigger Search and wait for loading to finish.
- Distinguish a genuine empty result from a delayed/loading table or portal error.
- Collect all rows whose License Number matches the queried number; the ERN is a different value and must not be substituted.
- Click each row's Details link, capture required fields, and return to results safely.
- Persist search outcome and error per number, increment progress accurately, and checkpoint exports so long runs retain completed work.

Earlier runs missed rows in the results table. The latest saved local run (#45) found four detail records and exported them. The user reported blank mobile-number cells; verify whether the portal supplies those values on the corresponding detail pages. Keep testing multi-row search results and longer runs before claiming broad reliability.

## Run and verify locally

From the repository root:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python app.py
```

Dashboard default: <http://127.0.0.1:8765/>. Use `OPEN_DASHBOARD=0` to avoid the app opening a browser automatically. Runtime `PORT` can override the port. On Windows, activate with `.venv\\Scripts\\Activate.ps1`.

Before testing portal automation, enable Chrome remote debugging in the same Chrome profile at `chrome://inspect/#remote-debugging` and approve Chrome's prompt. The dashboard should make explicit which existing tab it will use and show a visible portal login page before saying it is awaiting human login.

## Next priorities

1. Verify login expiry and re-authentication during a longer run; CAPTCHA and UAE PASS approval remain human steps.
2. Verify multi-row search results and required fields, including whether mobile numbers are actually shown by the portal.
3. Check incremental XLSX saves, dated `Run N.xlsx` exports, and dashboard reporting over a longer run.
4. Complete and verify macOS and Windows installers after development stabilizes.

## Working style for the next developer/chat

Start by inspecting `git status`, the most recent database run error/logs, and the current source before changing anything. Preserve the existing local database and exports. Make a focused fix, verify it with the available evidence, restart the server only after source changes, and report exactly what was verified and what still needs a human portal session.
