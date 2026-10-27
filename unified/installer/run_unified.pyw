"""run_unified.pyw — единая точка запуска установленной программы «Афина» (Windows).

Порядок (наследует run_server.pyw линии A, расширен до двух серверов):
  1. локальная проверка целостности бандла по bundle_manifest.json (SEC-002):
     подмена/утеря файла → отказ запуска + запись в журнал;
  2. выбор портов (8080/8470 по умолчанию; заняты — свободные);
  3. подъём вычислительного контура ENG6-PLAN (bundled node.exe, subprocess);
  4. подъём платформы «Афина» В ПРОЦЕССЕ (bundled python, create_server + сид
     каталога — как в штатном загрузчике линии A); БД и журналы — в
     %LOCALAPPDATA%\\AfinaUnified (Program Files недоступен на запись);
  5. ожидание /healthz обоих;
  6. открытие браузера на unified/ui/workspace.html?engine=…&platform=…
     (единое рабочее место; фрейм платформы и запросы к контуру — из него);
  7. pid-файл для «Остановить»; журнал — unified.log там же.

Внешних обращений нет (AUT-002/008): всё исполняется из установленного дерева.
"""
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "AfinaUnified")
os.makedirs(DATA, exist_ok=True)
LOG = os.path.join(DATA, "unified.log")
PIDFILE = os.path.join(DATA, "unified.pid")
DB = os.path.join(DATA, "afina.db")
# очередь платформы и аудит-леджер создаются рядом с БД автоматически
# (dirname(db_path) → %LOCALAPPDATA%\AfinaUnified) — явные пути не нужны

NODE_EXE = os.path.join(HERE, "runtimes", "node", "node.exe")
ENGINE_DIR = os.path.join(HERE, "eng6-service")
PLATFORM_SRC = os.path.join(HERE, "afina", "src")
WORKSPACE = os.path.join(HERE, "unified", "ui", "workspace.html")
REQUIREMENTS = os.path.join(HERE, "afina", "data", "Афина_Реестр_требований_ПриложениеА.json")
TZ = os.path.join(HERE, "afina", "Афина_Техническое_задание.md")


def log(msg: str):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def fail(msg: str, code: int = 2):
    log("ОТКАЗ: " + msg)
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, msg, "СПО «Афина» — запуск отклонён", 0x10)
    except Exception:
        pass
    sys.exit(code)


def verify_bundle():
    """SEC-002: sha256 каждого файла бандла против манифеста."""
    man_path = os.path.join(HERE, "bundle_manifest.json")
    if not os.path.exists(man_path):
        return True, "манифест не найден — пропуск (сборка dev)"
    with open(man_path, encoding="utf-8") as f:
        man = json.load(f)
    bad = []
    for rel, h in man["files"].items():
        p = os.path.join(HERE, rel.replace("/", os.sep))
        try:
            with open(p, "rb") as fp:
                if hashlib.sha256(fp.read()).hexdigest() != h:
                    bad.append(rel)
        except OSError:
            bad.append(rel)
    return (not bad), ("целостность подтверждена (" + str(len(man["files"])) + " файлов)"
                       if not bad else "нарушена целостность: " + ", ".join(bad[:8]))


def free_port(preferred: int) -> int:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            pass
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_health(url: str, timeout_s: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def main():
    ok, msg = verify_bundle()
    log("контроль целостности (SEC-002): " + msg)
    if not ok:
        fail("Установка отклонена (SEC-002): " + msg + "\nОбратитесь к администратору.")

    if not os.path.isfile(NODE_EXE):
        fail(f"Не найден bundled Node: {NODE}\nПереустановите программу.")
    eng_port = free_port(8080)
    plat_port = free_port(8470)

    # --- контур ENG6-PLAN (подпроцесс, bundled node.exe) ---
    env = dict(os.environ, ENG6_PORT=str(eng_port), ENG6_HOST="127.0.0.1",
               ENG6_LOG_LEVEL="info", ENG6_SERVE_UI="true")
    eng_log = open(os.path.join(DATA, "engine.log"), "a", encoding="utf-8")
    creationflags = 0x00000208 if os.name == "nt" else 0  # NEW_PROCESS_GROUP | NO_WINDOW
    engine = subprocess.Popen([NODE_EXE, "api/main.mjs"], cwd=ENGINE_DIR, env=env,
                              stdout=eng_log, stderr=eng_log, creationflags=creationflags)
    log(f"контур ENG6-PLAN: pid {engine.pid}, порт {eng_port}")

    # --- платформа «Афина» (в процессе, bundled python) ---
    sys.path.insert(0, PLATFORM_SRC)
    try:
        from afina.server.app import create_server
        from afina.catalog import seed
    except Exception as e:
        engine.terminate()
        fail(f"Не удалось импортировать платформу: {e}")

    # pid-файл — СРАЗУ после старта движка: «Остановить» убьёт сироту даже при
    # падении платформы (дефект OBS-14: раньше pid писался позже, движок зависал)
    def write_pid(**extra):
        data = {"engine_pid": engine.pid, "launcher_pid": os.getpid(),
                "engine_port": eng_port, "platform_port": plat_port,
                "started": time.strftime("%Y-%m-%d %H:%M:%S")}
        data.update(extra)
        try:
            with open(PIDFILE, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except OSError:
            pass
    write_pid(platform="starting")

    try:
        # ВНИМАНИЕ: только параметры из фактической сигнатуры create_server()
        # (db_path, host, port, requirements_path, tz_path, ai_enabled) —
        # queue_path НЕТ в фабрике (OBS-14); очередь платформа кладёт сама
        # рядом с БД (dirname(db_path)/queue.json) — то есть в %LOCALAPPDATA%.
        httpd, app = create_server(db_path=DB, host="127.0.0.1", port=plat_port,
                                   requirements_path=REQUIREMENTS if os.path.exists(REQUIREMENTS) else None,
                                   tz_path=TZ if os.path.exists(TZ) else None)
        seed.build(app.store)
    except Exception as e:
        engine.terminate()
        try:
            engine.wait(timeout=5)
        except Exception:
            engine.kill()
        fail(f"Не удалось запустить платформу: {type(e).__name__}: {e}")

    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log(f"платформа: порт {plat_port}, БД {DB}")
    write_pid(platform="running")

    if not wait_health(f"http://127.0.0.1:{eng_port}/healthz"):
        engine.terminate()
        fail("Контур ENG6-PLAN не ответил за 60 с — см. журнал: " + os.path.join(DATA, "engine.log"))
    if not wait_health(f"http://127.0.0.1:{plat_port}/healthz"):
        engine.terminate()
        fail("Платформа не ответила за 60 с — см. журнал: " + LOG)
    log("оба сервиса готовы")

    # --- единое рабочее место (HTML, обращающийся к серверам) ---
    ws = WORKSPACE.replace(os.sep, "/")
    webbrowser.open(f"file:///{ws}?engine=http://127.0.0.1:{eng_port}&platform=http://127.0.0.1:{plat_port}")
    log("браузер открыт: workspace.html")

    # удержание процесса: платформа живёт здесь; остановка — «Остановить.bat»
    try:
        while True:
            if engine.poll() is not None:
                log(f"контур завершился (код {engine.returncode}) — остановка платформы")
                break
            time.sleep(2)
    finally:
        try:
            httpd.shutdown()
        except Exception:
            pass
        try:
            os.remove(PIDFILE)
        except OSError:
            pass
        log("остановлено")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        fail(f"Непредвиденная ошибка запуска: {type(e).__name__}: {e}\nЖурнал: {LOG}")
