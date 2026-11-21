; FolderLink のセットアップ（Inno Setup 6）。
; 作り方: ./Tools/build_exe.sh → ./Tools/build_installer.sh（scripts/package.ps1 でも可）
; 出力: installer/Output/FolderLink_Setup.exe
;
; 方針:
; - 管理者権限は要らない（ユーザーごとのインストール。既定は %LOCALAPPDATA%\Programs\FolderLink）。
; - 同期エンジン Syncthing（MPL-2.0）を改変せずに同梱し、ライセンス文書も一緒に入れる。
; - アンインストール時は、動いている FolderLink に終了を頼み（エンジンも正しく止まる）、
;   それでも残っていれば「インストール先のフォルダにある実行ファイル」から起動したプロセスだけを止める。
;   利用者が別に動かしている Syncthing には触れない。
; - アンインストールしても、利用者のデータ（同期フォルダ）と設定・鍵（%LOCALAPPDATA%\FolderLink）は消さない。

#define AppName "FolderLink"
#define AppVersion "0.1.0"
#define AppPublisher "ururuzawa"
#define AppExe "FolderLink.exe"

[Setup]
AppId={{6F3D7C2A-5B1E-4B8F-9C2D-F01D3A7E4B21}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=Output
OutputBaseFilename=FolderLink_Setup
SetupIconFile=..\build\FolderLink.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
LicenseFile=..\LICENSE
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "japanese"; MessagesFile: "compiler:Languages\Japanese.isl"

[Tasks]
Name: "autostart"; Description: "Windows にサインインしたときに FolderLink を起動する（通知領域で同期を続けます）"; GroupDescription: "起動:"
Name: "desktopicon"; Description: "デスクトップにアイコンを作る"; GroupDescription: "アイコン:"; Flags: unchecked

[Files]
Source: "..\dist\FolderLink\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\{#AppName} のアンインストール"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; アプリの設定画面の「自動起動」と同じ値（core/autostart.py の build_command と同じ形）
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "FolderLink"; ValueData: """{app}\{#AppExe}"" --minimized"; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#AppExe}"; Description: "FolderLink を起動する"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; インストール先に PyInstaller が作るキャッシュだけ消す。利用者のデータ・設定には触れない
Type: filesandordirs; Name: "{app}\_internal\__pycache__"

[Messages]
japanese.UninstalledAll=%1 をアンインストールしました。%n%n同期フォルダの中のファイルと、FolderLink の設定（ユーザーの AppData\Local\FolderLink）は残してあります。不要なら手で削除してください。

[Code]
{ インストール先のフォルダにある実行ファイルから起動したプロセスだけを止める（パスで絞る）。
  利用者が別の場所で動かしている Syncthing などには触れない。 }
procedure StopOwnProcesses(const Dir: String);
var
  Cmd: String;
  Code: Integer;
begin
  Cmd := '-NoProfile -ExecutionPolicy Bypass -Command "' +
    '$d = [IO.Path]::GetFullPath(''' + Dir + ''').TrimEnd(''\'') + ''\''; ' +
    'Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($d, [StringComparison]::OrdinalIgnoreCase) } | ' +
    'ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"';
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Cmd, '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

procedure AskToQuitAndWait(const Dir: String);
var
  Code: Integer;
begin
  if FileExists(Dir + '\{#AppExe}') then
  begin
    Exec(Dir + '\{#AppExe}', '--quit', '', SW_HIDE, ewWaitUntilTerminated, Code);
    Sleep(4000);  { エンジンが正しく止まるのを待つ }
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Dir: String;
begin
  Result := '';
  Dir := ExpandConstant('{app}');
  if DirExists(Dir) then
  begin
    AskToQuitAndWait(Dir);
    StopOwnProcesses(Dir);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    { 動いている FolderLink に、エンジンを止めて終了するよう頼む → 残っていれば止める }
    AskToQuitAndWait(ExpandConstant('{app}'));
    StopOwnProcesses(ExpandConstant('{app}'));
  end;
end;
