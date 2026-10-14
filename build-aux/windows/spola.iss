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
; the AppUserModelID Spola sets for itself: its toasts, its taskbar button
Name: "{group}\Spola"; Filename: "{app}\Spola.exe"; Comment: "{cm:AppDescription}"; AppUserModelID: "io.github.daniel_g_carrasco.spola"
Name: "{autodesktop}\Spola"; Filename: "{app}\Spola.exe"; Tasks: desktopicon; AppUserModelID: "io.github.daniel_g_carrasco.spola"

[Run]
Filename: "{app}\Spola.exe"; Description: "{cm:LaunchProgram,Spola}"; Flags: nowait postinstall skipifsilent

[Code]
const
  WM_CLOSE = $0010;
  TrayWindowClass = 'io.github.daniel_g_carrasco.spola.tray';
  QuitTimeoutMs = 100000;

{ The processes started from Spola's folder (Spola.exe, its gdbus.exe), by
  WMI; with Terminate, ended. Only those: another app's gdbus.exe is left
  alone. }
function AppProcesses(Terminate: Boolean): Integer;
var
  Locator, Service, Items, Item: Variant;
  Dir, Name: String;
  I: Integer;
begin
  Result := 0;
  try
    Dir := AddBackslash(ExpandConstant('{app}'));
    StringChangeEx(Dir, '\', '\\', True);
    Locator := CreateOleObject('WbemScripting.SWbemLocator');
    Service := Locator.ConnectServer('.', 'root\CIMV2');
    Items := Service.ExecQuery('SELECT Name, ProcessId FROM Win32_Process WHERE ExecutablePath LIKE ''' + Dir + '%''');
    Result := Items.Count;
    for I := 0 to Items.Count - 1 do begin
      Item := Items.ItemIndex(I);
      if Terminate then begin
        Name := Item.Name;      { a Variant: Format() does not take it }
        Log('Ending ' + Name);
        Item.Terminate();
      end;
    end;
  except
    Log('WMI: ' + GetExceptionMessage);
  end;
end;

{ A running Spola is asked to quit through the window of its icon, and
  quits gracefully: rclone stopped with its state saved (up to 95 s when it
  has transfers to finish); the window goes when that is done. The Restart
  Manager alone cannot close it: it lives in the notification area, and its
  gdbus.exe has no window. What is left a few seconds after (the process
  ending, gdbus.exe) is ended. }
procedure QuitSpola();
var
  Waited: Integer;
begin
  if FindWindowByClassName(TrayWindowClass) <> 0 then begin
    Log('Spola is running: asking it to quit');
    PostMessage(FindWindowByClassName(TrayWindowClass), WM_CLOSE, 0, 0);
    Waited := 0;
    while (FindWindowByClassName(TrayWindowClass) <> 0) and (Waited < QuitTimeoutMs) do begin
      Sleep(250);
      Waited := Waited + 250;
    end;
    Log(Format('Spola quit after %d ms', [Waited]));
  end;
  Waited := 0;
  while (AppProcesses(False) > 0) and (Waited < 10000) do begin
    Sleep(250);
    Waited := Waited + 250;
  end;
  if AppProcesses(False) > 0 then begin
    AppProcesses(True);
    Sleep(1000);
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  QuitSpola();
  Result := '';
end;

function InitializeUninstall(): Boolean;
begin
  QuitSpola();
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then begin
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', 'Spola');
    { the name and icon of its toasts, written by Spola itself }
    RegDeleteKeyIncludingSubkeys(HKEY_CURRENT_USER, 'Software\Classes\AppUserModelId\io.github.daniel_g_carrasco.spola');
  end;
end;
