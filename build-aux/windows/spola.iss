; Inno Setup script for Spola, as in p7m-extractor.
; Compiled by the CI with:  ISCC.exe /DAppVersion=X.Y.Z build-aux\windows\spola.iss
; Expects the PyInstaller output in dist\Spola (repository root).
;
; Spola writes its own start at login (the "Spola" value of the user's Run
; key, from Preferences): Setup only removes it when Spola is uninstalled.
; The settings, the listings and the backups (%APPDATA%\Spola,
; %LOCALAPPDATA%\Spola) stay: they are the memory of what is in sync, and a
; reinstall goes on from them. So does the Koofr app password in the
; Credential Manager (Koofr shows one only once): Spola's "Sign Out" takes
; it away. Uninstalled by another administrator, the entries written in the
; user's HKCU (start at login, File Explorer's) stay in that user's profile.

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

[InstallDelete]
; the version before's libraries and modules (PyInstaller's _internal) go
; first: one the new version no longer ships must not stay there and be
; loaded. Spola has quit by then (PrepareToInstall); its settings and data
; are not in {app}, the uninstaller is not in _internal
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\..\dist\Spola\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
; the AppUserModelID Spola sets for itself: its toasts, its taskbar button
Name: "{group}\Spola"; Filename: "{app}\Spola.exe"; Comment: "{cm:AppDescription}"; AppUserModelID: "io.github.daniel_g_carrasco.spola"
Name: "{autodesktop}\Spola"; Filename: "{app}\Spola.exe"; Tasks: desktopicon; AppUserModelID: "io.github.daniel_g_carrasco.spola"

[Run]
Filename: "{app}\Spola.exe"; Description: "{cm:LaunchProgram,Spola}"; Flags: nowait postinstall skipifsilent; Check: not SpolaWasRunning
; an update (from Spola's own dialog, /SILENT) starts again the Spola it closed
Filename: "{app}\Spola.exe"; Flags: nowait runasoriginaluser; Check: SpolaWasRunning

[UninstallRun]
; File Explorer's status (the sync roots) and entries, taken out; the folders
; and their files stay as they are
Filename: "{app}\Spola.exe"; Parameters: "forget-windows"; Flags: runhidden waituntilterminated; RunOnceId: "ForgetWindows"

[Code]
const
  WM_CLOSE = $0010;
  TrayWindowClass = 'io.github.daniel_g_carrasco.spola.tray';
  QuitTimeoutMs = 100000;

var
  WasRunning: Boolean;

function GetCurrentProcessId(): Cardinal; external 'GetCurrentProcessId@kernel32.dll stdcall';
function ProcessIdToSessionId(Pid: Cardinal; var Session: Cardinal): Boolean;
  external 'ProcessIdToSessionId@kernel32.dll stdcall';

function SpolaWasRunning(): Boolean;
begin
  Result := WasRunning;
end;

{ A path as a WQL string: backslashes and quotes escaped. }
function WqlString(Text: String): String;
begin
  StringChangeEx(Text, '\', '\\', True);
  StringChangeEx(Text, '''', '\''', True);
  Result := '''' + Text + '''';
end;

{ Spola's own processes (Spola.exe, its gdbus.exe and rclone.exe, by their
  exact paths) in this user's session, by WMI; with Terminate, ended. Never
  anything else: another program in the same folder (a folder typed in by
  hand, C:\Tools), another app's gdbus.exe, another user's Spola. }
function AppProcesses(Terminate: Boolean): Integer;
var
  Locator, Service, Items, Item: Variant;
  App, Name: String;
  Session: Cardinal;
  I: Integer;
begin
  Result := 0;
  try
    if not ProcessIdToSessionId(GetCurrentProcessId(), Session) then
      exit;
    App := AddBackslash(ExpandConstant('{app}'));
    Locator := CreateOleObject('WbemScripting.SWbemLocator');
    Service := Locator.ConnectServer('.', 'root\CIMV2');
    Items := Service.ExecQuery('SELECT Name, ProcessId FROM Win32_Process WHERE SessionId = ' + IntToStr(Session) +
      ' AND (ExecutablePath = ' + WqlString(App + 'Spola.exe') +
      ' OR ExecutablePath = ' + WqlString(App + '_internal\gdbus.exe') +
      ' OR ExecutablePath = ' + WqlString(App + '_internal\rclone.exe') + ')');
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
    WasRunning := True;
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
    { its entries in File Explorer (explorer.py), written by Spola itself }
    RegDeleteKeyIncludingSubkeys(HKEY_CURRENT_USER, 'Software\Classes\*\shell\Spola.OpenOnKoofr');
    RegDeleteKeyIncludingSubkeys(HKEY_CURRENT_USER, 'Software\Classes\*\shell\Spola.CopyKoofrLink');
    RegDeleteKeyIncludingSubkeys(HKEY_CURRENT_USER, 'Software\Classes\Directory\shell\Spola.OpenOnKoofr');
    RegDeleteKeyIncludingSubkeys(HKEY_CURRENT_USER, 'Software\Classes\Directory\shell\Spola.CopyKoofrLink');
    RegDeleteKeyIncludingSubkeys(HKEY_CURRENT_USER, 'Software\Classes\*\shell\Spola.Versions');
    RegDeleteKeyIncludingSubkeys(HKEY_CURRENT_USER, 'Software\Classes\Directory\shell\Spola.Deleted');
  end;
end;
