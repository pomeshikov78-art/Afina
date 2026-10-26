"""unified/scripts/demo_e2e.py — сквозной сценарий совмещённой платформы (UNIF-08).

Цепочка (всё stdlib-only, детерминированно):
  1. подъём вычислительного контура ENG6-PLAN (или подключение к запущенному);
  2. конформанс-гейт К-04 (недостоверное ядро → отказ, не подмена);
  3. сущности платформы (Прил. Г: ORG_UNIT/ASSET/TECHNICAL_STATE/GEO_OBJECT/заявки)
     → запрос ENG6-PLAN через unified/bridge/datamodel_map;
  4. расчёт plan/sync через Eng6PlanBackend + детерминированность NFR-04;
  5. обратный маппинг ответа в плоские таблицы платформы (Путь А, К-03);
  6. паспорт расчётного запуска (ENG-001) с фиксацией backend = ENG6-JS sha256;
  7. кросс-верификация ARC-002 (Python ENG6+ ⇄ вендор ENG6, 2 стадии);
  8. протокол ПРОТОКОЛ_UNIF_E2E_<ts>.{json,md} в unified/protocols/.

Запуск:  python3 demo_e2e.py [--base-url http://127.0.0.1:8080] [--own-engine]
Код возврата: 0 — сценарий выполнен; 1 — провал; 2 — инфраструктура.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
UNIFIED = os.path.dirname(HERE)
REPO = os.path.dirname(UNIFIED)
sys.path.insert(0, os.path.join(UNIFIED, "bridge"))

from backend_arc002 import (Eng6PlanBackend, Eng6ProbeBackend, PythonRefBackend,  # noqa: E402
                            make_passport, stable_hash, verify_arc002)
from datamodel_map import build_plan_request, plan_to_platform_tables  # noqa: E402
from eng6_js_client import Eng6JsClient, Eng6ServiceError  # noqa: E402
from xverify import render_md  # noqa: E402  (переиспользуем рендер протокола XVER)

VENDOR_SHA256_EXPECTED = "adb610c9eee1ca17b257cbc6087e2278fdde3eef92f3b6a360cfb51968d48b20"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------- «данные платформы»: сущности Прил. Г (синтетика, координаты учебные) ----------
ORG_UNITS = {
    "u-bat": {"path": "батальон", "nodeId": "n-kp"},          # КП батальона — узел-представитель
    "u-r1": {"path": "батальон/рота-1", "nodeId": "n-kp"},    # роты представлены узлом КП батальона
    "u-r2": {"path": "батальон/рота-2", "nodeId": "n-kp"},
}
ASSETS = [
    {"inventoryId": "КП-001", "model": "П-240Б (УШБ)", "nodeType": "cp", "techState": "исправна",
     "powerWatt": 0, "cost": 40, "deployTimeMin": 45, "crew": 6},
    {"inventoryId": "РЛ-002", "model": "Р-419Л1", "nodeType": "relay", "techState": "исправна",
     "powerWatt": 20, "cost": 30, "deployTimeMin": 35, "crew": 4},
    {"inventoryId": "РС-003", "model": "Р-168-100У", "nodeType": "radio", "techState": "исправна",
     "powerWatt": 100, "cost": 10, "deployTimeMin": 15, "crew": 2},
    {"inventoryId": "РС-004", "model": "Р-168-100У", "nodeType": "radio", "techState": "ограниченно пригодна",
     "powerWatt": 100, "cost": 10, "deployTimeMin": 15, "crew": 2},
    {"inventoryId": "РС-005", "model": "Р-168-25У", "nodeType": "radio", "techState": "исправна",
     "powerWatt": 25, "cost": 8, "deployTimeMin": 12, "crew": 2},
    {"inventoryId": "РС-006", "model": "Р-168-25У", "nodeType": "radio", "techState": "неисправна",
     "powerWatt": 25, "cost": 8, "deployTimeMin": 12, "crew": 2},
]
GEO = [
    {"id": "n-kp", "type": "cp", "lat": 55.750000, "lng": 37.620000, "unitId": "КП-001", "orgUnitId": "u-bat"},
    {"id": "n-rl", "type": "relay", "lat": 55.800000, "lng": 37.720000, "unitId": "РЛ-002", "orgUnitId": "u-bat"},
    {"id": "n-r1a", "type": "radio", "lat": 55.830000, "lng": 37.660000, "unitId": "РС-003", "orgUnitId": "u-r1"},
    {"id": "n-r1b", "type": "radio", "lat": 55.850000, "lng": 37.700000, "unitId": "РС-004", "orgUnitId": "u-r1"},
    {"id": "n-r2a", "type": "radio", "lat": 55.770000, "lng": 37.760000, "unitId": "РС-005", "orgUnitId": "u-r2"},
    {"id": "n-r2b", "type": "radio", "lat": 55.740000, "lng": 37.700000, "unitId": "РС-006", "orgUnitId": "u-r2"},
]
RELS = [
    # УКВ-радионаправления (оборудование из парка, стоимость активации 0 — LINK_TYPES.VHF);
    # тип указан явно: автовыбор по дальности дал бы ВОЛС на коротких плечах (дефицит бюджета)
    {"id": "l-kp-rl", "a": "n-kp", "b": "n-rl", "linkType": "VHF"},
    {"id": "l-rl-r1a", "a": "n-rl", "b": "n-r1a", "linkType": "VHF"},
    {"id": "l-rl-r1b", "a": "n-rl", "b": "n-r1b", "linkType": "VHF"},
    {"id": "l-rl-r2a", "a": "n-rl", "b": "n-r2a", "linkType": "VHF"},
    {"id": "l-kp-r2b", "a": "n-kp", "b": "n-r2b", "linkType": "VHF"},
]
ORDERS = [
    {"id": "d-r1a", "src": "n-kp", "dst": "n-r1a", "rateKbps": 16, "priority": 1},
    {"id": "d-r1b", "src": "n-kp", "dst": "n-r1b", "rateKbps": 16, "priority": 2},
    {"id": "d-r2a", "src": "n-kp", "dst": "n-r2a", "rateKbps": 16, "priority": 2},
    {"id": "d-r2b", "src": "n-kp", "dst": "n-r2b", "rateKbps": 16, "priority": 3},
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=None, help="URL запущенного ENG6-PLAN (иначе поднимаем свой)")
    ap.add_argument("--out", default=os.path.join(UNIFIED, "protocols"))
    args = ap.parse_args()

    proc = None
    base_url = args.base_url or os.environ.get("AFINA_ENG6_URL")
    if not base_url:
        if not shutil.which("node"):
            print("E2E: node недоступен — сценарий не выполнить", file=sys.stderr)
            return 2
        port = free_port()
        base_url = f"http://127.0.0.1:{port}"
        env = dict(os.environ, ENG6_PORT=str(port), ENG6_HOST="127.0.0.1",
                   ENG6_SERVE_UI="false", ENG6_LOG_LEVEL="warn")
        proc = subprocess.Popen(["node", "api/main.mjs"], cwd=os.path.join(REPO, "eng6-service"),
                                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                preexec_fn=os.setsid if hasattr(os, "setsid") else None)
        print(f"E2E: поднят вычислительный контур ENG6-PLAN на {base_url} (pid {proc.pid})")

    client = Eng6JsClient(base_url, requestor="afina-unified-e2e")
    steps: list[dict] = []
    exit_code = 0
    try:
        deadline = time.monotonic() + 45
        while not client.ready():
            if proc and proc.poll() is not None:
                raise Eng6ServiceError("контур завершился при старте")
            if time.monotonic() > deadline:
                raise Eng6ServiceError("контур не поднялся за 45 с")
            time.sleep(0.3)
        steps.append({"step": 1, "name": "контур готов", "ok": True, "base_url": base_url})

        # 2. конформанс-гейт (К-04)
        conf = client.conformance_gate()
        steps.append({"step": 2, "name": "конформанс-гейт К-04", "ok": bool(conf.get("ok")),
                      "selfTest": conf.get("selfTestTotal"), "regression": conf.get("regressionTotal")})

        # 3. сущности платформы → запрос ENG6-PLAN
        request = build_plan_request(ASSETS, GEO, RELS, ORDERS, ORG_UNITS,
                                     constraints={"budget": 200, "physics": "fast",
                                                  "maxSimultaneousDeploy": 2, "crewSpeedKmh": 40},
                                     environment={"clutterType": "open", "terrain": {"source": "flat"}},
                                     meta={"name": "E2E совмещённой платформы", "author": "afina-unified"})
        steps.append({"step": 3, "name": "маппинг Прил.Г → контракт ENG6-PLAN", "ok": True,
                      "nodes": len(request["topology"]["nodes"]), "links": len(request["topology"]["links"]),
                      "fleet": len(request["fleet"]), "demands": len(request["demands"]),
                      "request_sha256": stable_hash(request)})

        # 4. расчёт + детерминированность
        plan_backend = Eng6PlanBackend(client)
        fp = plan_backend.fingerprint()
        t0 = time.perf_counter()
        resp = plan_backend.plan(request, mode="sync")
        dt_ms = (time.perf_counter() - t0) * 1000
        resp2 = plan_backend.plan(request, mode="sync")

        def strip(r):
            c = json.loads(json.dumps(r))
            for k in ("timings", "id", "requestId", "taskId"):
                c.pop(k, None)
            sch = (c.get("plan") or {}).get("schedule")
            if sch:
                sch.pop("criticalPath", None)
            return json.dumps(c, sort_keys=True)

        det_ok = strip(resp) == strip(resp2)
        steps.append({"step": 4, "name": "расчёт plan/sync + NFR-04", "ok": resp.get("status") in
                      ("OPTIMAL", "FEASIBLE", "PARTIAL") and det_ok,
                      "status": resp.get("status"), "grade": (resp.get("metrics") or {}).get("grade"),
                      "compute_ms": round(dt_ms, 1), "deterministic": det_ok,
                      "core_sha256": fp.sha256})
        if fp.sha256 != VENDOR_SHA256_EXPECTED:
            steps[-1]["warning"] = "sha256 ядра не совпал с эталоном вендора"

        # 5. обратный маппинг в таблицы платформы
        # 6. паспорт запуска (ENG-001) с фиксацией бэкенда
        probe_fp = Eng6ProbeBackend().fingerprint()
        passport = make_passport("PLAN-01", "ENG6-PLAN", {
            "request_sha256": stable_hash(request),
            "scenario": "батальонный рубеж (синтетика E2E)",
            "nodes": len(request["topology"]["nodes"]),
        }, [fp, probe_fp], mode="Verification")
        tables = plan_to_platform_tables(resp, run_id=passport.run_id)
        issues = [(resp.get("diagnostics") or {}).get("issues") or []]
        passport.finish(status="done" if resp.get("status") in ("OPTIMAL", "FEASIBLE", "PARTIAL") else "error",
                        results_ref=stable_hash(resp),
                        warnings=[f"{i.get('sev')}/{i.get('code')}" for i in issues[0][:6]])
        steps.append({"step": 5, "name": "обратный маппинг → таблицы платформы (К-03)", "ok": True,
                      "tables": {k: len(v) for k, v in tables.items() if v}})
        steps.append({"step": 6, "name": "паспорт запуска ENG-001", "ok": passport.status == "done",
                      "run_id": passport.run_id, "duration_s": passport.duration_s(),
                      "backends": [fp.backend_id, probe_fp.backend_id]})

        # 7. ARC-002 / XVER
        xv = verify_arc002(PythonRefBackend(), Eng6ProbeBackend())
        steps.append({"step": 7, "name": "кросс-верификация ARC-002 (RAW→ALIGNED)",
                      "ok": xv["verdict"] in ("PASS", "DEGRADED"),
                      "verdict": xv["verdict"], "points": xv["grid_points"],
                      "aligned": xv["stages"]["aligned"]["summary"]})

        # 8. протокол
        ts = time.strftime("%Y%m%d_%H%M%S")
        os.makedirs(args.out, exist_ok=True)
        proto = {
            "document": "ПРОТОКОЛ UNIF-E2E — сквозной сценарий совмещённой платформы",
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "requirement": {"id": "UNIF-08", "criteria": "Сквозная цепочка: сущности платформы → ENG6-PLAN → таблицы платформы + паспорт + ARC-002"},
            "engine": {"base_url": base_url, "core_sha256": fp.sha256, "conformance_ok": bool(conf.get("ok"))},
            "passport": passport.to_dict(),
            "plan_summary": {"status": resp.get("status"), "metrics": resp.get("metrics"),
                             "schedule": {"makespanMin": ((resp.get("plan") or {}).get("schedule") or {}).get("makespanMin"),
                                          "waves": len(((resp.get("plan") or {}).get("schedule") or {}).get("waves") or [])}},
            "steps": steps,
            "xver_summary": {"verdict": xv["verdict"], "raw": xv["stages"]["raw"]["summary"],
                             "aligned": xv["stages"]["aligned"]["summary"],
                             "alignments": xv["alignments"], "known_incompatible": xv["known_incompatible"]},
            "verdict": "PASS" if all(s["ok"] for s in steps) else "FAIL",
        }
        jp = os.path.join(args.out, f"ПРОТОКОЛ_UNIF_E2E_{ts}.json")
        with open(jp, "w", encoding="utf-8") as f:
            json.dump(proto, f, ensure_ascii=False, indent=1)
        # md-представление: шаги + XVER-таблицы (рендер протокола сверки)
        md = ["# ПРОТОКОЛ UNIF-E2E — сквозной сценарий совмещённой платформы", "",
              f"Дата: {proto['generated_at']} · Вердикт: **{proto['verdict']}** · Ядро: ENG6 v{fp.version} ({str(fp.sha256)[:16]}…)", "",
              "| Шаг | Проверка | Ok | Детали |", "|---|---|---|---|"]
        for s in steps:
            det = {k: v for k, v in s.items() if k not in ("step", "name", "ok")}
            md.append(f"| {s['step']} | {s['name']} | {'✅' if s['ok'] else '❌'} | {json.dumps(det, ensure_ascii=False)[:220]} |")
        md += ["", "## ARC-002 / XVER (фрагмент)", "",
               f"RAW: {json.dumps(xv['stages']['raw']['summary'], ensure_ascii=False)}",
               f"ALIGNED: {json.dumps(xv['stages']['aligned']['summary'], ensure_ascii=False)} — вердикт **{xv['verdict']}**", "",
               f"Полный протокол сверки: {jp.replace('UNIF_E2E', 'ARC002_XVER')} (генерируется стендом xverify.py).", ""]
        mp = jp.replace(".json", ".md")
        with open(mp, "w", encoding="utf-8") as f:
            f.write("\n".join(md))

        print(f"E2E: вердикт {proto['verdict']} · статус плана {resp.get('status')} грейд {(resp.get('metrics') or {}).get('grade')} · "
              f"расчёт {dt_ms:.0f} мс · детерминизм {'ok' if det_ok else 'НАРУШЕН'} · XVER {xv['verdict']}")
        print(f"  протокол: {jp}")
        exit_code = 0 if proto["verdict"] == "PASS" else 1
    except Eng6ServiceError as e:
        print(f"E2E: отказ контура: {e}", file=sys.stderr)
        exit_code = 2
    finally:
        if proc and proc.poll() is None:
            if hasattr(os, "killpg"):
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            else:
                proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
