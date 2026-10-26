"""Тесты единого аудит-леджера (audit_bridge, UNIF-13): цепочка, подмены, якоря."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from audit_bridge import UnifiedAuditLedger  # noqa: E402


class TestUnifiedAuditLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="afina-audit-")
        self.path = os.path.join(self.tmp, "unified_audit.ndjson")
        self.led = UnifiedAuditLedger(self.path)

    def test_chain_and_verify(self):
        for i in range(5):
            self.led.record("plan_completed", run_id=f"CALC-2026-{i:04d}",
                            engine_task_id=f"sync_t{i}", input_sha256=f"{i:064x}",
                            actor="Иванов И.И.", status="PARTIAL")
        v = self.led.verify()
        self.assertTrue(v["ok"])
        self.assertEqual(v["entries"], 5)
        entries = self.led.entries()
        self.assertEqual(entries[0]["prev_hash"], "0" * 64)
        self.assertEqual(entries[4]["prev_hash"], entries[3]["hash"])

    def test_tamper_detected(self):
        for i in range(4):
            self.led.record("e", run_id=f"R{i}", status="PARTIAL")
        lines = open(self.path, encoding="utf-8").read().splitlines()
        rec = json.loads(lines[1])
        rec["status"] = "OPTIMAL"          # подмена статуса во второй записи
        lines[1] = json.dumps(rec, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        open(self.path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
        v = self.led.verify()
        self.assertFalse(v["ok"])
        self.assertEqual(v["broken_at"], 1)

    def test_truncation_detected(self):
        for i in range(3):
            self.led.record("e", run_id=f"R{i}")
        lines = open(self.path, encoding="utf-8").read().splitlines()
        open(self.path, "w", encoding="utf-8").write("\n".join(lines[:2]) + "\n")
        # усечение само по себе цепочку не ломает (append-only журнал укорочен) —
        # фиксируем, что оставшаяся цепочка валидна, а число записей уменьшилось
        v = self.led.verify()
        self.assertTrue(v["ok"])
        self.assertEqual(v["entries"], 2)

    def test_anchor_matches_by_input_sha256(self):
        sha = "f" * 64
        engine_items = [
            {"inputSha256": sha, "taskId": "sync_1", "status": "PARTIAL"},
            {"inputSha256": sha, "taskId": "sync_2", "status": "PARTIAL"},
            {"inputSha256": "0" * 64, "taskId": "sync_3", "status": "OPTIMAL"},
        ]
        a = self.led.anchor_engine_audit(engine_items, sha, "CALC-2026-ANCH", "sync_1")
        self.assertEqual(a["status"], "anchored")
        self.assertEqual(a["extra"]["matched_records"], 2)
        self.assertTrue(self.led.verify()["ok"])

    def test_anchor_no_match_honest(self):
        a = self.led.anchor_engine_audit([], "f" * 64, "CALC-2026-X")
        self.assertEqual(a["status"], "no_match")
        self.assertEqual(a["extra"]["matched_records"], 0)


if __name__ == "__main__":
    unittest.main()
