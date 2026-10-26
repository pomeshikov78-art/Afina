; Альтернатива NSIS — Inno Setup (тот же бандл)
[Setup]
AppName=СПО Афина NextGen
AppVersion=0.1.0
DefaultDirName={autopf}\AfinaNextGen
OutputBaseFilename=AfinaNextGen-Setup-0.1.0
ArchitecturesInstallIn64BitMode=x64
[Languages]
Name: ru; MessagesFile: compiler:Languages\Russian.isl
[Files]
Source: "..\src\*"; DestDir: "{app}\src"; Flags: recursesubdirs
Source: "..\data\*"; DestDir: "{app}\data"
Source: "run_server.pyw"; DestDir: "{app}"
[Icons]
Name: "{autodesktop}\Афина"; Filename: "{app}\run_server.pyw"
[Run]
Filename: "{app}\run_server.pyw"; Description: "Запустить сервер и клиент"; Flags: postinstall nowait
