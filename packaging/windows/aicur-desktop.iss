; Per-user installer for the ai-cur desktop client (Inno Setup 6). No admin rights needed.
;
;   iscc /DAppVersion=0.2.0 /DTrayDir=%CD%\dist\win\aicur-tray ^
;        /DBackendDir=%CD%\dist\win\aicur-backend /DOutputDir=%CD%\dist packaging\windows\aicur-desktop.iss
;
; Pass ABSOLUTE paths: ISCC resolves relative Source and OutputDir paths against this file's
; folder, not the current directory.
;
; /DNoBackend builds the negative-control installer for CI: same tray, no backend folder.
; The smoke test must fail on it with "no backend health".

#ifndef AppVersion
  #error Pass /DAppVersion=<version>
#endif
#ifndef TrayDir
  #error Pass /DTrayDir=<PyInstaller aicur-tray folder>
#endif
#ifndef NoBackend
  #ifndef BackendDir
    #error Pass /DBackendDir=<PyInstaller aicur-backend folder>
  #endif
#endif
#ifndef OutputDir
  #define OutputDir "dist"
#endif
#ifdef NoBackend
  #define OutputName "ai-cur-desktop-setup-" + AppVersion + "-nobackend"
#else
  #define OutputName "ai-cur-desktop-setup-" + AppVersion
#endif

#define AppName "ai-cur desktop client"
#define TrayExe "aicur-tray.exe"

[Setup]
AppId={{6E0C1C0B-4F55-4E0B-9B7C-2D6B1A3F9E41}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Harvto
AppPublisherURL=https://github.com/harvto-llc/ai-usage-tracker
DefaultDirName={localappdata}\Programs\{#AppName}
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
OutputDir={#OutputDir}
OutputBaseFilename={#OutputName}
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#TrayExe}
; The installer and uninstaller stop a running tray themselves (see [Code]).
CloseApplications=no
WizardStyle=modern

[Files]
Source: "{#TrayDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
#ifndef NoBackend
Source: "{#BackendDir}\*"; DestDir: "{app}\backend"; Flags: recursesubdirs createallsubdirs ignoreversion
#endif

[Icons]
Name: "{userprograms}\{#AppName}"; Filename: "{app}\{#TrayExe}"

[Registry]
; Start with Windows. The tray's "Start at login" item edits this same value.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; \
  ValueName: "{#AppName}"; ValueData: """{app}\{#TrayExe}"""; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#TrayExe}"; Description: "Start {#AppName} now"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
// Stop the tray (which stops the backend), then make sure nothing from {app} is left running,
// so the files can be replaced or deleted. ~/.usage-tracker (data and config) is left alone.
procedure StopRunningCopies(AppDir: String);
var
  ResultCode: Integer;
begin
  if FileExists(AppDir + '\{#TrayExe}') then
    Exec(AppDir + '\{#TrayExe}', '--quit', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(3000);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM {#TrayExe}', '', SW_HIDE,
       ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM aicur-backend.exe', '', SW_HIDE,
       ewWaitUntilTerminated, ResultCode);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopRunningCopies(ExpandConstant('{app}'));
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    StopRunningCopies(ExpandConstant('{app}'));
end;
