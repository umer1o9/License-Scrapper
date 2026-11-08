#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
python3 -m venv .build-venv
.build-venv/bin/python -m pip install --upgrade pip
.build-venv/bin/python -m pip install -r requirements.txt
.build-venv/bin/pyinstaller \
  --noconfirm --clean --windowed --name "Growth License Scrapper" \
  --add-data "static:static" \
  --collect-all playwright app.py
mkdir -p dist/dmg-root
cp -R "dist/Growth License Scrapper.app" dist/dmg-root/
ln -s /Applications dist/dmg-root/Applications
hdiutil create -volname "Growth License Scrapper" -srcfolder dist/dmg-root -ov -format UDZO "dist/Growth-License-Scrapper-macOS.dmg"
