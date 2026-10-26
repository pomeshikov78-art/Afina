# Единый загрузчик и сборка установочных пакетов

## Статус (честно)
- **Windows .exe**: скрипты NSIS (`afina.nsi`) и Inno Setup (`afina.iss`) ГОТОВЫ, но собираются
  только в Windows-тулчейне (makensis / ISCC). В среде разработки (Linux-песочница) бинарный .exe
  не собирался — это операция сборочного стенда (G2, WP-сборка).
- **Linux**: `build_linux.sh` собирает офлайн-бандл tar.xz с манифестом целостности (работает здесь).
- **Проверка целостности при запуске**: `run_server.pyw` сверяет бандл с `bundle_manifest.json`;
  подмена → отказ запуска + сообщение (SEC-002, AUT-T07).
- **Подпись**: signtool (Windows) / gpg (Linux) — на сборочном стенде; процедура §8.3.

## Состав бандла (офлайн, AUT-002)
```
AfinaNextGen/
├── src/afina/        # ядро: eng6, gis, server, ai, catalog
├── src/client/       # веб-клиент (раздаётся сервером)
├── data/             # БД (создаётся), Приложение А, словари
├── run_server.pyw    # единая точка запуска
└── bundle_manifest.json
```

## Запуск без установки (любая ОС с Python 3.11+)
```
cd src && python3 -m afina.server.app --db ../data/afina.db --port 8470 \
  --requirements ../data/Афина_Реестр_требований_ПриложениеА.json
# клиент: http://127.0.0.1:8470/
```
