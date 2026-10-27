; СПО «Афина» — ЕДИНАЯ ПРОГРАММА: установщик Windows x64 (NSIS)
; Сборка (кросс, Linux или нативно Windows):
;   makensis -DPAYLOAD=<каталог-стенд> -DVERSION=1.0.0 -DOUT=<путь.exe> afina_unified.nsi
; Состав стенда формирует build_windows_exe.sh (payload = дерево репозитория +
; bundled-рантаймы python/node + запускалки + bundle_manifest.json).
Unicode true
SetCompressor /SOLID lzma

!ifndef VERSION
  !define VERSION "1.0.0"
!endif
!ifndef PAYLOAD
  !define PAYLOAD "build/payload"
!endif
!ifndef OUT
  !define OUT "AfinaUnified-Setup-${VERSION}.exe"
!endif

Name "СПО «Афина» — единая программа ${VERSION}"
OutFile "${OUT}"
InstallDir "$PROGRAMFILES64\AfinaUnified"
InstallDirRegKey HKLM "Software\AfinaUnified" "InstallDir"
RequestExecutionLevel admin
BrandingText "Афина · линия A + линия B + unified · zero-dependency"

Page directory
Page instfiles
UninstPage uninstConfirm
UninstPage instfiles

Section "Единая программа (ядро обязательно)"
  SectionIn RO
  SetOutPath "$INSTDIR"

  ; рантаймы (bundled): python embeddable + node.exe — установка НЕ требует
  ; предварительно установленных Python/Node (автономность, режим Б)
  File /r "${PAYLOAD}/runtimes"

  ; дерево программы (структура репозитория — verify_install работает «как есть»)
  File /r "${PAYLOAD}/afina"
  File /r "${PAYLOAD}/eng6-service"
  File /r "${PAYLOAD}/unified"

  ; корневые файлы запуска и целостности
  File "${PAYLOAD}/run_unified.pyw"
  File "${PAYLOAD}/stop_unified.py"
  File "${PAYLOAD}/bundle_manifest.json"
  File "${PAYLOAD}/Афина.bat"
  File "${PAYLOAD}/Остановить.bat"
  File "${PAYLOAD}/Проверка.bat"
  File "${PAYLOAD}/README_УСТАНОВКА.txt"

  ; данные приложения — в профиле пользователя (Program Files недоступен на запись)
  ExecWait 'cmd /c if not exist "%LOCALAPPDATA%\AfinaUnified" mkdir "%LOCALAPPDATA%\AfinaUnified"'

  ; ярлыки
  CreateDirectory "$SMPROGRAMS\Афина"
  CreateShortCut "$SMPROGRAMS\Афина\Афина — запуск.lnk" "$INSTDIR\Афина.bat" "" "$INSTDIR\Афина.bat" 0
  CreateShortCut "$SMPROGRAMS\Афина\Остановить.lnk" "$INSTDIR\Остановить.bat" "" "$INSTDIR\Остановить.bat" 0
  CreateShortCut "$SMPROGRAMS\Афина\Проверка установки.lnk" "$INSTDIR\Проверка.bat" "" "$INSTDIR\Проверка.bat" 0
  CreateShortCut "$SMPROGRAMS\Афина\Рабочее место (HTML).lnk" "$INSTDIR\unified\ui\workspace.html"
  CreateShortCut "$SMPROGRAMS\Афина\Руководство по установке.lnk" "$INSTDIR\README_УСТАНОВКА.txt"
  CreateShortCut "$SMPROGRAMS\Афина\Удаление.lnk" "$INSTDIR\uninstall.exe"
  CreateShortCut "$DESKTOP\Афина.lnk" "$INSTDIR\Афина.bat" "" "$INSTDIR\Афина.bat" 0

  ; запись в «Установка и удаление программ»
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\AfinaUnified" "DisplayName" "СПО Афина — единая программа"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\AfinaUnified" "DisplayVersion" "${VERSION}"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\AfinaUnified" "Publisher" "Afina Unified"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\AfinaUnified" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegStr HKLM "Software\AfinaUnified" "InstallDir" "$INSTDIR"

  WriteUninstaller "$INSTDIR\uninstall.exe"
SectionEnd

Section "Запустить после установки"
  Exec '"$INSTDIR\Афина.bat"'
SectionEnd

Section Uninstall
  ; остановить процессы, если работают
  ExecWait 'taskkill /F /IM node.exe /FI "WINDOWTITLE eq afina-engine*" 2>nul'
  nsExec::ExecToLog 'cmd /c for /f "tokens=2" %i in (tasklist ^| findstr /i pythonw.exe) do @echo %i'
  Delete "$DESKTOP\Афина.lnk"
  Delete "$SMPROGRAMS\Афина\*.*"
  RMDir "$SMPROGRAMS\Афина"
  RMDir /r "$INSTDIR\runtimes"
  RMDir /r "$INSTDIR\afina"
  RMDir /r "$INSTDIR\eng6-service"
  RMDir /r "$INSTDIR\unified"
  Delete "$INSTDIR\run_unified.pyw"
  Delete "$INSTDIR\stop_unified.py"
  Delete "$INSTDIR\bundle_manifest.json"
  Delete "$INSTDIR\Афина.bat"
  Delete "$INSTDIR\Остановить.bat"
  Delete "$INSTDIR\Проверка.bat"
  Delete "$INSTDIR\README_УСТАНОВКА.txt"
  Delete "$INSTDIR\uninstall.exe"
  RMDir "$INSTDIR"
  DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\AfinaUnified"
  DeleteRegKey HKLM "Software\AfinaUnified"
  ; данные пользователя (БД, журналы) СОЗНАТЕЛЬНО не удаляются без запроса:
  MessageBox MB_YESNO|MB_ICONQUESTION "Удалить также данные (БД, журналы) в %LOCALAPPDATA%\AfinaUnified?" IDNO done
  RMDir /r "$LOCALAPPDATA\AfinaUnified"
  done:
SectionEnd
