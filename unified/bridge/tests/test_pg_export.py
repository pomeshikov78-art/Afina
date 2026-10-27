"""Тесты миграционного комплекта SQLite → PostgreSQL (pg_export, UNIF-22) — офлайн."""
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest

BRIDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BRIDGE)

import pg_export  # noqa: E402
import platform_db  # noqa: E402
from audit_bridge import UnifiedAuditLedger  # noqa: E402
from datamodel_map import plan_to_platform_tables  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_platform_db import RESP, REQUEST, PASSPORT  # noqa: E402


class TestPgExport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="afina-pg-")
        self.db_path = os.path.join(self.tmp, "unified.db")
        self.conn = platform_db.connect(self.db_path)
        self.tables = plan_to_platform_tables(RESP, run_id="CALC-2026-PG")
        platform_db.save_run(self.conn, "CALC-2026-PG", REQUEST, RESP, PASSPORT,
                             self.tables, engine_task_id="sync_pg_1", engine_input_sha256="a" * 64)

    def tearDown(self):
        self.conn.close()

    def test_export_sql_content_and_determinism(self):
        out = os.path.join(self.tmp, "export.sql")
        info1 = pg_export.export_sqlite(self.conn, out)
        sql = open(out, encoding="utf-8").read()
        self.assertIn("BEGIN;", sql)
        self.assertIn("COMMIT;", sql)
        self.assertIn("INSERT INTO plan_runs", sql)
        self.assertIn("CALC-2026-PG", sql)
        self.assertIn("ON CONFLICT DO NOTHING", sql)          # идемпотентность keyed
        self.assertIn("DELETE FROM route WHERE run_id =", sql)  # предочистка append
        self.assertIn("::jsonb", sql)                          # JSONB-литералы
        self.assertEqual(info1["rows"]["route"], 2)
        self.assertEqual(info1["rows"]["plan_runs"], 1)
        # детерминированность: повторный экспорт — тот же sha256
        out2 = os.path.join(self.tmp, "export2.sql")
        info2 = pg_export.export_sqlite(self.conn, out2)
        self.assertEqual(info1["sha256"], info2["sha256"])

    def test_ledger_export_with_rotation(self):
        led_path = os.path.join(self.tmp, "unified_audit.ndjson")
        led = UnifiedAuditLedger(led_path, max_bytes=300)
        for i in range(20):
            led.record("plan_completed", run_id=f"CALC-2026-{i:04d}", status="PARTIAL")
        self.assertTrue(led.verify()["ok"])
        out = os.path.join(self.tmp, "export_ledger.sql")
        info = pg_export.export_ledger(led_path, out)
        sql = open(out, encoding="utf-8").read()
        # все записи (включая якоря ротаций) экспортированы, seq уникальны
        n_inserts = sql.count("INSERT INTO unified_audit_entry")
        self.assertEqual(n_inserts, info["entries"])
        self.assertGreaterEqual(info["entries"], 20)
        self.assertEqual(sql.count("ON CONFLICT (seq) DO NOTHING"), n_inserts)

    def test_ledger_export_rejects_duplicate_seq(self):
        led_path = os.path.join(self.tmp, "bad.ndjson")
        with open(led_path, "w", encoding="utf-8") as f:
            for i in (1, 1, 2):
                f.write(json.dumps({"seq": i, "event": "e", "hash": "x", "prev_hash": "y"}) + "\n")
        with self.assertRaises(ValueError):
            pg_export.export_ledger(led_path, os.path.join(self.tmp, "bad.sql"))

    def test_manifest_and_verify(self):
        out_dir = os.path.join(self.tmp, "out")
        os.makedirs(out_dir)
        a1 = pg_export.export_sqlite(self.conn, os.path.join(out_dir, "export.sql"))
        man_path = os.path.join(out_dir, "MANIFEST.json")
        with open(man_path, "w", encoding="utf-8") as f:
            json.dump({"artifacts": [a1]}, f)
        v = pg_export.verify_export(man_path)
        self.assertTrue(v["ok"], v)
        # подмена артефакта обнаруживается
        with open(a1["path"], "a", encoding="utf-8") as f:
            f.write("-- подмена\n")
        v2 = pg_export.verify_export(man_path)
        self.assertFalse(v2["ok"])
        self.assertIn("sha256 не совпал", v2["problems"][0])


if __name__ == "__main__":
    unittest.main()
