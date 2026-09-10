; Inno Setup script for Ticker.
;
;   ISCC.exe /DAppVersion=1.2.3 packaging\windows\ticker.iss
;
; build.py --installer does this for you. It wraps the onedir folder from
; dist\Ticker, so run the PyInstaller build first.
;
; Per-user install by default (PrivilegesRequired=lowest): no UAC prompt, no
; admin rights, and the app only ever writes to %LOCALAPPDATA% anyway. That
; also means an unprivileged user can install it without asking anyone,
; which is most of the point of shipping an installer at all.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

#define AppName "Ticker"
#define AppPublisher "Ticker"
#define AppExeName "Ticker.exe"
#define SourceDir "..\..\dist\Ticker"

[Setup]
AppId={{8E3B2A14-6F5D-4C7E-9A21-Ticker000001}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; Per-user by default. Users who want it machine-wide can elevate; Inno
; switches {autopf} and {autoprograms} to the machine locations for them.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\..\dist
OutputBaseFilename=Ticker-{#AppVersion}-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; The uninstaller ends up in Add/Remove Programs, which is a large part of
; why an installer beats a zip for anyone who isn't a developer.
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExeName}
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; \
    GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
; The whole onedir tree. recursesubdirs picks up _internal, which is where
; PyInstaller puts the Python runtime, bleak, and the migrations.
Source: "{#SourceDir}\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; \
    Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; \
    Description: "Launch {#AppName}"; \
    Flags: nowait postinstall skipifsilent

[UninstallDelete]
; PyInstaller's folder is removed by the uninstaller, but Python leaves
; __pycache__ directories behind that it doesn't know about.
Type: filesandordirs; Name: "{app}\_internal\__pycache__"

; Deliberately NOT deleted on uninstall: %LOCALAPPDATA%\Ticker, which holds
; the database. Removing the program should not remove years of the user's
; heart rate history -- they can delete that folder themselves if they mean
; to.
