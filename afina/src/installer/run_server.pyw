#!/usr/bin/env python3
"""Единая точка запуска (режим Б, одноместовая поставка): сервер + открытие клиента.

Порядок: локальная проверка целостности бандла → запуск API → браузер на http://127.0.0.1:8470/
Внешних обращений нет (AUT-002/008). Остановка — закрытие окна/CTRL+C.
"""
import hashlib, json, os, sys, threading, time, webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "src"))

def verify_bundle():
    man = os.path.join(HERE, "bundle_manifest.json")
    if not os.path.exists(man):
        return True, "манифест не найден — пропуск (сборка dev)"
    bad = []
    for rel, h in json.load(open(man, encoding="utf-8"))["files"].items():
        p = os.path.join(HERE, rel)
        if not os.path.exists(p) or hashlib.sha256(open(p, "rb").read()).hexdigest() != h:
            bad.append(rel)
    return (not bad), ("нарушена целостность: " + ", ".join(bad[:5]) if bad else "целостность подтверждена")

ok, msg = verify_bundle()
print("[Афина]", msg)
if not ok:
    print("Установка отклонена (SEC-002). Обратитесь к администратору."); sys.exit(2)

from afina.server.app import create_server
from afina.catalog import seed
httpd, app = create_server(db_path=os.path.join(HERE, "data", "afina.db"), host="127.0.0.1", port=8470,
                           requirements_path=os.path.join(HERE, "data", "Афина_Реестр_требований_ПриложениеА.json"),
                           tz_path=os.path.join(HERE, "Афина_Техническое_задание.md"))
seed.build(app.store)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
time.sleep(0.5)
webbrowser.open("http://127.0.0.1:8470/")
print("[Афина] API: http://127.0.0.1:8470/api/v1 — остановка: Ctrl+C")
try:
    while True: time.sleep(3600)
except KeyboardInterrupt:
    httpd.shutdown()
