"""Тесты абстракции вычислительных бэкендов и сверки ARC-002."""
import math
import os
import shutil
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend_arc002 import (PAIRED_MODELS, Eng6ProbeBackend, PythonRefBackend,  # noqa: E402
                            default_grid, make_passport, verify_arc002)

VENDOR_SHA256 = "adb610c9eee1ca17b257cbc6087e2278fdde3eef92f3b6a360cfb51968d48b20"
HAS_NODE = shutil.which("node") is not None


class TestPythonRefBackend(unittest.TestCase):
    def setUp(self):
        self.b = PythonRefBackend()

    def test_fspl_analytic_reference(self):
        # MAT-01 эталон: FSPL = 32.44 + 20lg(900) + 20lg(5) = 105.5043 дБ
        v = self.b.path_loss_db("FSPL", 900.0, 5.0, 30.0, 2.0)
        self.assertAlmostEqual(v, 32.44 + 20 * math.log10(900) + 20 * math.log10(5), places=6)

    def test_fingerprint_stable(self):
        f1, f2 = self.b.fingerprint(), self.b.fingerprint()
        self.assertEqual(f1.sha256, f2.sha256)
        self.assertEqual(f1.backend_id, "ENG6+-Python-reference")


@unittest.skipUnless(HAS_NODE, "node недоступен")
class TestEng6ProbeBackend(unittest.TestCase):
    def setUp(self):
        self.b = Eng6ProbeBackend()

    def test_probe_meta_and_sha256(self):
        core = self.b.probe_meta()
        self.assertEqual(core["sha256"], VENDOR_SHA256)
        self.assertIn("hata", core["propKeys"])
        self.assertIn("fspl", core["propKeys"])

    def test_fspl_matches_analytic(self):
        v = self.b.path_loss_db("FSPL", 900.0, 5.0, 30.0, 2.0)
        self.assertAlmostEqual(v, 105.5043, delta=0.01)

    def test_unknown_model_rejected(self):
        with self.assertRaises(Exception):
            self.b.path_loss_db("ВЕДУЩИЙ_КОСОСЧЁТ", 900.0, 5.0, 30.0, 2.0)


@unittest.skipUnless(HAS_NODE, "node недоступен")
class TestArc002(unittest.TestCase):
    def test_small_grid_verdict_not_fail(self):
        a, b = PythonRefBackend(), Eng6ProbeBackend()
        grid = [{"model": "FSPL", "f_mhz": 900.0, "d_km": 5.0, "h_bs_m": 30.0, "h_ms_m": 2.0, "env": "open"},
                {"model": "OKUMURA_HATA", "f_mhz": 900.0, "d_km": 5.0, "h_bs_m": 30.0, "h_ms_m": 2.0, "env": "open"}]
        proto = verify_arc002(a, b, grid)
        self.assertIn(proto["verdict"], ("PASS", "DEGRADED"))
        self.assertEqual(proto["summary"]["failed"], 0)
        self.assertEqual(proto["path_b"]["sha256"], VENDOR_SHA256)

    def test_full_grid_deterministic(self):
        a, b = PythonRefBackend(), Eng6ProbeBackend()
        p1 = verify_arc002(a, b, default_grid())
        p2 = verify_arc002(a, b, default_grid())
        rows1 = [{k: v for k, v in r.items()} for r in p1["rows"]]
        rows2 = [{k: v for k, v in r.items()} for r in p2["rows"]]
        self.assertEqual(rows1, rows2)   # детерминированность обоих путей (NFR-04)

    def test_passport_records_both_backends(self):
        a, b = PythonRefBackend(), Eng6ProbeBackend()
        p = make_passport("RF-01", "OKUMURA_HATA", {"f_mhz": 900, "d_km": 5},
                          [a.fingerprint(), b.fingerprint()])
        d = p.to_dict()
        self.assertIn("ENG6+-Python-reference", d["environment"]["backend"])
        self.assertIn("ENG6-JS-vendor-probe", d["environment"]["backend"])
        self.assertIn(VENDOR_SHA256[:16], d["environment"]["backends"])


if __name__ == "__main__":
    unittest.main()
