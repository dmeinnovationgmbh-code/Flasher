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
; The compiler is invoked from the repository root, so all paths are relative to
; that root.

#define AppName       "MED17.7.5 Flash Tool"
#define AppShortName  "MED17 Flash Tool"
#define AppPublisher  "DME Innovation GmbH"
#define AppExeName    "med17flasher-desktop.exe"
#ifndef AppVersion
  #define AppVersion  "1.0.0"
#endif

[Setup]
AppId={{5B0C6E2A-3D71-4C9F-9E2B-MED1775FLASH}}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\MED17FlashTool
DefaultGroupName={#AppShortName}
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#AppExeName}
OutputDir=dist\installer
OutputBaseFilename=med17flasher-setup
SetupIconFile=packaging\icon.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
; Per-user install by default so no admin prompt is needed.
PrivilegesRequiredOverridesAllowed=dialog

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "german";  MessagesFile: "compiler:Languages\German.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "dist\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "packaging\icon.ico";  DestDir: "{app}"; Flags: ignoreversion
Source: "README.md";           DestDir: "{app}"; Flags: ignoreversion isreadme
Source: "docs\SAFETY.md";      DestDir: "{app}\docs"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppShortName}"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\icon.ico"
Name: "{group}\{cm:UninstallProgram,{#AppShortName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppShortName}"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\icon.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#AppShortName}}"; Flags: nowait postinstall skipifsilent
