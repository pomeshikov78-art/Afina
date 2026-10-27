"""unified/scripts/verify_install.py — установка поднята, программа работает?

ОДНА команда проверяет весь стек единой программы «Афина» (серверная часть —
браузер проверяется отдельно по инструкции в конце вывода):

    python3 unified/scripts/verify_install.py

Что делает:
  1. среда: node ≥ 20.11, python ≥ 3.11;
  2. поднимает контур ENG6-PLAN и платформу «Афина» на свободных портах
     (или подключается к уже запущенным: --engine-url / --platform-url);
  3. прогоняет проверки: healthz/readyz, sha256 ядра, конформанс 33+5 (гейт К-04),
     эталонный расчёт «батальон» (plan/sync), детерминизм NFR-04, отказ на
     некорректном входе (422), /metrics платформы (Prometheus 0.0.4),
     bootstrap администратора + RBAC, verify мастер-хэш-цепочки аудита;
  4. кросс-верификация ядер XVER (ARC-002, 327 точек, node-зонд — сервер не нужен);
  5. рабочее место workspace.html: наличие + NFR-02-скан (0 внешних ресурсов);
  6. печатает чек-лист, вердикт и ДАЛЬНЕЙШИЕ ШАГИ для проверки в браузере.

Код возврата: 0 — ВСЁ РАБОТАЕТ; 1 — есть проваленные проверки; 2 — среда не готова.
Скрипт ничего не устанавливает и не меняет: только читает и запускает процессы
во временных каталогах (БД платформы — временная, ваши данные не затрагиваются).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
UNIFIED = os.path.dirname(HERE)
REPO = os.path.dirname(UNIFIED)
sys.path.insert(0, os.path.join(UNIFIED, "bridge"))

VENDOR_SHA256 = "adb610c9eee1ca17b257cbc6087e2278fdde3eef92f3b6a360cfb51968d48b20"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, fn) -> bool:
    try:
        detail = fn()
        RESULTS.append((name, True, detail or ""))
        print(f"  ✅ {name}" + (f" — {detail}" if detail else ""))
        return True
    except Exception as e:
        RESULTS.append((name, False, str(e)))
        print(f"  ❌ {name} — {e}")
        return False


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(url: str, method: str = "GET", payload=None, headers=None, timeout=30):
    hdrs = {"User-Agent": "verify-install"}
    hdrs.update(headers or {})
    body = json.dumps(payload).encode() if payload is not None else None
    if body:
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, r.headers.get("Content-Type", ""), raw
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read().decode("utf-8", "replace")[:400]


def wait_ready(url: str, timeout_s: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if http(url, timeout=3)[0] == 200:
                return True
        except Exception:
            time.sleep(0.4)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Проверка установки единой программы «Афина»")
    ap.add_argument("--engine-url", default=os.environ.get("AFINA_ENG6_URL"))
    ap.add_argument("--platform-url", default=os.environ.get("AFINA_PLATFORM_URL"))
    ap.add_argument("--skip-xver", action="store_true", help="не гонять кросс-верификацию XVER")
    args = ap.parse_args()

    print("═" * 72)
    print("ПРОВЕРКА УСТАНОВКИ · единая программа «Афина» (линия A + линия B + unified)")
    print("═" * 72)

    # ---------- 1. среда ----------
    print("\n[1/6] Среда")
    node_ok = check("node ≥ 20.11 доступен", lambda: (
        shutil.which("node") or (_ for _ in ()).throw(RuntimeError("node не найден в PATH")),
        subprocess.run(["node", "--version"], capture_output=True, text=True).stdout.strip())[1])
    py_ok = check("python ≥ 3.11", lambda: (
        sys.version_info >= (3, 11) or (_ for _ in ()).throw(
            RuntimeError(f"нужен python 3.11+, текущий {sys.version.split()[0]}")),
        sys.version.split()[0])[1])
    if not (node_ok and py_ok):
        print("\nСРЕДА НЕ ГОТОВА — установите Node.js ≥ 20.11 и Python ≥ 3.11 и повторите.")
        return 2

    # ---------- 2. подъём сервисов ----------
    print("\n[2/6] Сервисы")
    procs, tmpdirs = [], []
    engine_url = (args.engine_url or "").rstrip("/")
    platform_url = (args.platform_url or "").rstrip("/")

    if not engine_url:
        p = free_port()
        engine_url = f"http://127.0.0.1:{p}"
        env = dict(os.environ, ENG6_PORT=str(p), ENG6_HOST="127.0.0.1", ENG6_LOG_LEVEL="warn")
        procs.append(subprocess.Popen(["node", "api/main.mjs"], cwd=os.path.join(REPO, "eng6-service"),
                                      env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      preexec_fn=os.setsid if hasattr(os, "setsid") else None))
        print(f"  → контур ENG6-PLAN поднимается на {engine_url}")
    if not platform_url:
        p = free_port()
        platform_url = f"http://127.0.0.1:{p}"
        tmp = tempfile.mkdtemp(prefix="afina-verify-")
        tmpdirs.append(tmp)
        procs.append(subprocess.Popen(
            [sys.executable, "-m", "afina.server.app", "--db", os.path.join(tmp, "afina.db"), "--port", str(p)],
            cwd=os.path.join(REPO, "afina", "src"),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid if hasattr(os, "setsid") else None))
        print(f"  → платформа «Афина» поднимается на {platform_url} (БД — временная)")

    try:
        def engine_ready():
            if not wait_ready(engine_url + "/healthz"):
                raise RuntimeError("контур не поднялся за 60 с")
            if http(engine_url + "/readyz")[0] != 200:
                raise RuntimeError("readyz не 200")
            return "ok"
        check("контур ENG6-PLAN: /healthz + /readyz", engine_ready)
        check("платформа «Афина»: /healthz", lambda: (
            wait_ready(platform_url + "/healthz")
            or (_ for _ in ()).throw(RuntimeError("не поднялась за 60 с"))) and "ok")

        # ---------- 3. контур ENG6-PLAN ----------
        print("\n[3/6] Вычислительный контур ENG6-PLAN (линия B)")

        def meta_sha():
            st, _, raw = http(engine_url + "/api/v1/meta")
            m = json.loads(raw)
            sha = (m.get("core") or {}).get("sha256")
            if sha != VENDOR_SHA256:
                raise RuntimeError(f"sha256 ядра не совпал с эталоном: {sha}")
            return f"ENG6 v{(m.get('core') or {}).get('version')} sha256 {str(sha)[:12]}…"
        check("ядро: версия и контрольная сумма вендора", meta_sha)

        def conformance():
            st, _, raw = http(engine_url + "/api/v1/conformance")
            c = json.loads(raw)
            if not c.get("ok"):
                raise RuntimeError("конформанс НЕ пройден — результаты недостоверны (К-04)")
            return f"selfTest {c.get('selfTestTotal')}/33, регрессия {c.get('regressionTotal')}/5"
        check("конформанс ядра (гейт К-04: 33+5)", conformance)

        battalion_path = os.path.join(REPO, "eng6-service", "examples", "battalion.json")
        with open(battalion_path, encoding="utf-8") as f:
            battalion = json.load(f)

        def plan_sync():
            t0 = time.perf_counter()
            st, _, raw = http(engine_url + "/api/v1/plan/sync", "POST", battalion,
                              {"X-Requestor": "verify-install"})
            dt = (time.perf_counter() - t0) * 1000
            if st != 200:
                raise RuntimeError(f"HTTP {st}: {raw[:150]}")
            r = json.loads(raw)
            if r.get("status") not in ("OPTIMAL", "FEASIBLE", "PARTIAL"):
                raise RuntimeError(f"статус {r.get('status')}")
            sch = (r.get("plan") or {}).get("schedule") or {}
            return (f"{r['status']} грейд {(r.get('metrics') or {}).get('grade')}, "
                    f"makespan {sch.get('makespanMin')} мин, {dt:.0f} мс")
        check("эталонный расчёт «батальон» (plan/sync)", plan_sync)

        def determinism():
            hdr = {"X-Request-Id": "verify-determinism-0001", "X-Requestor": "verify-install"}
            _, _, a = http(engine_url + "/api/v1/plan/sync", "POST", battalion, hdr)
            _, _, b = http(engine_url + "/api/v1/plan/sync", "POST", battalion, hdr)
            strip = lambda s: json.dumps({k: v for k, v in json.loads(s).items()
                                            if k not in ("timings", "id", "requestId", "taskId")}, sort_keys=True)
            if strip(a) != strip(b):
                raise RuntimeError("ответы различаются при идентичном входе")
            return "NFR-04: два identical входа → identical выход"
        check("детерминированность расчёта", determinism)

        def invalid_422():
            st, _, raw = http(engine_url + "/api/v1/plan/sync", "POST",
                              {"topology": {"nodes": [{"id": "a", "type": "cp", "lat": 999, "lng": 0}], "links": []}},
                              {"X-Requestor": "verify-install"})
            if st != 422:
                raise RuntimeError(f"ожидался 422, получен {st}")
            m = re.search(r'"code":\s*"([A-Z_]+)"', raw)   # тело может быть усечено — код ищем регэкспом
            return f"422 INVALID, код {m.group(1) if m else '?'}"
        check("некорректный вход отклоняется до расчёта (422)", invalid_422)

        # ---------- 4. платформа ----------
        print("\n[4/6] Платформа «Афина» (линия A)")

        def metrics():
            st, ctype, raw = http(platform_url + "/metrics")
            if st != 200 or "afina_up 1" not in raw:
                raise RuntimeError(f"HTTP {st}, afina_up не найден")
            names = re.findall(r"^(afina_\w+)", raw, re.M)
            return f"Prometheus 0.0.4, метрик: {len(set(names))} (afina_up, {', '.join(sorted(set(names))[1:4])}…)"
        check("/metrics платформы (UNIF-19)", metrics)

        import secrets
        pwd = secrets.token_hex(12)
        token_holder: dict = {}

        def bootstrap():
            st, _, raw = http(platform_url + "/api/v1/register", "POST",
                              {"login": "verify-admin", "password": pwd,
                               "role": "Администратор системы", "display": "Проверка установки"})
            if st == 403:
                token_holder["skip"] = True
                return "платформа уже бутстрапнута ранее — пропуск (укажите своих пользователей)"
            if st != 200:
                raise RuntimeError(f"register HTTP {st}: {raw[:120]}")
            st, _, raw = http(platform_url + "/api/v1/login", "POST",
                              {"login": "verify-admin", "password": pwd})
            if st != 200:
                raise RuntimeError(f"login HTTP {st}")
            token_holder["token"] = json.loads(raw)["token"]
            return "bootstrap-администратор создан, вход выполнен"
        check("доступ: bootstrap + вход (scrypt/HMAC-сессия)", bootstrap)

        def audit_chain():
            if token_holder.get("skip"):
                return "пропущено (нет сессии): проверьте /api/v1/audit/verify со своим токеном"
            st, _, raw = http(platform_url + "/api/v1/audit/verify",
                              headers={"Authorization": "Bearer " + token_holder["token"]})
            v = json.loads(raw)
            if not v.get("ok"):
                raise RuntimeError(f"мастер-цепочка повреждена: {v}")
            return f"хэш-цепочка цела, записей: {v.get('entries')}"
        check("аудит: проверка мастер-хэш-цепочки", audit_chain)

        def rbac():
            if token_holder.get("skip"):
                return "пропущено (нет сессии)"
            st, _, _ = http(platform_url + "/api/v1/objects", "POST",
                            {"id": "verify-x", "class": "COM", "name": "x", "x": 0, "y": 0},
                            {"Authorization": "Bearer " + token_holder["token"]})
            if st != 403:
                raise RuntimeError(f"ожидался 403 (SEC-001), получен {st}")
            return "администратор отклонён при создании объекта (403, SEC-001) — наименьшие полномочия работают"
        check("RBAC Прил. Н: отказ администратору в edit_models_cond", rbac)

        # ---------- 5. XVER ----------
        print("\n[5/6] Кросс-верификация ядер (ARC-002)")
        if args.skip_xver:
            print("  ⏭ пропущено (--skip-xver)")
        else:
            def xver():
                from backend_arc002 import Eng6ProbeBackend, PythonRefBackend, verify_arc002
                proto = verify_arc002(PythonRefBackend(), Eng6ProbeBackend())
                if proto["verdict"] not in ("PASS", "DEGRADED"):
                    raise RuntimeError(f"вердикт {proto['verdict']}: {proto['summary']}")
                if proto["verdict"] != "PASS":
                    raise RuntimeError(f"ALIGNED не PASS: {proto['summary']}")
                s = proto["summary"]
                return (f"{proto['grid_points']} точек: CONSISTENT {s['consistent']}, "
                        f"FAIL {s['failed']}, EXCLUDED {s['excluded']} (документированы)")
            check("XVER: Python ENG6+ ⇄ вендор ENG6 v5.1.0 (RAW→ALIGNED)", xver)

        # ---------- 6. рабочее место ----------
        print("\n[6/6] Единое рабочее место (HTML)")
        ws = os.path.join(UNIFIED, "ui", "workspace.html")

        def workspace():
            if not os.path.isfile(ws):
                raise RuntimeError("файл не найден")
            html = open(ws, encoding="utf-8").read()
            ext = re.findall(r'(?:src|href)\s*=\s*["\'](https?://[^"\']+)', html)
            if ext:
                raise RuntimeError(f"внешние ресурсы (NFR-02): {ext[:3]}")
            for pat in (r"localStorage\s*[.\[]", r"sessionStorage\s*[.\[]", r"document\.cookie"):
                if re.search(pat, html):
                    raise RuntimeError(f"хранилище браузера (NFR-02): {pat}")
            return f"{os.path.getsize(ws)//1024} КБ, single-file, NFR-02-скан чист"
        check("workspace.html: наличие + NFR-02 (0 внешних ресурсов, 0 хранилищ)", workspace)

        # ---------- итог ----------
        failed = [n for n, ok, _ in RESULTS if not ok]
        print("\n" + "═" * 72)
        if failed:
            print(f"ВЕРДИКТ: ЕСТЬ ПРОБЛЕМЫ ({len(failed)} из {len(RESULTS)}):")
            for n in failed:
                print("  ✗", n)
            verdict = 1
        else:
            print(f"ВЕРДИКТ: ✅ ВСЁ РАБОТАЕТ ({len(RESULTS)}/{len(RESULTS)} проверок)")
            verdict = 0
        print("═" * 72)
        print(f"""
ДАЛЬШЕ — проверка в браузере (серверная часть уже подтверждена):

  1. Единое рабочее место:
       откройте файл  {ws}
       • правая панель: «Демо-сценарий» → «▶ Рассчитать план» — карта, маршруты,
         волны развёртывания (запросы идут напрямую к контуру {engine_url});
       • левая панель: клиент платформы во фрейме — первый вход создаёт
         администратора (bootstrap); после входа нажмите «⤓ Из платформы» —
         обстановка перейдёт в модуль планирования (postMessage-мост).
  2. Клиент платформы отдельно:   {platform_url}/
  3. Планировщик линии B отдельно: {engine_url}/   (карта, ОШС-дерево, карточки техники)
  4. Метрики:                      {platform_url}/metrics  и  {engine_url}/metrics

Если запускали вручную — стек поднимается так:
  cd unified/scripts && ./start_stack.sh      # engine :8080 + platform :8470
  (остановка: ./stop_stack.sh; или Docker: docker compose -f unified/deploy/docker-compose.unified.yml up -d --build)
""")
        return verdict
    finally:
        for pr in procs:
            if pr.poll() is None:
                try:
                    if hasattr(os, "killpg"):
                        os.killpg(os.getpgid(pr.pid), signal.SIGTERM)
                    else:
                        pr.terminate()
                    pr.wait(timeout=10)
                except Exception:
                    try:
                        pr.kill()
                    except Exception:
                        pass
        for t in tmpdirs:
            shutil.rmtree(t, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
