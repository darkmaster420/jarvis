#define MyAppName "Jarvis"
#define MyAppVersion "0.2.21"
#define MyAppPublisher "Jarvis"
#define MyAppExeName "Jarvis.exe"

[Setup]
AppId={{9BCB2EA0-DA49-4F82-B545-18DF0F78A5B7}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
; Installer EXE and ARP (Apps & features) use this 4-part version
VersionInfoVersion=0.2.21.0
DefaultDirName={autopf}\Jarvis
DefaultGroupName=Jarvis
DisableProgramGroupPage=yes
OutputDir=..\dist\installer
OutputBaseFilename=JarvisSetup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
PrivilegesRequired=admin
SetupLogging=yes
VersionInfoDescription={#MyAppName} Windows installer
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion=0.2.21.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
; User overrides live in %LocalAppData%\Jarvis; do not ship runtime logs or old single-file state from dist.
Source: "..\dist\Jarvis\*"; DestDir: "{app}"; Excludes: "backend\.venv\*,*\__pycache__\*,*.pyc,*.pyo,backend.log,setup_runtime.log,state.json,hud_layout.json,config.yaml"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Jarvis"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{autodesktop}\Jarvis"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\DirectX\UserGpuPreferences"; ValueType: string; ValueName: "{app}\{#MyAppExeName}"; ValueData: "GpuPreference=2;"; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Microsoft\DirectX\UserGpuPreferences"; ValueType: string; ValueName: "{localappdata}\Programs\Ollama\ollama app.exe"; ValueData: "GpuPreference=2;"
Root: HKCU; Subkey: "Software\Microsoft\DirectX\UserGpuPreferences"; ValueType: string; ValueName: "{localappdata}\Programs\Ollama\ollama.exe"; ValueData: "GpuPreference=2;"

[Run]
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\_install\setup_runtime.ps1"" -AppRoot ""{app}"""; StatusMsg: "Installing Jarvis backend runtime..."; Flags: waituntilterminated
Filename: "{app}\{#MyAppExeName}"; Description: "Launch Jarvis"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: files; Name: "{app}\backend.log"
Type: files; Name: "{app}\setup_runtime.log"
Type: filesandordirs; Name: "{app}\backend\.venv"

[Code]
function InitializeSetup(): Boolean;
begin
  Result := True;
end;
