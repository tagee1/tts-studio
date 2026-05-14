; installer.iss — Inno Setup script for VoxWild
;
; Prerequisites:
;   1. Run PyInstaller:  pyinstaller app.spec
;      Output must be in:  dist\VoxWild\
;   2. Install Inno Setup 6:  https://jrsoftware.org/isinfo.php
;   3. Compile:  iscc installer.iss
;      Output: installer_output\VoxWild-Setup.exe
;
; Signing (after you have a code signing cert):
;   signtool sign /tr http://timestamp.sectigo.com /td sha256 /fd sha256 ^
;     /a installer_output\VoxWild-Setup.exe

#define MyAppName      "VoxWild"
#define MyAppVersion   "1.3.4"
#define MyAppPublisher "Cookie Studios"
#define MyAppURL       "https://cookiestudios.gumroad.com/l/VoxWildPro"
#define MyAppSupportURL "mailto:cookiestudios.dev@gmail.com"
#define MyAppUpdatesURL "https://github.com/tagee1/VoxWild/releases/latest"
#define MyAppExeName   "VoxWild.exe"
#define MyBuildDir     "dist\VoxWild"

[Setup]
AppId={{B3F2A1C4-7D8E-4F0A-9B2C-5E6D3A1F8C90}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppSupportURL}
AppUpdatesURL={#MyAppUpdatesURL}

; Install to %LOCALAPPDATA%\Programs\VoxWild — user-writable, no UAC required.
; Enables silent in-app patch updates (bypasses SmartScreen on updates since no new .exe is executed).
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes

; Output
OutputDir=installer_output
OutputBaseFilename=VoxWild-Setup
SetupIconFile=icon.ico

; Compression (LZMA2 is best ratio for large binaries)
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes

; Minimum Windows 10
MinVersion=10.0

; Require 64-bit Windows
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; Show a license page during installation
LicenseFile=EULA.rtf

; User-level install — no admin required. This is what Chrome, Discord, VS Code do.
; Allows the app to self-update without UAC prompts.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline

; Uninstall
Uninstallable=yes
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
CreateUninstallRegKey=yes


[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"


[Tasks]
Name: "desktopicon";   Description: "{cm:CreateDesktopIcon}";   GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "startmenuicon"; Description: "Create a Start Menu shortcut"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce


[Files]
; ── Main application (PyInstaller one-dir build) ────────────────────────────
Source: "{#MyBuildDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

; ── Legal docs ──────────────────────────────────────────────────────────────
Source: "CREDITS.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "PRIVACY.txt"; DestDir: "{app}"; Flags: ignoreversion

; ── worker scripts (alongside the exe, found via _res()) ─────────────────────
; Already included above via recursesubdirs since they're in the PyInstaller output.
; chatterbox_worker.py — Natural mode (chatterbox_env / python_embed)
; enhance_worker.py   — AI Enhancement (python_embed)


[Icons]
Name: "{group}\{#MyAppName}";          Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#MyAppName}";    Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon


[Run]
; Launch app after install (optional — user can uncheck)
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent


[UninstallRun]
; Nothing special needed — user data lives in %APPDATA%\TTS Studio (legacy path preserved for upgrades), not here


[Registry]
; File association, version info, etc. — add here if needed


[Code]
// Silently remove any existing installation before installing the new version.
// /VERYSILENT + SW_HIDE means no uninstaller window appears — user only sees
// the new installer, making upgrades feel like a single seamless install.
// Checks both HKCU (user install — current default) and HKLM (legacy Program Files install).
function InitializeSetup(): Boolean;
var
  UninstExe: String;
  ResultCode: Integer;
begin
  Result := True;
  // Try user-level install first (current default).
  if RegQueryStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{B3F2A1C4-7D8E-4F0A-9B2C-5E6D3A1F8C90}_is1',
                         'UninstallString', UninstExe) then
  begin
    Exec(RemoveQuotes(UninstExe), '/VERYSILENT /NORESTART', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end
  // Fall back to admin-level install (legacy Program Files install from v1.1.2 and earlier).
  else if RegQueryStringValue(HKLM, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{B3F2A1C4-7D8E-4F0A-9B2C-5E6D3A1F8C90}_is1',
                              'UninstallString', UninstExe) then
  begin
    Exec(RemoveQuotes(UninstExe), '/VERYSILENT /NORESTART', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end;
end;
