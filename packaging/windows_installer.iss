; Inno Setup script for the DME Innovation MED17 Flasher desktop app.
;
; Turns the single PyInstaller executable (dist\med17flasher-desktop.exe) into a
; classic Windows installer with Start-menu and (optional) desktop shortcuts,
; and installs the bundled Tactrix / J2534 driver so the Openport 2.0 works
; right after setup with no separate download.
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
; AppName is the full product name (wizard title, Add/Remove Programs).
; AppShortName names the shortcut inside the publisher's Start-menu folder, so
; it does not repeat the company: Start menu -> DME Innovation -> MED17 Flasher.
#define AppName       "DME Innovation MED17 Flasher"
#define AppShortName  "MED17 Flasher"
#define AppPublisher  "DME Innovation GmbH"
#define AppGroup      "DME Innovation"
#define AppExeName    "med17flasher-desktop.exe"
#ifndef AppVersion
  #define AppVersion  "1.0.0"
#endif

; Detect at COMPILE time whether a real driver installer was dropped into
; packaging\drivers\ (an .exe or .msi, not just the README). If so, define
; HaveDriver, which gates the driver Task + Files below. Doing this at compile
; time is essential: the end-user machine has no repo to look at, so a runtime
; check against {#RepoRoot} would be meaningless.
#define DrvExe FindFirst(RepoRoot + "\packaging\drivers\*.exe", 0)
#if DrvExe
  #define HaveDriver
  #expr FindClose(DrvExe)
#endif
#ifndef HaveDriver
  #define DrvMsi FindFirst(RepoRoot + "\packaging\drivers\*.msi", 0)
  #if DrvMsi
    #define HaveDriver
    #expr FindClose(DrvMsi)
  #endif
#endif

[Setup]
AppId={{5B0C6E2A-3D71-4C9F-9E2B-7F1A6C4D0E93}}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\DME Innovation\MED17 Flasher
DefaultGroupName={#AppGroup}
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#AppExeName}
OutputDir={#RepoRoot}\dist\installer
OutputBaseFilename=med17flasher-setup
SetupIconFile={#RepoRoot}\packaging\icon.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Per-user install: no UAC prompt, installs under %LOCALAPPDATA%\Programs.
; The bundled driver installer requests its own elevation when it runs (a
; kernel-mode USB/J2534 driver needs admin), so setup itself stays lowest.
PrivilegesRequired=lowest

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "german";  MessagesFile: "compiler:Languages\German.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
#ifdef HaveDriver
; Only present when a driver installer was actually bundled at build time;
; ticked by default so a first-time user gets a working Tactrix.
Name: "installdriver"; Description: "{cm:InstallTactrixDriver}"
#endif

[CustomMessages]
english.InstallTactrixDriver=Install the Tactrix / J2534 driver (needed for the Openport 2.0)
german.InstallTactrixDriver=Tactrix-/J2534-Treiber installieren (fuer den Openport 2.0 noetig)
english.InstallingDriver=Installing the Tactrix / J2534 driver...
german.InstallingDriver=Tactrix-/J2534-Treiber wird installiert...

[Files]
Source: "{#RepoRoot}\dist\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#RepoRoot}\packaging\icon.ico";  DestDir: "{app}"; Flags: ignoreversion
Source: "{#RepoRoot}\README.md";           DestDir: "{app}"; Flags: ignoreversion
Source: "{#RepoRoot}\docs\SAFETY.md";      DestDir: "{app}\docs"; Flags: ignoreversion
Source: "{#RepoRoot}\docs\INSTALL.md";     DestDir: "{app}\docs"; Flags: ignoreversion
#ifdef HaveDriver
; Driver payload: everything the operator dropped into packaging\drivers\ is
; copied to a temp folder and run during setup, then removed. recursesubdirs so
; a driver shipped as a folder works too. This whole line is compiled in only
; when a real .exe/.msi is present (HaveDriver), so a build with no driver
; committed does not fail with "No files found matching".
Source: "{#RepoRoot}\packaging\drivers\*"; DestDir: "{tmp}\drivers"; \
    Flags: recursesubdirs deleteafterinstall; Tasks: installdriver
#endif

[Icons]
Name: "{group}\{#AppShortName}"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\icon.ico"
Name: "{group}\{cm:UninstallProgram,{#AppShortName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppShortName}"; Filename: "{app}\{#AppExeName}"; IconFilename: "{app}\icon.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#AppShortName}}"; Flags: nowait postinstall skipifsilent

[Code]
{ ---- Bundled Tactrix / J2534 driver auto-install ------------------------- }
{
  The operator drops the official Tactrix driver installer into
  packaging\drivers\ before building (any .exe or .msi). Setup copies that
  folder to {tmp}\drivers and, after the app files are in place, runs each
  installer silently. Silent flags default to "/S" (the Tactrix/NSIS and FTDI
  CDM installers use it); to override for a different packer, drop a plain-text
  file "_silent_args.txt" next to the installer whose contents are passed
  verbatim to every .exe instead.
}

{ Read optional per-build silent-flag override; default to "/S". }
function SilentArgs(): String;
var
  Lines: TArrayOfString;
  ArgsFile: String;
begin
  Result := '/S';
  ArgsFile := AddBackslash(ExpandConstant('{tmp}\drivers')) + '_silent_args.txt';
  if FileExists(ArgsFile) then
    if LoadStringsFromFile(ArgsFile, Lines) then
      if GetArrayLength(Lines) > 0 then
        Result := Trim(Lines[0]);
end;

procedure RunBundledDrivers();
var
  Rec: TFindRec;
  Dir: String;
  Ext: String;
  ResultCode: Integer;
begin
  Dir := ExpandConstant('{tmp}\drivers');
  if not DirExists(Dir) then
    Exit;
  if FindFirst(AddBackslash(Dir) + '*', Rec) then
  begin
    try
      repeat
        if (Rec.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0 then
          Continue;
        Ext := Lowercase(ExtractFileExt(Rec.Name));
        if Ext = '.exe' then
        begin
          WizardForm.StatusLabel.Caption := ExpandConstant('{cm:InstallingDriver}');
          Exec(AddBackslash(Dir) + Rec.Name, SilentArgs(), '',
               SW_SHOW, ewWaitUntilTerminated, ResultCode);
        end
        else if Ext = '.msi' then
        begin
          WizardForm.StatusLabel.Caption := ExpandConstant('{cm:InstallingDriver}');
          Exec(ExpandConstant('{sys}\msiexec.exe'),
               '/i "' + AddBackslash(Dir) + Rec.Name + '" /qn /norestart', '',
               SW_SHOW, ewWaitUntilTerminated, ResultCode);
        end;
      until not FindNext(Rec);
    finally
      FindClose(Rec);
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    if WizardIsTaskSelected('installdriver') then
      RunBundledDrivers();
end;
