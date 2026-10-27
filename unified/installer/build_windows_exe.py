#!/usr/bin/env python3
"""unified/installer/build_windows_exe.py — сборка установщика Windows x64 (кросс-сборка).

Этапы:
  1. стенд (payload/): дерево программы (структура репозитория — afina/, eng6-service/,
     unified/) БЕЗ мусора (node_modules, var, кэш, протоколы), запускалки (.bat),
     README_УСТАНОВКА.txt, единый загрузчик run_unified.pyw;
  2. рантаймы (self-contained): python-3.11.9-embed-amd64 + node.exe v20 (win-x64)
     скачиваются с официальных дистрибутивов (кэш в ~/.cache/afina-installer),
     извлекаются в payload/runtimes/ — установка НЕ требует Python/Node в системе;
  3. bundle_manifest.json — sha256 КАЖДОГО файла стенда (SEC-002: загрузчик сверяет
     при каждом старте, подмена → отказ);
  4. makensis (NSIS ≥ 3, работает и на Linux) → dist/AfinaUnified-Setup-<ver>.exe
     + sha256 установщика в dist/MANIFEST.txt.

Честные ограничения:
  * исполнение .exe проверяется только на Windows-стенде (в Linux-песочнице Wine нет);
  * node.exe может потребовать VC++ Redistributable 2015–2022 на «голой» Windows —
    указано в README_УСТАНОВКА.txt;
  * подпись кода (signtool) — на стенде заказчика (§8.3 линии A).

Запуск: python3 build_windows_exe.py [--version 1.0.0] [--skip-runtimes]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
UNIFIED = os.path.dirname(HERE)
REPO = os.path.dirname(UNIFIED)

PY_URL = "https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip"
NODE_URL = "https://nodejs.org/dist/v20.18.1/node-v20.18.1-win-x64.zip"
NODE_EXE_IN_ZIP = "node-v20.18.1-win-x64/node.exe"

# что берём из дерева репозитория (include-списки — всё остальное не входит)
INCLUDE = {
    "afina/src/afina": None,          # пакет платформы (целиком)
    "afina/src/client": None,         # веб-клиент
    "afina/data": None,               # машиночитаемые приложения (реестры/словари/RTM-шаблон)
    "afina/Афина_Техническое_задание.md": None,
    "eng6-service/core": None,
    "eng6-service/api": None,
    "eng6-service/frontend": None,
    "eng6-service/integrations": None,
    "eng6-service/scripts": None,
    "eng6-service/examples": None,
    "eng6-service/docs": None,
    "eng6-service/package.json": None,
    "unified/bridge": None,
    "unified/ui": None,
    "unified/scripts": None,
    "unified/rtm": None,
    "unified/deploy": None,
    "unified/docs": None,
    "unified/README.md": None,
    "unified/WORKPLAN.md": None,
}
EXCLUDE_DIRS = {"__pycache__", "node_modules", "var", "out", ".git"}
EXCLUDE_SUFFIX = (".pyc", ".log", ".ndjson", ".db")


def log(msg: str):
    print(f"[build] {msg}", flush=True)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def copy_tree(src: str, dst: str):
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for fn in files:
            if fn.endswith(EXCLUDE_SUFFIX):
                continue
            s = os.path.join(root, fn)
            rel = os.path.relpath(s, src)
            d = os.path.join(dst, rel)
            os.makedirs(os.path.dirname(d), exist_ok=True)
            shutil.copy2(s, d)


def stage_payload(payload: str, skip_runtimes: bool, cache: str):
    if os.path.isdir(payload):
        shutil.rmtree(payload)
    os.makedirs(payload)

    # 1. дерево программы
    for rel in INCLUDE:
        src = os.path.join(REPO, rel)
        if not os.path.exists(src):
            raise SystemExit(f"в репозитории отсутствует {rel}")
        dst = os.path.join(payload, rel.replace("/", os.sep))
        if os.path.isdir(src):
            copy_tree(src, dst)
        else:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
    log("дерево программы скопировано")

    # 2. запускалки из installer/
    for f in ("run_unified.pyw", "stop_unified.py"):
        shutil.copy2(os.path.join(HERE, f), os.path.join(payload, f))

    def write_crlf(name: str, text: str):
        # newline="" — без автоконверсии; нормализуем сами: сначала убираем \r, потом \n→\r\n
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
        with open(os.path.join(payload, name), "w", encoding="utf-8", newline="") as f:
            f.write(text)

    write_crlf("Афина.bat", '@echo off\r\ncd /d "%~dp0"\r\n'
                            'start "" "runtimes\\python\\pythonw.exe" run_unified.pyw\r\n')
    write_crlf("Остановить.bat", '@echo off\r\ncd /d "%~dp0"\r\n'
                                 '"runtimes\\python\\python.exe" stop_unified.py\r\npause\r\n')
    write_crlf("Проверка.bat", '@echo off\r\ncd /d "%~dp0"\r\n'
                               'set "PATH=%~dp0runtimes\\node;%PATH%"\r\n'
                               '"runtimes\\python\\python.exe" unified\\scripts\\verify_install.py '
                               '--skip-xver\r\npause\r\n')
    write_crlf("README_УСТАНОВКА.txt", README_INSTALL_TXT)

    # 3. рантаймы
    if not skip_runtimes:
        os.makedirs(cache, exist_ok=True)
        py_zip = os.path.join(cache, os.path.basename(PY_URL))
        node_zip = os.path.join(cache, os.path.basename(NODE_URL))
        for url, dst in ((PY_URL, py_zip), (NODE_URL, node_zip)):
            if not os.path.exists(dst):
                log(f"скачиваю {os.path.basename(url)}…")
                urllib.request.urlretrieve(url, dst + ".part")
                os.replace(dst + ".part", dst)
        # python embeddable → runtimes/python/
        pydir = os.path.join(payload, "runtimes", "python")
        os.makedirs(pydir, exist_ok=True)
        with zipfile.ZipFile(py_zip) as z:
            names = z.namelist()
            for req in ("python.exe", "pythonw.exe"):
                if req not in names:
                    raise SystemExit(f"в {py_zip} нет {req}")
            z.extractall(pydir)
        # критичные модули платформы: sqlite3, hashlib(OpenSSL), ssl
        flat = " ".join(names)
        for req in ("_sqlite3.pyd", "_hashlib.pyd", "libcrypto"):
            if req not in flat:
                log(f"ВНИМАНИЕ: в embeddable-пакете не найден {req} — проверьте на стенде")
        # node.exe → runtimes/node/node.exe
        nodedir = os.path.join(payload, "runtimes", "node")
        os.makedirs(nodedir, exist_ok=True)
        with zipfile.ZipFile(node_zip) as z:
            with z.open(NODE_EXE_IN_ZIP) as srcf, \
                 open(os.path.join(nodedir, "node.exe"), "wb") as dstf:
                shutil.copyfileobj(srcf, dstf)
        log("рантаймы извлечены: python 3.11.9 embeddable + node v20.18.1")
    else:
        log("--skip-runtimes: установщик БЕЗ рантаймов (потребуется системные Python 3.11+/Node 20+)")

    # 4. манифест целостности (SEC-002)
    files = {}
    for root, dirs, fns in os.walk(payload):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for fn in fns:
            p = os.path.join(root, fn)
            rel = os.path.relpath(p, payload).replace(os.sep, "/")
            files[rel] = sha256_file(p)
    man = {"document": "bundle_manifest — СПО «Афина» единая программа (SEC-002)",
           "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "hash_algorithm": "sha256", "files_total": len(files), "files": files}
    with open(os.path.join(payload, "bundle_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False, indent=0)
    log(f"манифест: {len(files)} файлов")
    return len(files)


README_INSTALL_TXT = """СПО «Афина» — единая программа планирования и моделирования систем связи
================================================================================

УСТАНОВКА
  1. Запустите AfinaUnified-Setup-*.exe (права администратора — стандартный
     запрос UAC; программа ставится в Program Files\\AfinaUnified).
  2. После установки: ярлык «Афина» на рабочем столе или в меню Пуск.

ЗАПУСК / РАБОТА
  «Афина — запуск» (Афина.bat):
    • проверяет целостность установки (sha256-манифест, SEC-002);
    • поднимает вычислительный контур ENG6-PLAN (порт 8080) и платформу
      «Афина» (порт 8470); если порты заняты — берутся свободные;
    • открывает браузер на ЕДИНОМ РАБОЧЕМ МЕСТЕ (workspace.html):
        – правая панель: «Демо-сценарий» → «Рассчитать план» — карта, маршруты,
          волны развёртывания (запросы напрямую к контуру);
        – левая панель: клиент платформы; ПЕРВЫЙ вход создаёт администратора
          (bootstrap); затем «⤓ Из платформы» переносит обстановку в планировщик.
  «Остановить» (Остановить.bat) — корректная остановка обоих сервисов.
  «Проверка установки» (Проверка.bat) — серверные проверки (healthz, sha256
    ядра, конформанс 33+5, эталонный расчёт, детерминизм, /metrics, RBAC,
    аудит-цепочка); отчёт — на экране.

ГДЕ ЧТО ЛЕЖИТ
  Программа:  C:\\Program Files\\AfinaUnified\\
  Данные (БД, журналы, pid):  %LOCALAPPDATA%\\AfinaUnified\\
    afina.db — база платформы; unified.log / engine.log — журналы;
    unified_audit.ndjson — единый журнал аудита (хэш-цепочка).

ТРЕБОВАНИЯ
  Windows 10/11 x64. Python и Node.js в системе НЕ нужны (встроены в установку).
  Если Windows сообщит об отсутствии VCRUNTIME140_1.dll — установите
  «VC++ Redistributable 2015–2022» (однократно, сайт Microsoft).

БЕЗОПАСНОСТЬ / ЧЕСТНОСТЬ
  • Все обращения — только 127.0.0.1 (внешних соединений нет, AUT-002/008);
  • координаты не логируются (в аудите — sha256 входа, требование Военпреда);
  • подмена любого файла установки → отказ запуска (SEC-002);
  • ядро ENG6 v5.1.0 — вендор без изменений (sha256 adb610c9eee1ca17…);
    правовой статус — см. unified/docs/03_IP_AUDIT_LETTER_DRAFT.md;
  • подпись установщика (signtool) выполняется на стенде заказчика (§8.3).

УДАЛЕНИЕ
  «Параметры → Приложения → СПО Афина» или ярлык «Удаление».
  Данные в %LOCALAPPDATA%\\AfinaUnified удаляются только по явному подтверждению.

Версия: 1.0.0 · Собрано: """ + time.strftime("%d.%m.%Y") + """
"""


def build_nsis(payload: str, dist: str, version: str) -> str:
    if not shutil.which("makensis"):
        raise SystemExit("makensis не найден: apt-get install nsis (Linux) или NSIS 3 (Windows)")
    os.makedirs(dist, exist_ok=True)
    out = os.path.join(dist, f"AfinaUnified-Setup-{version}.exe")
    cmd = ["makensis", "-INPUTCHARSET", "UTF8",
           f"-DPAYLOAD={payload}", f"-DVERSION={version}", f"-DOUT={out}",
           os.path.join(HERE, "afina_unified.nsi")]
    log(" ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True)
    tail = "\n".join(r.stdout.splitlines()[-12:])
    if r.returncode != 0:
        print(r.stdout[-4000:])
        print(r.stderr[-2000:])
        raise SystemExit(f"makensis rc={r.returncode}")
    log(tail)
    if not os.path.exists(out):
        raise SystemExit(f"установщик не создан: {out}")
    sha = sha256_file(out)
    size_mb = os.path.getsize(out) / 1e6
    with open(os.path.join(dist, "MANIFEST.txt"), "w", encoding="utf-8") as f:
        f.write(f"AfinaUnified-Setup-{version}.exe\nsha256={sha}\nbytes={os.path.getsize(out)}\n"
                f"built={time.strftime('%Y-%m-%d %H:%M:%S')}\nnsis=v3 (кросс-сборка Linux x64)\n"
                f"runtimes=python-3.11.9-embed-amd64 + node-v20.18.1-win-x64\n")
    log(f"ГОТОВО: {out} · {size_mb:.1f} МБ · sha256 {sha[:16]}…")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="1.0.0")
    ap.add_argument("--dist", default=os.path.join(HERE, "dist"))
    ap.add_argument("--cache", default=os.path.join(os.path.expanduser("~"), ".cache", "afina-installer"))
    ap.add_argument("--skip-runtimes", action="store_true",
                    help="вариант без bundled-рантаймов (требует системные Python/Node)")
    args = ap.parse_args()
    payload = os.path.join(HERE, "build", "payload")
    n = stage_payload(payload, args.skip_runtimes, args.cache)
    out = build_nsis(payload, args.dist, args.version)
    print(f"\nУстановщик: {out}")
    print(f"Файлов в бандле: {n} · проверка исполнения — на Windows-стенде (см. README_УСТАНОВКА.txt)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
