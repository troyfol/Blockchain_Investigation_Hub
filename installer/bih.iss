; bih.iss — Inno Setup script for the Blockchain Investigation Hub one-folder app (P9).
;
;   make installer    ->  scripts/installer.py runs ISCC.exe on this script, bundling dist/BIH/ into a
;                         single UNSIGNED  dist/installer/BIH-Setup-<ver>.exe
;
; Confirm-first (CLAUDE.md §6): authored against Inno Setup 6.7.3 (the version `make installer` installs via
; winget if missing). The volatile values (version, the dist source dir, the output dir, the icon path) are
; passed in by installer.py as ISCC /D defines; each is #ifndef-guarded below so running ISCC on this file
; directly still works with sane repo-relative fallbacks.
;
; Design intent:
;   * Per-user OR Program Files: PrivilegesRequired=lowest + ...OverridesAllowed=dialog lets the user pick
;     "just me" (no admin -> {localappdata}\Programs) or "all users" (admin -> Program Files).
;   * Start-Menu + Desktop shortcuts use 8.ico (shipped into {app} and referenced explicitly).
;   * A clean uninstaller is auto-registered in Programs-and-Features (keyed on the STABLE AppId GUID).
;   * UNINSTALL NEVER TOUCHES %APPDATA%\BlockchainInvestigationHub. The installer writes nothing there, and
;     there is deliberately no uninstall-delete directive for it — the user's cases/registry/settings
;     survive an uninstall (and a reinstall). See the closing note at the bottom of this file.

#ifndef MyAppVersion
  #define MyAppVersion "1.3.1"
#endif
; Absolute path to the built one-folder app (dist/BIH). Fallback assumes ISCC is run from installer/.
#ifndef MySourceDir
  #define MySourceDir "..\dist\BIH"
#endif
; Where the produced setup.exe is written.
#ifndef MyOutputDir
  #define MyOutputDir "..\dist\installer"
#endif
; The app/shortcut icon (repo-root 8.ico).
#ifndef MyIconFile
  #define MyIconFile "..\8.ico"
#endif

#define MyAppName "Blockchain Investigation Hub"
#define MyAppExeName "BIH.exe"
#define MyAppPublisher "Blockchain Investigation Hub Project"
; The %APPDATA% data-dir name (== backend/app/app_paths.APP_NAME). Documented here; never deleted on uninstall.
#define MyAppDataDir "BlockchainInvestigationHub"

[Setup]
; STABLE identity GUID — keyed for upgrade/uninstall. NEVER change it (matches app_metadata.INSTALLER_APP_GUID).
; The leading {{ is an escaped literal "{" — the actual AppId is {7B1C0DE5-...}.
AppId={{7B1C0DE5-9A2B-4C3D-8E5F-0A1B2C3D4E5F}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; Per-user by default (no admin); the dialog lets the user escalate to an all-users Program Files install.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
; 64-bit app (Python 3.13 x64) — install into the 64-bit Program Files when all-users.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#MyOutputDir}
OutputBaseFilename=BIH-Setup-{#MyAppVersion}
SetupIconFile={#MyIconFile}
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
; This installer is UNSIGNED by default (no cert required to build). See README "Distribution" + make sign.

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; The whole one-folder PyInstaller app (BIH.exe + _internal/...).
Source: "{#MySourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
; Ship 8.ico into {app} so the shortcuts reference it explicitly (the exe also carries it embedded).
Source: "{#MyIconFile}"; DestDir: "{app}"; DestName: "8.ico"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\8.ico"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\8.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

; NOTE (uninstall data preservation): there is intentionally no uninstall-delete section. The app writes
; all user data (cases, the case registry, settings.json, logs) to %APPDATA%\{#MyAppDataDir}, which this
; installer never creates and the uninstaller therefore never removes. Uninstalling — or reinstalling — the
; app leaves every case intact. Removing user data is a manual, deliberate user action, never the uninstaller's.
