; Inno Setup script - builds the Windows installer around the PyInstaller output.
; Built by the GitHub Actions workflow: ISCC /DAppVersion=1.2.3 installer.iss
; Expects the PyInstaller folder at dist\TTSReader\ (containing TTSReader.exe).

#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif

[Setup]
; Never change this GUID: Windows uses it to recognise upgrades of the same app.
AppId={{1327D44F-1AD9-4B5B-A671-6E55F6A0CEFE}
AppName=TTS Reader
AppVersion={#AppVersion}
DefaultDirName={autopf}\TTS Reader
DefaultGroupName=TTS Reader
DisableProgramGroupPage=yes
; Per-user install: no administrator prompt. Installs to %LOCALAPPDATA%\Programs\TTS Reader.
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=TTSReader-Setup-{#AppVersion}
SetupIconFile=app.ico
UninstallDisplayIcon={app}\TTSReader.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Close a running copy during upgrade/uninstall so files can be replaced.
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; Flags: unchecked

[Files]
Source: "dist\TTSReader\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\TTS Reader"; Filename: "{app}\TTSReader.exe"
Name: "{autodesktop}\TTS Reader"; Filename: "{app}\TTSReader.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\TTSReader.exe"; Description: "Launch TTS Reader now"; Flags: nowait postinstall skipifsilent
