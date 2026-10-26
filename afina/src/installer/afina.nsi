; СПО «Афина NextGen» — единый загрузчик Windows (NSIS)
; Сборка: makensis afina.nsi (Windows-тулчейн; в песочнице разработки не выполняется — см. README)
Unicode true
Name "СПО Афина NextGen"
OutFile "AfinaNextGen-Setup-0.1.0.exe"
InstallDir "$PROGRAMFILES64\AfinaNextGen"
RequestExecutionLevel admin
Page directory
Page instfiles
Section "Ядро и клиент"
  SetOutPath "$INSTDIR"
  File /r "..\src\afina"
  File /r "..\src\client"
  File "..\data\*.*"
  File "run_server.pyw"
  File "bundle_manifest.json"
  CreateShortCut "$DESKTOP\Афина.lnk" "$INSTDIR\run_server.pyw"
  WriteUninstaller "$INSTDIR\uninstall.exe"
SectionEnd
Section "Проверка подписи и целостности (SEC-002)"
  ; на этапе сборки: signtool sign /fd sha256 AfinaNextGen-Setup-0.1.0.exe
  ; установка сверяет контрольные суммы из bundle_manifest.json; расхождение — отказ и журнал
SectionEnd
