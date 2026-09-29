; Inno Setup script for the BlueShelf installer. Run build.ps1 rather than compiling this directly.
#define AppName "BlueShelf"
#define AppVersion "0.1.0"

[Setup]
AppId={{8B7E2C1A-4F2D-4E7B-9C1E-5A3D2F6B7C80}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppName}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=BlueShelfSetup-{#AppVersion}
SetupIconFile=build\blueshelf.ico
UninstallDisplayIcon={app}\BlueShelf.exe
Compression=lzma2/ultra64
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

; Upgrading from the old name: its executable and shortcuts would otherwise be left behind.
[InstallDelete]
Type: files; Name: "{app}\Vault.exe"
Type: files; Name: "{group}\Vault.lnk"
Type: files; Name: "{autodesktop}\Vault.lnk"

[Files]
Source: "dist\BlueShelf\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\BlueShelf.exe"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\BlueShelf.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\BlueShelf.exe"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
