#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

python3 -m venv .build-venv
.build-venv/bin/python -m pip install --upgrade pip
.build-venv/bin/python -m pip install -r requirements.txt

.build-venv/bin/pyinstaller \
  --noconfirm --clean --windowed --name "Growth License Scrapper" \
  --add-data "static:static" \
  --collect-all playwright \
  --hidden-import playwright.sync_api \
  --hidden-import greenlet \
  app.py

# Build a clean DMG staging folder with the app and an /Applications shortcut.
STAGE="$PWD/dist/dmg-root"
if [ -d "$STAGE" ]; then rm -rf "$STAGE"; fi
mkdir -p "$STAGE"
cp -R "dist/Growth License Scrapper.app" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "Growth License Scrapper" -srcfolder "$STAGE" -ov -format UDZO "dist/Growth-License-Scrapper-macOS.dmg"

echo "Installer: dist/Growth-License-Scrapper-macOS.dmg"
