"""Live-интеграция: платформа «Афина» + контур ENG6-PLAN + сшивка журналов.

Поднимаются РЕАЛЬНЫЕ серверы обеих линий (свободные порты, временные БД):
  1. bootstrap администратора платформы (первый пользователь);
  2. обстановка создаётся через API платформы (POST /objects: COM/LINK/ORDER/ORG);
  3. scenario_export: объекты платформы → контракт ENG6-PLAN (UNIF-18);
  4. PLAN-01 через штатную CalcQueue платформы → паспорт + БД + леджер (UNIF-11/12);
  5. cross_anchor: мастер-цепочка платформы + NDJSON контура + единый леджер (UNIF-17).

Требуют node ≥ 20.11 и python ≥ 3.11.
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
from master_chain_sync import cross_anchor  # noqa: E402
from plan_service import UnifiedPlanService, submit_plan, wait_job  # noqa: E402
from platform_client import PlatformClient  # noqa: E402
from scenario_export import export_request  # noqa: E402

HAS_NODE = shutil.which("node") is not None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_http(url: str, timeout_s: float = 60.0) -> bool:
    import urllib.request
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if urllib.request.urlopen(url, timeout=3).status == 200:
                return True
        except Exception:
            time.sleep(0.4)
    return False


@unittest.skipUnless(HAS_NODE and os.path.isdir(ENG6_DIR), "node или eng6-service недоступны")
class TestPlatformBridgeLive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="afina-bridge-live-")
        # --- контур ENG6-PLAN ---
        cls.eng_port = _free_port()
        env = dict(os.environ, ENG6_PORT=str(cls.eng_port), ENG6_HOST="127.0.0.1",
                   ENG6_SERVE_UI="false", ENG6_LOG_LEVEL="warn")
        cls.eng = subprocess.Popen(["node", "api/main.mjs"], cwd=ENG6_DIR, env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   preexec_fn=os.setsid if hasattr(os, "setsid") else None)
        # --- платформа «Афина» ---
        cls.plat_port = _free_port()
        cls.plat_db = os.path.join(cls.tmp, "afina.db")
        cls.plat = subprocess.Popen(
            [sys.executable, "-m", "afina.server.app", "--db", cls.plat_db, "--port", str(cls.plat_port)],
            cwd=AFINA_SRC, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid if hasattr(os, "setsid") else None)
        ok_eng = _wait_http(f"http://127.0.0.1:{cls.eng_port}/healthz")
        ok_plat = _wait_http(f"http://127.0.0.1:{cls.plat_port}/healthz")
        if not (ok_eng and ok_plat):
            raise RuntimeError(f"не поднялись сервисы: engine={ok_eng} platform={ok_plat}")

        cls.plat_url = f"http://127.0.0.1:{cls.plat_port}"
        cls.eng_url = f"http://127.0.0.1:{cls.eng_port}"
        # роль «Администратор системы»: audit_read, регистрация пользователей (bootstrap)
        cls.pc = PlatformClient(cls.plat_url)
        cls.pc.bootstrap_admin("unified-admin")
        # роль «Инженер-расчётчик»: edit_models_cond (создание объектов обстановки)
        cls.eng_password = "eng-" + os.urandom(8).hex()
        cls.pc.register("unified-eng", cls.eng_password, "Инженер-расчётчик", "Инженер единой программы")
        cls.pc_eng = PlatformClient(cls.plat_url)
        cls.pc_eng.login("unified-eng", cls.eng_password)

        # очередь платформы + единый сервис планирования
        cls.queue = CalcQueue(persist_path=os.path.join(cls.tmp, "queue.json"), workers=1)
        cls.svc = UnifiedPlanService(os.path.join(cls.tmp, "unified.db"), cls.eng_url,
                                     ledger_path=os.path.join(cls.tmp, "unified_audit.ndjson"),
                                     requestor="bridge-live")
        cls.svc.register(cls.queue)

    @classmethod
    def tearDownClass(cls):
        cls.queue.stop()
        for p in (cls.eng, cls.plat):
            if p.poll() is None:
                if hasattr(os, "killpg"):
                    os.killpg(os.getpgid(p.pid), signal.SIGTERM)
                else:
                    p.terminate()
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_01_bootstrap_and_session(self):
        self.assertEqual(self.pc.identity["role"], "Администратор системы")
        self.assertEqual(self.pc_eng.identity["role"], "Инженер-расчётчик")
        # audit_read доступен администратору (RBAC Прил. Н)
        v = self.pc.audit_verify()
        self.assertTrue(v["ok"], v)
        # наименьшие полномочия: инженеру аудит недоступен, админу — создание объектов
        from platform_client import PlatformError
        with self.assertRaises(PlatformError) as c1:
            self.pc_eng.audit_verify()
        self.assertEqual(c1.exception.http_status, 403)
        with self.assertRaises(PlatformError) as c2:
            self.pc.create_object({"id": "x", "class": "COM", "name": "x", "x": 0, "y": 0})
        self.assertEqual(c2.exception.http_status, 403)

    def test_02_export_platform_objects_to_scenario(self):
        # обстановка создаётся ЧЕРЕЗ API платформы (аудит-цепочка пополняется)
        objs = [
            {"id": "n-kp", "class": "COM", "name": "КП батальона", "x": 37.620000, "y": 55.750000,
             "attrs": {"node_type": "cp", "org_unit": "u-bat",
                                  "unit": {"inventoryId": "КП-001", "model": "П-240Б",
                                           "techState": "исправна", "cost": 40, "deployTimeMin": 45}}},
            {"id": "n-rl", "class": "COM", "name": "Ретранслятор", "x": 37.720000, "y": 55.800000,
             "attrs": {"node_type": "relay", "org_unit": "u-bat",
                                  "unit": {"inventoryId": "РЛ-002", "model": "Р-419Л1",
                                           "techState": "ограниченно пригодна", "cost": 30, "deployTimeMin": 35}}},
            {"id": "n-r1", "class": "COM", "name": "РС-003", "x": 37.660000, "y": 55.830000,
             "attrs": {"node_type": "radio", "org_unit": "u-bat",
                                  "unit": {"inventoryId": "РС-003", "model": "Р-168-100У",
                                           "techState": "исправна", "cost": 10, "deployTimeMin": 15}}},
            {"id": "n-r2", "class": "COM", "name": "РС-006 (неисправна)", "x": 37.700000, "y": 55.740000,
             "attrs": {"node_type": "radio", "org_unit": "u-bat",
                                  "unit": {"inventoryId": "РС-006", "model": "Р-168-25У",
                                           "techState": "неисправна", "cost": 8, "deployTimeMin": 12}}},
            {"id": "l-kp-rl", "class": "LINK", "name": "КП—РЛ", "x": 0, "y": 0,
             "attrs": {"a": "n-kp", "b": "n-rl", "link_type": "VHF"}},
            {"id": "l-rl-r1", "class": "LINK", "name": "РЛ—РС3", "x": 0, "y": 0,
             "attrs": {"a": "n-rl", "b": "n-r1", "link_type": "VHF"}},
            {"id": "l-kp-r2", "class": "LINK", "name": "КП—РС6", "x": 0, "y": 0,
             "attrs": {"a": "n-kp", "b": "n-r2", "link_type": "VHF"}},
            {"id": "d-r1", "class": "ORDER", "name": "Заявка КП→РС3", "x": 0, "y": 0,
             "attrs": {"src": "n-kp", "dst": "n-r1", "rate_kbps": 16, "priority": 1}},
            {"id": "d-r2", "class": "ORDER", "name": "Заявка КП→РС6", "x": 0, "y": 0,
             "attrs": {"src": "n-kp", "dst": "n-r2", "rate_kbps": 16, "priority": 2}},
            {"id": "u-bat", "class": "ORG", "name": "батальон", "x": 0, "y": 0,
             "attrs": {"org_id": "u-bat", "path": "батальон", "node_id": "n-kp"}},
        ]
        for o in objs:
            self.pc_eng.create_object(o)          # edit_models_cond — роль «Инженер-расчётчик»
        res = export_request(self.pc_eng)
        c = res["counts"]
        self.assertEqual(c["nodes"], 4, res["warnings"])
        self.assertEqual(c["links"], 3)
        self.assertEqual(c["demands"], 2)
        self.assertEqual(c["assets"], 4)
        # неисправная техника → red (FR-RES-03 семантика сохранена при экспорте)
        red = [a for a in res["entities"]["assets"] if a["techState"] == "неисправна"]
        self.assertEqual(len(red), 1)
        # ОШС: org_unit u-bat представлен узлом n-kp → parentId у подчинённых
        req = res["request"]
        by_id = {n["id"]: n for n in req["topology"]["nodes"]}
        self.assertEqual(by_id["n-rl"].get("parentId"), "n-kp")
        self.assertNotIn("parentId", by_id["n-kp"])  # самоссылка исключена
        TestPlatformBridgeLive.exported = res

    def test_03_plan_via_platform_queue(self):
        res = getattr(TestPlatformBridgeLive, "exported", None) or export_request(self.pc_eng)
        ent = res["entities"]
        payload = {k: ent[k] for k in ("assets", "geo_objects", "relations", "orders")}
        payload["org_units"] = ent["org_units"]
        payload["constraints"] = {"budget": 200, "physics": "fast", "maxSimultaneousDeploy": 2}
        payload["environment"] = {"clutterType": "open", "terrain": {"source": "flat"}}
        payload["actor"] = "unified-admin"
        jid = submit_plan(self.queue, payload)
        job = wait_job(self.queue, jid, timeout_s=120)
        self.assertEqual(job["status"], "done", job.get("error"))
        with open(job["result_ref"], encoding="utf-8") as f:
            out = json.load(f)
        self.assertIn(out["status"], ("PARTIAL", "FEASIBLE", "OPTIMAL"))
        # red-техника не размещена → заявка d-r2 отклонена → PARTIAL (FR-RES-03)
        self.assertEqual(out["status"], "PARTIAL")
        TestPlatformBridgeLive.plan_out = out

    def test_04_cross_anchor_three_journals(self):
        out = TestPlatformBridgeLive.plan_out
        proto = cross_anchor(self.svc.ledger, self.pc, self.svc.client,
                             run_id=out["run_id"], engine_task_id=out["engine_task_id"],
                             input_sha256=out["engine_input_sha256"], actor="unified-admin")
        self.assertEqual(proto["verdict"], "ANCHORED", json.dumps(proto["reasons"], ensure_ascii=False))
        self.assertTrue(proto["platform"]["verify"]["ok"])
        self.assertGreaterEqual(proto["platform"]["entries"], 5)   # bootstrap + создание объектов
        self.assertGreaterEqual(proto["engine"]["matched"], 1)     # сквозная связка по inputSha256
        self.assertTrue(proto["unified_ledger"]["verify"]["ok"])
        # мастер-цепочка платформы не повреждена после всех операций
        self.assertTrue(self.pc.audit_verify()["ok"])
        # протокол — в артефакты (детерминированная часть)
        out_dir = os.path.join(os.path.dirname(BRIDGE), "protocols")
        os.makedirs(out_dir, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(out_dir, f"ПРОТОКОЛ_CROSS_ANCHOR_{ts}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(proto, f, ensure_ascii=False, indent=1)
        TestPlatformBridgeLive.anchor_proto_path = path

    def test_05_unified_db_links_everything(self):
        out = TestPlatformBridgeLive.plan_out
        conn = platform_db.connect(self.svc.db_path)
        try:
            run = platform_db.load_run(conn, out["run_id"])
            self.assertEqual(run["engine_input_sha256"], out["engine_input_sha256"])
            v = platform_db.verify_run(conn, out["run_id"])
            self.assertTrue(v["ok"], v)
            # паспорт: оба вычислительных пути
            self.assertIn("ENG6-PLAN-service", run["passport"]["environment"]["backend"])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
