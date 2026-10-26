"""Интеграционные тесты моста: реальный сервис ENG6-PLAN (поднимается в тесте).

Требуют node ≥ 20.11. Сервис стартует на свободном порту, после тестов гасится.
Покрытием служат: конформанс-гейт (К-04), plan/sync, детерминированность (NFR-04),
обратный маппинг в таблицы платформы, аудит.
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import unittest

BRIDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BRIDGE)
REPO = os.path.dirname(os.path.dirname(BRIDGE))
ENG6_DIR = os.path.join(REPO, "eng6-service")

from datamodel_map import plan_to_platform_tables  # noqa: E402
from eng6_js_client import Eng6JsClient, Eng6ServiceError  # noqa: E402

HAS_NODE = shutil.which("node") is not None
VENDOR_SHA256 = "adb610c9eee1ca17b257cbc6087e2278fdde3eef92f3b6a360cfb51968d48b20"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(HAS_NODE and os.path.isdir(ENG6_DIR), "node или eng6-service недоступны")
class TestEng6ServiceIntegration(unittest.TestCase):
    proc = None
    port = None

    @classmethod
    def setUpClass(cls):
        cls.port = _free_port()
        env = dict(os.environ, ENG6_PORT=str(cls.port), ENG6_HOST="127.0.0.1",
                   ENG6_SERVE_UI="false", ENG6_LOG_LEVEL="warn")
        cls.proc = subprocess.Popen(
            ["node", "api/main.mjs"], cwd=ENG6_DIR, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid if hasattr(os, "setsid") else None)
        cls.client = Eng6JsClient(f"http://127.0.0.1:{cls.port}", requestor="bridge-test")
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if cls.client.ready():
                break
            if cls.proc.poll() is not None:
                raise RuntimeError("сервис ENG6-PLAN завершился при старте")
            time.sleep(0.3)
        else:
            raise RuntimeError("сервис ENG6-PLAN не поднялся за 45 с")

    @classmethod
    def tearDownClass(cls):
        if cls.proc and cls.proc.poll() is None:
            if hasattr(os, "killpg"):
                os.killpg(os.getpgid(cls.proc.pid), signal.SIGTERM)
            else:
                cls.proc.terminate()
            try:
                cls.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                cls.proc.kill()

    def example(self, name: str):
        with open(os.path.join(ENG6_DIR, "examples", name), encoding="utf-8") as f:
            return json.load(f)

    def test_01_conformance_gate(self):
        proto = self.client.conformance_gate()
        self.assertTrue(proto["ok"])
        self.assertEqual(proto.get("selfTestTotal") or proto.get("selfTest", {}).get("total"), 33)

    def test_02_meta_vendor_sha256(self):
        meta = self.client.meta()
        core = meta.get("core") or {}
        self.assertEqual(core.get("sha256"), VENDOR_SHA256)

    def test_03_plan_sync_battalion(self):
        resp = self.client.plan_sync(self.example("battalion.json"))
        self.assertIn(resp["status"], ("OPTIMAL", "FEASIBLE", "PARTIAL"))
        self.assertEqual(resp["status"], "PARTIAL")            # эталон README/selfcheck
        self.assertEqual(resp["metrics"]["grade"], "B")
        tables = plan_to_platform_tables(resp, run_id="TEST-RUN-01")
        self.assertTrue(tables["link_line"])
        self.assertTrue(tables["route"])
        self.assertEqual(tables["calc_run"][0]["run_id"], "TEST-RUN-01")

    def test_04_determinism_nfr04(self):
        req = self.example("battalion.json")
        hdr = {"X-Request-Id": "bridge-determinism-check-0001"}
        r1 = self.client._request("POST", "/api/v1/plan/sync", req, hdr)
        r2 = self.client._request("POST", "/api/v1/plan/sync", req, hdr)

        # Контракт детерминированности линии B (тест NFR-DET-01 в planner_reference.test.mjs):
        # побайтово идентичны ВСЕ артефакты плана, кроме замерных величин (timings,
        # schedule.criticalPath) и идентификатора конверта запроса.
        def strip(resp):
            c = json.loads(json.dumps(resp))
            c.pop("timings", None)
            c.pop("id", None)
            c.pop("requestId", None)
            c.pop("taskId", None)
            sch = (c.get("plan") or {}).get("schedule")
            if sch:
                sch.pop("criticalPath", None)
            return json.dumps(c, sort_keys=True)

        self.assertEqual(strip(r1), strip(r2))
        self.assertEqual(r1["metrics"]["objectiveValue"], r2["metrics"]["objectiveValue"])

    def test_05_invalid_input_422(self):
        bad = {"topology": {"nodes": [{"id": "a", "type": "cp", "lat": 999, "lng": 0}], "links": []}}
        with self.assertRaises(Eng6ServiceError) as c:
            self.client.plan_sync(bad)
        self.assertEqual(c.exception.http_status, 422)

    def test_06_async_roundtrip(self):
        accepted = self.client.plan_async(self.example("battalion.json"))
        self.assertIn("taskId", accepted)
        done = self.client.wait_task(accepted["taskId"], timeout_s=120)
        self.assertEqual(done.get("state") or done.get("status"), "SUCCEEDED")

    def test_07_audit_has_runs(self):
        audit = self.client.audit(limit=5)
        items = audit.get("items") or audit.get("entries") or []
        self.assertTrue(len(items) >= 1)


if __name__ == "__main__":
    unittest.main()
