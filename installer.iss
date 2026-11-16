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
; Upgrades fail with "DeleteFile failed; code 5" when the old app is running.
CloseApplications=force
RestartApplications=no

[Files]
Source: "dist\Growth License Scrapper\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion restartreplace

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: postinstall nowait skipifsilent

[Code]
procedure KillRunningApp();
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM "{#MyAppExeName}"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(1500);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  // The app has no main window, so Inno's Restart Manager can miss it.
  // Force-close it so the old executable can be replaced.
  KillRunningApp();
  Result := '';
end;
