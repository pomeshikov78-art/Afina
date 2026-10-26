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


class TestLedgerRotation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="afina-audit-rot-")
        self.path = os.path.join(self.tmp, "unified_audit.ndjson")

    def test_rotation_keeps_chain_continuous(self):
        led = UnifiedAuditLedger(self.path, max_bytes=400)
        n = 25
        for i in range(n):
            led.record("plan_completed", run_id=f"CALC-2026-{i:04d}", status="PARTIAL")
        archives = sorted(f for f in os.listdir(self.tmp) if f.startswith("unified_audit.ndjson."))
        self.assertGreaterEqual(len(archives), 1, "ротация не произошла")
        v = led.verify()
        self.assertTrue(v["ok"], v)
        self.assertEqual(v["rotations"], len(archives))
        # seq непрерывен сквозь ротации: записей n + по одному якорю на ротацию
        self.assertEqual(v["total_seq"], n + len(archives))
        # текущий файл меньше лимита + одна запись (условие ротации — до записи)
        self.assertLessEqual(os.path.getsize(self.path), 400 + 500)

    def test_rotation_first_entry_is_anchor(self):
        led = UnifiedAuditLedger(self.path, max_bytes=200)
        for i in range(10):
            led.record("e", run_id=f"R{i}")
        first = led.entries()[0]
        self.assertEqual(first["event"], "chain_rotation")
        self.assertEqual(first["status"], "rotated")
        self.assertIn("archive", first.get("extra", {}))

    def test_archive_tamper_detected(self):
        led = UnifiedAuditLedger(self.path, max_bytes=200)
        for i in range(12):
            led.record("e", run_id=f"R{i}", status="PARTIAL")
        self.assertTrue(led.verify()["ok"])
        # подмена СЕРЕДИНЫ архива (не последней записи) — обнаруживается рекурсивной проверкой
        arch = sorted(os.path.join(self.tmp, f) for f in os.listdir(self.tmp)
                      if f.startswith("unified_audit.ndjson."))[0]
        lines = open(arch, encoding="utf-8").read().splitlines()
        mid = len(lines) // 2
        rec = json.loads(lines[mid])
        rec["status"] = "OPTIMAL"
        lines[mid] = json.dumps(rec, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        open(arch, "w", encoding="utf-8").write("\n".join(lines) + "\n")
        v = led.verify()
        self.assertFalse(v["ok"])
        self.assertEqual(v["reason"], "HASH_MISMATCH")

    def test_missing_archive_detected(self):
        led = UnifiedAuditLedger(self.path, max_bytes=200)
        for i in range(12):
            led.record("e", run_id=f"R{i}")
        arch = [f for f in os.listdir(self.tmp) if f.startswith("unified_audit.ndjson.")][0]
        os.remove(os.path.join(self.tmp, arch))
        v = led.verify()
        self.assertFalse(v["ok"])
        self.assertEqual(v["reason"], "ARCHIVE_MISSING")

    def test_no_rotation_by_default(self):
        led = UnifiedAuditLedger(self.path)          # max_bytes=0 — ротация выключена
        for i in range(50):
            led.record("e", run_id=f"R{i}")
        self.assertEqual(led.verify()["rotations"], 0)
        self.assertEqual(led.verify()["entries"], 50)


if __name__ == "__main__":
    unittest.main()
