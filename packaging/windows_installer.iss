; Inno Setup script for the MED17.7.5 Flash Tool desktop app.
;
; Turns the single PyInstaller executable (dist\med17flasher-desktop.exe) into a
; classic Windows installer with Start-menu and (optional) desktop shortcuts.
;
; Build (on Windows, after the PyInstaller build):
;   choco install innosetup -y
;   iscc packaging\windows_installer.iss
; Output: dist\installer\med17flasher-setup.exe
;
; NOTE: Inno Setup resolves relative paths against the directory containing THIS
; script (packaging\), not against the current working directory. Every path
; below therefore goes through {#RepoRoot}, which points at the repo root, so the
; script compiles identically from any CWD.

#define RepoRoot      AddBackslash(SourcePath) + ".."
#define AppName       "MED17.7.5 Flash Tool"
#define AppShortName  "MED17 Flash Tool"
#define AppPublisher  "DME Innovation GmbH"
#define AppExeName    "med17flasher-desktop.exe"
#ifndef AppVersion
  #define AppVersion  "1.0.0"
#endif

[Setup]
AppId={{5B0C6E2A-3D71-4C9F-9E2B-7F1A6C4D0E93}}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\MED17FlashTool
DefaultGroupName={#AppShortName}
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#AppExeName}
OutputDir={#RepoRoot}\dist\installer
OutputBaseFilename=med17flasher-setup
SetupIconFile={#RepoRoot}\packaging\icon.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Per-user install: no UAC prompt, installs under %LOCALAPPDATA%\Programs.
PrivilegesRequired=lowest

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "german";  MessagesFile: "compiler:Languages\German.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#RepoRoot}\dist\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#RepoRoot}\packaging\icon.ico";  DestDir: "{app}"; Flags: ignoreversion
Source: "{#RepoRoot}\README.md";           DestDir: "{app}"; Flags: ignoreversion
Source: "{#RepoRoot}\docs\SAFETY.md";      DestDir: "{app}\docs"; Flags: ignoreversion
Source: "{#RepoRoot}\docs\INSTALL.md";     DestDir: "{app}\docs"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppShortName}"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\icon.ico"
Name: "{group}\{cm:UninstallProgram,{#AppShortName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppShortName}"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\icon.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#AppShortName}}"; Flags: nowait postinstall skipifsilent
