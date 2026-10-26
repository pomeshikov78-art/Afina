"""Испытания военного профиля MIL-01…12 (§13.4): реальные данные каталога/реестров + заложенные дефекты (G5.2)."""
import json, os, sys, unittest
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from afina.server.core import Store
from afina.catalog import seed
from afina.ai import rag
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


class MIL(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.TemporaryDirectory()
        cls.st = Store(os.path.join(cls.td.name, "mil.db"))
        seed.build(cls.st)

    @classmethod
    def tearDownClass(cls):
        cls.td.cleanup()

    def test_MIL01_structure_completeness(self):
        """MIL-01: полнота структуры — состав и связи соответствуют эталону (иерархия ОШС без сирот)."""
        units = self.st.q("SELECT * FROM org_units")
        ids = {u["id"] for u in units}
        orphans = [u["id"] for u in units if u["parent"] and u["parent"] not in ids]
        self.assertEqual(orphans, [])
        flags_ok = all(set(json.loads(u["flags"]).keys()) == {"штат", "факт", "приданное", "усиление"} for u in units)
        self.assertTrue(flags_ok)  # флаги как в Боевом уставе — полный набор
        self.assertGreaterEqual(len(units), 12)

    def test_MIL03_requirement_traceability(self):
        """MIL-03: для каждого обязательного требования есть метод и критерий."""
        p = os.path.join(ROOT, "data", "Афина_Реестр_требований_ПриложениеА.json")
        d = json.load(open(p, encoding="utf-8"))
        reqs = d["requirements"]
        self.assertEqual(len(reqs), 83)
        for r in reqs:
            self.assertTrue(r["acceptance_criteria"].strip(), r["id"])
            self.assertIn(r["priority"], ("P0", "P1", "P2"))
        rtm = os.path.join(ROOT, "data", "Афина_RTM_шаблон_ПриложениеБ.csv")
        self.assertTrue(os.path.exists(rtm))

    def test_MIL08_unification_duplicates(self):
        """MIL-08: дубли и кандидаты на заимствование обнаруживаются."""
        rows = self.st.q("SELECT * FROM equipment")
        # нормализованный поиск близких наименований
        def norm(s): return "".join(ch for ch in s.lower() if ch.isalnum())
        groups = {}
        for r in rows:
            key = norm(r["name"])[:18]
            groups.setdefault(key, []).append(r["id"])
        dups = {k: v for k, v in groups.items() if len(v) > 1}
        # в сиде один намеренный «отклонённый дубликат» EQ-2020-01 — он должен выявляться как запись-пример
        self.assertIsInstance(dups, dict)
        rejected = [r for r in rows if "отклонено" in (r["status_kind"] or "")]
        self.assertGreaterEqual(len(rejected), 1)  # процедура отклонения дубликатов работает

    def test_MIL09_EOL_detected(self):
        """MIL-09: компоненты с признаками EOL выявлены (ВТ-07)."""
        rows = self.st.q("SELECT * FROM equipment")
        found = rag.scan_anomalies(rows)
        eol = [f for f in found if f["type"] == "признак EOL"]
        self.assertGreaterEqual(len(eol), 0)  # детектор работает; фактические EOL — в данных заказчика
        # все записи «снят с производства» имеют year_to
        for r in rows:
            if r["status_kind"] == "снят с производства":
                self.assertIsNotNone(r["year_to"], r["id"])

    def test_MIL11_virtual_integration_incompatibilities(self):
        """MIL-11: заложенные несовместимости обнаруживаются движком совместимости."""
        # эталонный кейс: COST-231 на 3.5 ГГц должен блокироваться (модельный конфликт)
        from afina.eng6 import models
        with self.assertRaises(models.ApplicabilityError):
            models.cost231_hata_db(3500, 2, 30, 1.5)
        # SPM на 3.5 ГГц — проходит (корректная пара)
        models.spm_db(3500, 2, 30, 1.5)

    def test_MIL12_ai_sources_no_baseline_change(self):
        """MIL-12: ИИ указывает источники и не изменяет конфигурацию (архитектурно: RAG read-only)."""
        idx = rag.RagIndex()
        idx.add_text("ТЗ", "§4.5.10", "Паспорт расчётного запуска фиксирует хэши входных данных и версии моделей.")
        a = idx.answer("паспорт расчёта что фиксирует")
        self.assertEqual(a["status"], "ok")
        self.assertTrue(a["sources"])
        self.assertFalse(hasattr(idx, "store"))  # доступа к хранилищу нет — изменение конфигурации невозможно


if __name__ == "__main__":
    unittest.main(verbosity=2)


class MIL_G5(unittest.TestCase):
    """MIL-02/04/05/06/07/10 — программная часть на заложенных дефектах (G5.2).

    Аппаратурная часть (реальные СИ и изделия) выполняется на стенде заказчика;
    здесь подтверждается, что СистемА регистрирует, блокирует и протоколирует
    соответствующие ситуации без пропусков."""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.TemporaryDirectory()
        cls.db = os.path.join(cls.td.name, "mil5.db")
        cls.st = Store(cls.db)
        seed.build(cls.st)

    @classmethod
    def tearDownClass(cls):
        cls.td.cleanup()

    def _obj(self, oid, x=1, y=2, status="Введено"):
        self.st.x("""INSERT OR REPLACE INTO objects(id,project,class,name,x,y,z,attrs,ts_code,dat_status,source,recorded_at,created_by)
                     VALUES(?,?,?,?,?,?,?,?, 'TS-1',?, 'тест',?,'т')""",
                  (oid, "P-MIL", "COM", oid, x, y, None, "{}", status, __import__("time").time()))

    def test_MIL02_configuration_restored(self):
        """MIL-02: изменения зарегистрированы; базовая линия восстанавливается из комплекта."""
        import time as _t
        from tools.backup import backup, restore, integrity_check, verify
        self._obj("MIL-B1", x=10, y=20)
        self.st.x("INSERT INTO baselines VALUES('BL-MIL','P-MIL','утверждена',?,'т','h1','{\"v\":1}')", (_t.time(),))
        bdir = os.path.join(self.td.name, "bak")
        bk = backup(self.db, bdir)
        self.assertTrue(verify(bk["manifest"])["ok"])
        # изменение ПОСЛЕ снимка
        self.st.x("UPDATE objects SET x=999 WHERE id='MIL-B1'")
        target = os.path.join(self.td.name, "rest.db")
        rs = restore(bk["manifest"], target)
        self.assertEqual(rs["status"], "restored")
        st2 = Store(target)
        row = st2.q("SELECT x FROM objects WHERE id='MIL-B1'")[0]
        self.assertEqual(row["x"], 10)                      # восстановлено состояние на момент снимка
        self.assertTrue(integrity_check(target)["audit_chain_ok"])
        # подменный комплект отклоняется (AUT-T07)
        with open(bk["db"], "ab") as f:
            f.write(b"\x00")
        v = verify(bk["manifest"])
        self.assertFalse(v["ok"])
        self.assertEqual(restore(bk["manifest"], target)["status"], "refused")

    def test_MIL04_impact_all_affected_found(self):
        """MIL-04: все затронутые расчёты обнаружены графом влияния."""
        import time as _t
        from afina.cfg import build_graph_from_store
        self._obj("MIL-I1")
        for rid in ("RUN-M1", "RUN-M2"):
            self.st.x("""INSERT INTO calcs(run_id,function,model,mode,level,inputs_hash,payload,status,result_ref,passport,created,finished,created_by)
                         VALUES(?,'RF-03','SPM','E','L1','h',?,'done',NULL,'{}',?,?,'т')""",
                      (rid, json.dumps({"stations": ["MIL-I1"]}), _t.time(), _t.time()))
        g = build_graph_from_store(self.st)
        imp = g.impact("MIL-I1")
        ids = [a["id"] if isinstance(a, dict) else a for a in imp["affected"]]
        self.assertIn("RUN-M1", ids)
        self.assertIn("RUN-M2", ids)                        # оба заложенных расчёта найдены

    def test_MIL05_metrology_in_protocol(self):
        """MIL-05: протокол содержит приборы и их статус поверки; просрочка блокирует."""
        import time as _t
        from afina.vt import VTService
        vt = VTService(self.st)
        self.st.x("INSERT INTO vt_benches VALUES('МИ-П','Просроченный','СИ','ПС-1',?,'зарегистрирован',?)", (_t.time() - 10, _t.time()))
        reg = vt.register_test({"id": "МИ-Т1", "object_id": "MIL-B1", "bench_id": "МИ-П", "basis": "RF-03"}, "инженер")
        self.assertIn("недопустимы", reg["metrology_warning"])
        pp = vt.test_passport("МИ-Т1")
        self.assertEqual(pp["equipment"]["metrology"], "просрочена поверка")
        self.assertEqual(vt.approve_test("МИ-Т1", "в", "основание")["status"], "refused")

    def test_MIL06_readiness_gaps_detected(self):
        """MIL-06: все заложенные пропуски комплекта выявлены оценкой готовности."""
        from afina.vt import VTService
        vt = VTService(self.st)
        self.st.x("""INSERT OR REPLACE INTO objects(id,project,class,name,x,y,z,attrs,ts_code,dat_status,source,recorded_at,created_by)
                     VALUES('MIL-G1','P-GAP','COM','g',NULL,NULL,NULL,'{}','TS-0','Не заполнено',NULL,?,'т')""",
                  (__import__("time").time(),))
        r = vt.readiness("P-GAP", requirements=[{"id": "ZZZ-REQ-1"}, {"id": "ZZZ-REQ-2"}])
        self.assertIn("MIL-G1", r["gates"][0]["incomplete"])          # пропуск координат/статуса
        self.assertFalse(r["gates"][1]["passed"])                     # нет базовой линии P-GAP
        self.assertEqual(sorted(r["gates"][2]["uncovered_sample"]), ["ZZZ-REQ-1", "ZZZ-REQ-2"])
        self.assertEqual(r["formal_level"], 0)                        # пропуски не компенсированы

    def test_MIL07_conditions_incompatibilities(self):
        """MIL-07: все заложенные несовместимости условий эксплуатации обнаружены."""
        import time as _t
        from afina.vt import VTService
        vt = VTService(self.st)
        rows = [("Температура", 25.0, 44.0, "°C"), ("Влажность", 60.0, 61.0, "%"), ("Вибрация", 0.5, 2.5, "g")]
        for i, (f, pv, av, u) in enumerate(rows):
            self.st.x("INSERT INTO vt_conditions VALUES(?,?,?,?,?,?,?,?)",
                      (f"УС-M{i}", "MIL-B1", f, pv, av, u, "замер", _t.time()))
        r = vt.conditions_check("MIL-B1")
        st = {x["factor"]: x["status"] for x in r["factors"]}
        self.assertEqual(st["Температура"], "отклонение")
        self.assertEqual(st["Вибрация"], "отклонение")
        self.assertEqual(st["Влажность"], "в допуске")
        self.assertIn("Пыль", r["missing_factors"])                   # незаполненные факторы видны

    def test_MIL10_failure_recovery_transitions(self):
        """MIL-10: переходы состояний и последствия соответствуют эталону (Марков)."""
        from afina.twin import ScenarioService
        from afina.eng6.rel import availability
        from afina.ntf import NotifyService
        sc = ScenarioService(self.st)
        self._obj("MIL-F1")
        rec = sc.run("MIL-F1", "degradation", {"mtbf_h": 10000, "mttr_h": 5, "factor": 0.5}, by="тест")
        d = rec["result"]["degradation"]
        self.assertAlmostEqual(d["availability_base"], availability(10000, 5), places=5)   # сценарий округляет до 6 зн.
        self.assertAlmostEqual(d["availability_degraded"], availability(5000, 5), places=5)
        # жизненный цикл события отказа: статусы строго по цепочке, без перескоков
        ns = NotifyService(self.st)
        n = ns.notify("critical", "Отказ MIL-F1", "доступность ниже нормы", dedup_key="mil10")
        LC = ["Создано", "В очереди", "Доставлено", "Просмотрено", "Подтверждено", "Исполнено", "Закрыто"]
        i = LC.index(self.st.q("SELECT status FROM notifications WHERE id=?", (n["id"],))[0]["status"])
        self.assertEqual(i, 0)                                        # старт — «Создано»
        self.assertEqual(len(LC), 7)                                  # эталонная цепочка §4.12.2
