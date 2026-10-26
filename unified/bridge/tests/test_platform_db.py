"""Тесты персистентности результатов планирования (platform_db, UNIF-12)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import platform_db  # noqa: E402
from datamodel_map import plan_to_platform_tables  # noqa: E402

RESP = {
    "status": "PARTIAL",
    "core": {"engineVersion": "5.1.0", "engineSha256": "adb610c9" + "0" * 56, "planner": "1.0.0"},
    "timings": {"totalMs": 12.3},
    "metrics": {"grade": "B", "objectiveValue": 0.2, "demandsTotal": 2, "demandsSatisfied": 1},
    "plan": {
        "deployedUnits": [{"nodeId": "r2", "nodeType": "radio", "unitId": "Р-168-001",
                           "condition": "green", "lat": 56.3, "lng": 41.3, "cost": 10,
                           "wave": 1, "startMin": 0}],
        "activeLinks": [{"id": "l2", "from": "r1", "to": "r2", "type": "VHF", "distKm": 16.6,
                         "marginDb": 12.0, "capacityKbps": 100, "loadKbps": 64, "utilPct": 64,
                         "delayMs": 5.1, "availabilityPct": 99.0, "cost": 0, "role": "primary"}],
        "rejectedLinks": [{"id": "l9", "from": "a", "to": "b", "type": "UHF",
                           "code": "BELOW_MARGIN", "reason": "запас ниже порога", "marginDb": -3}],
        "routes": [
            {"demandId": "d1", "status": "ROUTED", "grantedKbps": 64, "deficitKbps": 0,
             "delayMs": 5.1, "hops": 2, "reason": None, "path": ["kp", "r1", "r2"], "links": ["l1", "l2"]},
            {"demandId": "d2", "status": "REJECTED", "grantedKbps": 0, "deficitKbps": 16,
             "delayMs": None, "hops": 0, "reason": "CAPACITY_SHORTAGE"},
        ],
        "schedule": {"makespanMin": 60.0, "crews": 2, "crewUtilPct": 55.0,
                     "criticalPath": [{"id": "kp"}, {"id": "r1"}],
                     "waves": [{"index": 1, "tStartMin": 0, "tEndMin": 60, "durationMin": 60,
                                "crewsUsed": 2,
                                "items": [{"nodeId": "kp", "unitId": "УШБ-003", "crew": 1,
                                           "startMin": 0, "endMin": 45}]}]},
        "survivability": {"spofCount": 1},
    },
    "diagnostics": {"issues": [{"sev": "warn", "code": "BRIDGE_LINK", "message": "мост"}]},
}
REQUEST = {"topology": {"nodes": [{"id": "kp", "lat": 56.1, "lng": 41.0}], "links": []}}
PASSPORT = {"run_id": "CALC-2026-DBTEST", "function": "PLAN-01", "status": "done"}


class TestPlatformDb(unittest.TestCase):
    def setUp(self):
        self.conn = platform_db.connect(":memory:")
        self.tables = plan_to_platform_tables(RESP, run_id="CALC-2026-DBTEST")
        self.hashes = platform_db.save_run(self.conn, "CALC-2026-DBTEST", REQUEST, RESP,
                                           PASSPORT, self.tables, engine_task_id="sync_x_1",
                                           engine_input_sha256="e" * 64)

    def tearDown(self):
        self.conn.close()

    def test_roundtrip(self):
        run = platform_db.load_run(self.conn, "CALC-2026-DBTEST")
        self.assertIsNotNone(run)
        self.assertEqual(run["status"], "PARTIAL")
        self.assertEqual(run["grade"], "B")
        self.assertEqual(run["engine_input_sha256"], "e" * 64)
        self.assertEqual(run["passport"]["run_id"], "CALC-2026-DBTEST")
        self.assertEqual(len(run["route"]), 2)
        self.assertEqual(run["link_line"][0]["line_id"], "l2")
        self.assertEqual(run["issue"][0]["code"], "BRIDGE_LINK")
        listed = platform_db.list_runs(self.conn)
        self.assertEqual(listed[0]["run_id"], "CALC-2026-DBTEST")

    def test_verify_ok(self):
        v = platform_db.verify_run(self.conn, "CALC-2026-DBTEST", RESP)
        self.assertTrue(v["ok"], v)
        self.assertEqual(v["rows"]["route"], 2)

    def test_verify_detects_response_substitution(self):
        fake = dict(RESP, status="OPTIMAL")
        v = platform_db.verify_run(self.conn, "CALC-2026-DBTEST", fake)
        self.assertFalse(v["ok"])
        self.assertEqual(v["reason"], "HASH_MISMATCH")

    def test_verify_detects_row_tampering(self):
        # подмена строки маршрута в БД (без изменения ответа) → TABLES_MISMATCH
        self.conn.execute("UPDATE route SET status='ROUTED', granted_kbps=64 WHERE demand_id='d2'")
        self.conn.commit()
        v = platform_db.verify_run(self.conn, "CALC-2026-DBTEST", RESP)
        self.assertFalse(v["ok"])
        self.assertEqual(v["reason"], "TABLES_MISMATCH")

    def test_unknown_run(self):
        self.assertEqual(platform_db.verify_run(self.conn, "НЕТ")["reason"], "RUN_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
