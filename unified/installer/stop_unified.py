"""stop_unified.py — корректная остановка единой программы (по pid-файлу)."""
import json
import os
import signal
import sys
import time

DATA = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "AfinaUnified")
PIDFILE = os.path.join(DATA, "unified.pid")


def main() -> int:
    if not os.path.exists(PIDFILE):
        print("программа не запущена (pid-файл отсутствует)")
        return 0
    with open(PIDFILE, encoding="utf-8") as f:
        pids = json.load(f)
    stopped = []
    for key in ("engine_pid", "launcher_pid"):
        pid = pids.get(key)
        if not pid:
            continue
        try:
            if os.name == "nt":
                subprocess_run = __import__("subprocess").run
                subprocess_run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True)
            else:
                os.kill(pid, signal.SIGTERM)
            stopped.append(f"{key}={pid}")
        except Exception as e:
            stopped.append(f"{key}={pid}: {e}")
    time.sleep(0.5)
    try:
        os.remove(PIDFILE)
    except OSError:
        pass
    print("остановлено:", ", ".join(stopped) or "ничего")
    return 0


if __name__ == "__main__":
    sys.exit(main())
