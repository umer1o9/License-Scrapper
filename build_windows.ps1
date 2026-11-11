$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

Write-Host '=== Growth License Scrapper: Windows build ==='

# 1. Create an isolated build environment.
if (Get-Command py -ErrorAction SilentlyContinue) {
    py -3 -m venv .build-venv
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    python -m venv .build-venv
} else {
    throw 'Python 3.10+ is required. Install it from https://python.org and run this script again.'
}

& .\.build-venv\Scripts\python.exe -m pip install --upgrade pip
& .\.build-venv\Scripts\python.exe -m pip install -r requirements.txt

# 2. Package the app with PyInstaller.
& .\.build-venv\Scripts\pyinstaller.exe --noconfirm --clean --windowed `
    --name "Growth License Scrapper" `
    --add-data "static;static" `
    --collect-all playwright `
    --hidden-import playwright.sync_api `
    --hidden-import greenlet `
    app.py

# 3. Build the installer when Inno Setup 6 is available.
$iscc = @(
    "$env:ProgramFiles(x86)\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1

if (-not $iscc) {
    Write-Host ''
    Write-Host 'App packaged in: dist\Growth License Scrapper'
    Write-Host 'Inno Setup 6 was not found, so the Setup .exe was not created.'
    Write-Host 'Install it from https://jrsoftware.org/isdl.php and run: ISCC.exe installer.iss'
    exit 0
}

& $iscc installer.iss

Write-Host ''
Write-Host 'Done: dist\Growth-License-Scrapper-Setup-Windows.exe'
