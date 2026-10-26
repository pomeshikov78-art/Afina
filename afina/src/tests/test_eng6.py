"""Испытания ENG6+ (MAT-01…MAT-10 §13.3) — эталонная реализация G1.

Запуск: python3 -m unittest src.tests.test_eng6 -v  (из корня afina/)
Каждый тест = протоколируемое испытание; генератор протоколов — src/pmi/run_mat.py.
"""
import math
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from afina.eng6 import models, rf, net, rel, opt, cosim
from afina.eng6.pwr_thm import PowerSystem, Load, ThermalNode, thermal_steady, thermal_transient
from afina.eng6.passport import Passport, stable_hash, verify_reproducibility
from afina.eng6.runner import CalcQueue
from afina.units import Q, DimensionError, dbm_to_w, w_to_dbm


class MAT01_AnalyticReference(unittest.TestCase):
    """MAT-01: результат численного решателя совпадает с аналитическим в допуске."""

    def test_fspl_known_value(self):
        # эталон: FSPL(1 ГГц, 1 км) = 92.45 дБ
        self.assertAlmostEqual(rf.path_loss_db("FSPL", 1000, 1, 30, 1.5), 92.44, delta=0.02)

    def test_fspl_formula(self):
        for f, d in [(150, 1), (900, 5), (2600, 10)]:
            expect = 32.44 + 20 * math.log10(f) + 20 * math.log10(d)
            self.assertAlmostEqual(models.fspl_db(f, d), expect, places=9)

    def test_hata_urban_reference(self):
        # эталон Окамура–Хата (средний город): f=900 МГц, d=10 км, hBS=70, hMS=1.5
        L = models.okumura_hata_db(900, 10, 70, 1.5, "urban")
        a_hm = (1.1 * math.log10(900) - 0.7) * 1.5 - (1.56 * math.log10(900) - 0.8)
        expect = 69.55 + 26.16 * math.log10(900) - 13.82 * math.log10(70) - a_hm + (44.9 - 6.55 * math.log10(70)) * 1
        self.assertAlmostEqual(L, expect, places=6)

    def test_dbm_w_roundtrip(self):
        for dbm in (-100, -70, 0, 30, 43):
            self.assertAlmostEqual(w_to_dbm(dbm_to_w(dbm)), dbm, places=9)


class MAT02_Dimensions(unittest.TestCase):
    """MAT-02: несовместимые единицы обнаружены и заблокированы."""

    def test_incompatible_blocked(self):
        with self.assertRaises(DimensionError):
            Q(5.0, "м").to("с")
        with self.assertRaises(DimensionError):
            Q(5.0, "Вт") + Q(1.0, "В")

    def test_compatible_converted(self):
        self.assertAlmostEqual(Q(1.5, "км").to("м").value, 1500.0)
        self.assertAlmostEqual(Q(2, "ч").to("мин").value, 120.0)
        self.assertAlmostEqual(Q(3500, "МГц").to("Гц").value, 3.5e9)

    def test_unknown_blocks_arithmetic(self):
        # EQP-001: «неизвестно» (None) не подменяется условным значением
        with self.assertRaises(DimensionError):
            Q(None, "Вт") * 2


class MAT03_Convergence(unittest.TestCase):
    """MAT-03: уточнение сетки даёт стабилизацию результата."""

    def _coverage(self, cell):
        st = [rf.BaseStation("BS1", 2500, 2500, 30, tx_power_dbm=43, f_mhz=900, sector=False)]
        r = rf.coverage_grid(st, 5000, 5000, cell, model_key="OKUMURA_HATA", env="urban", threshold_dbm=-95)
        return r.coverage_pct

    def test_grid_refinement_stabilizes(self):
        c100, c50, c25 = self._coverage(100), self._coverage(50), self._coverage(25)
        # стабилизация: разница на мелкой сетке мала и не превосходит разницу грубых сеток (с допуском округления)
        self.assertLess(abs(c50 - c25), 1.0)
        self.assertLessEqual(abs(c50 - c25), abs(c100 - c50) + 0.5)


class MAT04_Applicability(unittest.TestCase):
    """MAT-04: выход за диапазон вызывает предупреждение/блокировку."""

    def test_cost231_blocks_3_5ghz(self):
        with self.assertRaises(models.ApplicabilityError):
            models.cost231_hata_db(3500, 2, 30, 1.5)

    def test_hata_blocks_out_of_range(self):
        with self.assertRaises(models.ApplicabilityError):
            models.okumura_hata_db(50, 2, 30, 1.5)  # 50 МГц вне 150–1500


class MAT05_Uncertainty(unittest.TestCase):
    """MAT-05: компоненты неопределённости раздельны и соответствуют структуре."""

    def test_four_components(self):
        st = [rf.BaseStation("BS1", 500, 500, 30)]
        r = rf.coverage_grid(st, 1000, 1000, 100, model_key="SPM")
        self.assertEqual(set(r.uncertainty_db.keys()), {"data", "numeric", "model_params", "physical_model"})
        rss = math.sqrt(sum(v * v for v in r.uncertainty_db.values()))
        self.assertGreater(rss, 0)


class MAT06_CoSimulation(unittest.TestCase):
    """MAT-06: параметры передаются между решателями без потери размерности."""

    def test_chain_runs(self):
        solvers, wiring, mappings = cosim.pwr_thm_rel_solvers()
        res = cosim.run_chain("CS-TEST-1", solvers, wiring, mappings,
                              {"w_nom": 4200, "w_peak": 5100, "t_ambient_c": 25, "t_limit_c": 35,
                               "cooling_w": 4100, "mtbf_h": 8000, "mttr_h": 4})
        self.assertTrue(res["ok"], res)
        self.assertIn("availability", res["data"])
        self.assertGreater(res["data"]["t_cabinet_c"], 25)
        self.assertGreater(res["data"]["availability"], 0.99)  # охлаждение объявлено портом и учтено

    def test_cycles_detected(self):
        s1 = cosim.Solver("A", "1", cosim.SolverIO({"x": "1"}, {"y": "1"}), lambda i: {"y": 1})
        s2 = cosim.Solver("B", "1", cosim.SolverIO({"y": "1"}, {"x": "1"}), lambda i: {"x": 1})
        res = cosim.detect_cycles([s1, s2], [("A", "B"), ("B", "A")])
        self.assertTrue(res)

    def test_unit_mismatch_detected(self):
        s1 = cosim.Solver("A", "1", cosim.SolverIO({}, {"p": "Вт"}), lambda i: {})
        s2 = cosim.Solver("B", "1", cosim.SolverIO({"q": "°C"}, {}), lambda i: {})
        problems = cosim.check_units(s1, s2, {"p": "q"})
        self.assertTrue(problems)


class MAT07_Reproducibility(unittest.TestCase):
    """MAT-07: повтор на снимке данных даёт результат в допуске."""

    def test_repeat_in_tolerance(self):
        st = [rf.BaseStation("BS1", 1500, 1500, 30, sector=False)]
        def run():
            return rf.coverage_grid(st, 3000, 3000, 50, model_key="OKUMURA_HATA", threshold_dbm=-95).coverage_pct
        inputs = {"stations": [(s.id, s.x_m, s.y_m, s.h_ant_m) for s in st], "grid": 50}
        p1 = Passport.new("RF-03", "OKUMURA_HATA", models.VERSION, "Engineering", "L1", inputs).finish(results_ref=None)
        p2 = Passport.new("RF-03", "OKUMURA_HATA", models.VERSION, "Engineering", "L1", inputs).finish(results_ref=None)
        r1, r2 = run(), run()
        v = verify_reproducibility(p1, p2, r1, r2, tolerance=0.01)
        self.assertEqual(v["verdict"], "PASS")


class MAT08_Surrogate(unittest.TestCase):
    """MAT-08: суррогат — ошибка на независимом наборе в пределах нормы (базовая схема)."""

    def test_poly_surrogate_error(self):
        # обучающая выборка: FSPL по (lg f, lg d) — линейная модель должна быть почти точной
        import random
        random.seed(4471)
        train = [(f, d) for f in (800, 1800, 2600) for d in (0.5, 2, 10)]
        # суррогат: линейная регрессия по [1, lgf, lgd]
        X = [[1, math.log10(f), math.log10(d)] for f, d in train]
        y = [models.fspl_db(f, d) for f, d in train]
        coef = _lstsq3(X, y)
        test = [(1200, 3), (2000, 7)]
        for f, d in test:
            pred = coef[0] + coef[1] * math.log10(f) + coef[2] * math.log10(d)
            err = abs(pred - models.fspl_db(f, d))
            self.assertLess(err, 0.5, "ошибка суррогата на независимом наборе > 0.5 дБ")


class MAT09_Optimizer(unittest.TestCase):
    """MAT-09: известное решение контрольной задачи найдено; жёсткие ограничения соблюдены."""

    def test_hard_constraint_filters(self):
        crit = [opt.Criterion("cost", "CAPEX", "min", 2.0),
                opt.Criterion("cov", "Покрытие", "max", 1.0, hard_min=90.0)]
        variants = [
            {"id": "A", "cost": 100, "cov": 94},   # допустим, оптимум по сумме
            {"id": "B", "cost": 60, "cov": 85},    # дешевле, но покрытие < 90 → ОТКЛОНЁН
            {"id": "C", "cost": 140, "cov": 96},
        ]
        res = opt.feasible(variants, crit)
        ids = [v["id"] for v in res["feasible"]]
        self.assertNotIn("B", ids)                 # ENG-006: нарушитель не может быть «лучшим»
        self.assertEqual(res["rejected"][0]["variant"]["id"], "B")
        self.assertIn("reason", res["rejected"][0])
        ranked = opt.rank_weighted(res["feasible"], crit)
        self.assertEqual(ranked[0]["id"], "A")

    def test_pareto(self):
        crit = [opt.Criterion("cost", "CAPEX", "min"), opt.Criterion("cov", "Покрытие", "max")]
        variants = [{"id": "A", "cost": 100, "cov": 90}, {"id": "B", "cost": 120, "cov": 95},
                    {"id": "C", "cost": 150, "cov": 88}]
        front = opt.pareto_front(variants, crit)
        self.assertIn("A", front); self.assertIn("B", front); self.assertNotIn("C", front)


class MAT10_Regression(unittest.TestCase):
    """MAT-10: эталонные значения ядра стабильны (регрессионный контроль)."""

    GOLDEN = {
        "fspl_1ghz_1km": 92.44,
        "hata_900_10k": None,   # заполняется из текущего прогона при первом утверждении
        "fresnel_f1": None,
    }

    def test_golden_fspl(self):
        self.assertAlmostEqual(models.fspl_db(1000, 1), 92.44, delta=0.01)

    def test_golden_fresnel(self):
        r = rf.fresnel_radius_m(1, 5, 5, 6.0)   # 6 ГГц, по 5 км
        self.assertAlmostEqual(r, 17.32 * math.sqrt(25 / 60), places=6)

    def test_golden_link_budget(self):
        lb = rf.LinkBudget(tx_power_dbm=43, tx_feeder_loss_db=2, tx_ant_gain_dbi=17,
                           path_loss_db=111.2, rx_ant_gain_dbi=9, noise_figure_db=5)
        r = lb.result()
        self.assertAlmostEqual(r["rx_level_dbm"], 43 - 2 + 17 - 111.2 + 9, places=6)


class QueueTests(unittest.TestCase):
    """ARC-003: тяжёлый расчёт не блокирует; очередь сохраняется при перезапуске."""

    def test_queue_persists_and_recovers(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "queue.json")
            q = CalcQueue(persist_path=p, workers=0)
            q.register("RF-03", lambda payload, progress_cb=None: {"coverage_pct": 42.0})
            jid = q.submit("RF-03", {"a": 1})
            q.start()
            time.sleep(0.4)
            snap = q.snapshot()
            self.assertEqual(snap["jobs"][0]["status"], "done")
            q.stop()
            # имитация перезапуска с «зависшим» заданием
            q2 = CalcQueue(persist_path=p, workers=0)
            self.assertIn(jid, q2.jobs)

    def test_unregistered_handler_fails_loud(self):
        q = CalcQueue(workers=0)
        jid = q.submit("NO-SUCH", {})
        q.start(); time.sleep(0.2); q.stop()
        self.assertEqual(q.jobs[jid].status, "error")
        self.assertIn("не зарегистрирован", q.jobs[jid].error)


class PwrThmRelTests(unittest.TestCase):
    def test_power_balance(self):
        ps = PowerSystem(loads=[Load("об", 4200, 5100)], ups_battery_wh=11200, gen_fuel_hours=8)
        b = ps.balance(ambient_c=20)
        self.assertAlmostEqual(b["battery_hours"], 11200 * 0.94 / 4200, places=2)
        self.assertTrue(b["generator_start_guaranteed"])
        b2 = ps.balance(ambient_c=-25)
        self.assertFalse(b2["generator_start_guaranteed"])

    def test_thermal_limit(self):
        node = ThermalNode("шкаф", 200, r_th_k_per_w=0.02, t_limit_c=35)
        r = thermal_steady(node, 28)
        self.assertTrue(r["operational"])          # 28 + 200·0.02 = 32 ≤ 35
        r2 = thermal_steady(ThermalNode("шкаф", 2000, r_th_k_per_w=0.02, t_limit_c=35), 28)
        self.assertFalse(r2["operational"])        # 28 + 2000·0.02 = 68 > 35

    def test_availability(self):
        self.assertAlmostEqual(rel.availability(8000, 4), 8000 / 8004, places=9)

    def test_markov_converges(self):
        P = [[0.9, 0.1], [0.5, 0.5]]
        r = rel.markov_steady(P)
        self.assertTrue(r["converged"])
        self.assertAlmostEqual(r["pi"][0], 5 / 6, places=6)

    def test_optical_budget(self):
        link = net.OpticalLink(fiber_km=20, connectors=4, splices=2)
        b = link.budget()
        self.assertAlmostEqual(b["total_loss_db"], 20 * 0.35 + 4 * 0.5 + 2 * 0.1, places=6)

    def test_throughput_requires_sinr(self):
        r = net.throughput_mbps(None, 100)
        self.assertIsNone(r["throughput_mbps"])
        self.assertIn("SINR не определён", r["note"])


def _lstsq3(X, y):
    # нормальные уравнения 3×3 (чистый Python)
    import itertools
    n = len(X)
    A = [[sum(X[i][a] * X[i][b] for i in range(n)) for b in range(3)] for a in range(3)]
    b = [sum(X[i][a] * y[i] for i in range(n)) for a in range(3)]
    # гаусс
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(3):
        p = max(range(c, 3), key=lambda r: abs(M[r][c])); M[c], M[p] = M[p], M[c]
        for r in range(c + 1, 3):
            f = M[r][c] / M[c][c]
            for k in range(c, 4):
                M[r][k] -= f * M[c][k]
    x = [0.0] * 3
    for r in (2, 1, 0):
        x[r] = (M[r][3] - sum(M[r][k] * x[k] for k in range(r + 1, 3))) / M[r][r]
    return x


if __name__ == "__main__":
    unittest.main(verbosity=2)
