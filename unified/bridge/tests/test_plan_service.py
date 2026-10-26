"""Интеграционные тесты PLAN-01 в штатной очереди платформы (plan_service, UNIF-11…13).

Поднимает реальный ENG6-PLAN (node) и прогоняет полный цикл:
  CalcQueue(платформа) → мост → контур → паспорт → БД → аудит-леджер → якорь.
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

BRIDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BRIDGE)
REPO = os.path.dirname(os.path.dirname(BRIDGE))
ENG6_DIR = os.path.join(REPO, "eng6-service")
AFINA_SRC = os.path.join(REPO, "afina", "src")
sys.path.insert(0, AFINA_SRC)

from afina.eng6.runner import CalcQueue  # noqa: E402
import platform_db  # noqa: E402
from plan_service import UnifiedPlanService, submit_plan, wait_job  # noqa: E402

HAS_NODE = shutil.which("node") is not None

# ---------- синтетическая обстановка (сущности Прил. Г) ----------
ORG_UNITS = {"u-bat": {"path": "батальон", "nodeId": "kp"}}
ASSETS = [
    {"inventoryId": "КП-001", "model": "П-240Б", "nodeType": "cp", "techState": "исправна",
     "cost": 30, "deployTimeMin": 45},
    {"inventoryId": "РС-003", "model": "Р-168-100У", "nodeType": "radio", "techState": "исправна",
     "cost": 10, "deployTimeMin": 15},
    {"inventoryId": "РС-006", "model": "Р-168-25У", "nodeType": "radio", "techState": "неисправна",
     "cost": 8, "deployTimeMin": 12},
]
GEO = [
    {"id": "kp", "type": "cp", "lat": 55.750000, "lng": 37.620000, "unitId": "КП-001", "orgUnitId": "u-bat"},
    {"id": "r1", "type": "radio", "lat": 55.830000, "lng": 37.660000, "unitId": "РС-003", "orgUnitId": "u-bat"},
    {"id": "r2", "type": "radio", "lat": 55.770000, "lng": 37.760000, "unitId": "РС-006", "orgUnitId": "u-bat"},
]
RELS = [{"id": "l1", "a": "kp", "b": "r1", "linkType": "VHF"},
        {"id": "l2", "a": "kp", "b": "r2", "linkType": "VHF"}]
ORDERS = [{"id": "d1", "src": "kp", "dst": "r1", "rateKbps": 16, "priority": 1},
          {"id": "d2", "src": "kp", "dst": "r2", "rateKbps": 16, "priority": 2}]
PAYLOAD = {
    "assets": ASSETS, "geo_objects": GEO, "relations": RELS, "orders": ORDERS,
    "org_units": ORG_UNITS,
    "constraints": {"budget": 200, "physics": "fast", "maxSimultaneousDeploy": 2},
    "environment": {"clutterType": "open", "terrain": {"source": "flat"}},
    "actor": "Петров П.П.",
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(HAS_NODE and os.path.isdir(ENG6_DIR), "node или eng6-service недоступны")
class TestPlanServiceInQueue(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = _free_port()
        env = dict(os.environ, ENG6_PORT=str(cls.port), ENG6_HOST="127.0.0.1",
                   ENG6_SERVE_UI="false", ENG6_LOG_LEVEL="warn")
        cls.proc = subprocess.Popen(["node", "api/main.mjs"], cwd=ENG6_DIR, env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    preexec_fn=os.setsid if hasattr(os, "setsid") else None)
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        import urllib.request
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            try:
                if urllib.request.urlopen(cls.base_url + "/readyz", timeout=2).status == 200:
                    break
            except Exception:
                if cls.proc.poll() is not None:
                    raise RuntimeError("сервис ENG6-PLAN завершился при старте")
            time.sleep(0.3)
        else:
            raise RuntimeError("сервис ENG6-PLAN не поднялся за 45 с")

        cls.tmp = tempfile.mkdtemp(prefix="afina-unified-")
        cls.db_path = os.path.join(cls.tmp, "afina.db")
        cls.ledger_path = os.path.join(cls.tmp, "unified_audit.ndjson")
        cls.queue = CalcQueue(persist_path=os.path.join(cls.tmp, "queue.json"), workers=1)
        cls.svc = UnifiedPlanService(cls.db_path, cls.base_url,
                                     ledger_path=cls.ledger_path, requestor="queue-test")
        cls.svc.register(cls.queue)

    @classmethod
    def tearDownClass(cls):
        cls.queue.stop()
        if cls.proc.poll() is None:
            if hasattr(os, "killpg"):
                os.killpg(os.getpgid(cls.proc.pid), signal.SIGTERM)
            else:
                cls.proc.terminate()
            try:
                cls.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                cls.proc.kill()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _run_job(self, payload):
        jid = submit_plan(self.queue, payload)
        job = wait_job(self.queue, jid, timeout_s=120)
        self.assertEqual(job["status"], "done", job.get("error"))
        with open(job["result_ref"], encoding="utf-8") as f:
            return json.load(f)

    def test_01_full_cycle_through_platform_queue(self):
        res = self._run_job(PAYLOAD)
        # статус: red-техника (РС-006) не размещается → d2 отклонена → PARTIAL (FR-RES-03)
        self.assertIn(res["status"], ("PARTIAL", "FEASIBLE", "OPTIMAL"))
        self.assertTrue(res["run_id"].startswith("CALC-"))
        self.assertIsNotNone(res["engine_input_sha256"])
        # паспорт: оба вычислительных пути
        env = res["passport"]["environment"]
        self.assertIn("ENG6-PLAN-service", env["backend"])
        self.assertIn("ENG6-JS-vendor-probe", env["backend"])
        self.assertIn("adb610c9", env["backends"])
        # таблицы К-03 заполнены
        self.assertGreaterEqual(res["tables"].get("route", 0), 2)
        self.assertGreaterEqual(res["tables"].get("map_object", 0), 2)
        # якорь аудита: запись контура с нашим inputSha256 найдена
        self.assertGreaterEqual(res["anchors"].get("matched") or 0, 1,
                                f"якорь не совпал: {res['anchors']}")
        TestPlanServiceInQueue.res1 = res

    def test_02_db_persisted_and_verifiable(self):
        res = getattr(TestPlanServiceInQueue, "res1", None) or self._run_job(PAYLOAD)
        conn = platform_db.connect(self.db_path)
        try:
            run = platform_db.load_run(conn, res["run_id"])
            self.assertIsNotNone(run)
            self.assertEqual(run["engine_input_sha256"], res["engine_input_sha256"])
            self.assertEqual(run["passport"]["function"], "PLAN-01")
            v = platform_db.verify_run(conn, res["run_id"])
            self.assertTrue(v["ok"], v)
            runs = platform_db.list_runs(conn)
            self.assertTrue(any(r["run_id"] == res["run_id"] for r in runs))
        finally:
            conn.close()

    def test_03_ledger_chain_valid(self):
        from audit_bridge import UnifiedAuditLedger
        led = UnifiedAuditLedger(self.ledger_path)
        v = led.verify()
        self.assertTrue(v["ok"], v)
        events = [e["event"] for e in led.entries()]
        self.assertIn("plan_completed", events)
        self.assertIn("engine_audit_anchored", events)
        actor_ok = any(e.get("actor") == "Петров П.П." for e in led.entries())
        self.assertTrue(actor_ok, "X-Requestor/actor не донесён до леджера")

    def test_04_determinism_same_payload(self):
        res2 = self._run_job(PAYLOAD)
        res1 = getattr(TestPlanServiceInQueue, "res1", None)
        if res1:
            # идентичный вход → идентичный канонический хэш ядра (NFR-04 на уровне аудита)
            self.assertEqual(res1["engine_input_sha256"], res2["engine_input_sha256"])
            self.assertNotEqual(res1["run_id"], res2["run_id"])  # запуски различимы


if __name__ == "__main__":
    unittest.main()
