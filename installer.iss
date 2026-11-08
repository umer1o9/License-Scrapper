#define MyAppName "Growth License Scrapper"
#define MyAppVersion "0.1.0"
#define MyAppExeName "Growth License Scrapper.exe"
[Setup]
AppId={{A204F82B-9784-46A8-A741-E3BFF5C287CC}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={autopf}\Growth License Scrapper
DefaultGroupName={#MyAppName}
OutputDir=dist
OutputBaseFilename=Growth-License-Scrapper-Setup-Windows
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
[Files]
Source: "dist\Growth License Scrapper\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: postinstall nowait skipifsilent
