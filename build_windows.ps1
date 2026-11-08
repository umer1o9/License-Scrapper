$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
py -3 -m venv .build-venv
& .\.build-venv\Scripts\python.exe -m pip install --upgrade pip
& .\.build-venv\Scripts\python.exe -m pip install -r requirements.txt
& .\.build-venv\Scripts\pyinstaller.exe --noconfirm --clean --windowed --name "Growth License Scrapper" --add-data "static;static" --collect-all playwright app.py
Write-Host "Packaged app is in dist\Growth License Scrapper. Create the Windows installer with Inno Setup using installer.iss."
