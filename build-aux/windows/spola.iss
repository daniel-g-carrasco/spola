; Inno Setup script for Spola, as in p7m-extractor.
; Compiled by the CI with:  ISCC.exe /DAppVersion=X.Y.Z build-aux\windows\spola.iss
; Expects the PyInstaller output in dist\Spola (repository root).
;
; Spola writes its own start at login (the "Spola" value of the user's Run
; key, from Preferences): Setup only removes it when Spola is uninstalled.
; The settings, the listings and the backups (%APPDATA%\Spola,
; %LOCALAPPDATA%\Spola) stay: they are the memory of what is in sync, and a
; reinstall goes on from them.

#ifndef AppVersion
  #define AppVersion "dev"
#endif

[Setup]
AppId={{855C7C84-24AC-4DFB-8645-5A9013036CC2}
AppName=Spola
AppVersion={#AppVersion}
AppPublisher=Daniel Grasso
AppPublisherURL=https://spola.neistar.com
AppSupportURL=https://github.com/daniel-g-carrasco/spola/issues
AppUpdatesURL=https://github.com/daniel-g-carrasco/spola/releases
DefaultDirName={autopf}\Spola
DefaultGroupName=Spola
DisableProgramGroupPage=yes
; per-user install by default (no UAC); the dialog lets the user elevate
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\..\dist
OutputBaseFilename=spola-setup-v{#AppVersion}-windows-x64
SetupIconFile=spola.ico
UninstallDisplayIcon={app}\Spola.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; an update closes the running Spola (it saves bisync's state on the way out)
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "italian"; MessagesFile: "compiler:Languages\Italian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
italian.AppDescription=Sincronizza le tue cartelle con Koofr
english.AppDescription=Keep your folders in sync with Koofr

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; Flags: unchecked

[Files]
Source: "..\..\dist\Spola\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\Spola"; Filename: "{app}\Spola.exe"; Comment: "{cm:AppDescription}"
Name: "{autodesktop}\Spola"; Filename: "{app}\Spola.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Spola.exe"; Description: "{cm:LaunchProgram,Spola}"; Flags: nowait postinstall skipifsilent

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', 'Spola');
end;
