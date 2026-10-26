"""Тесты G4: сценарный модуль DT-5, объяснения ИИ (G4.1), оптимизация OPT-01/LCC-01
как сервис (G4.2), Co-Simulation и суррогаты (G4.3), военно-технический профиль
ВТ-01…08 (G4.4), геймификация и полигон (G4.5), аналитика руководителя (G4.6).

Часть 1 — юнит-тесты модулей на Store в памяти.
Часть 2 — сквозные HTTP-тесты (порт 8496).
"""
import json, os, sys, tempfile, threading, time, unittest
import urllib.request, urllib.error, urllib.parse

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from afina.server.core import Store
from afina.twin import TwinService, ScenarioService, SCENARIO_KINDS
from afina.ai.explain import explain_calc, explain_issue, suggest_scenarios, AI_POLICY


def mem_store():
    return Store(":memory:")


def add_obj(st, oid, x=None, y=None, cls="COM"):
    st.x("INSERT INTO objects(id,project,class,name,x,y,z,attrs,source,recorded_at,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
         (oid, "P-1", cls, oid, x, y, None, "{}", "тест", time.time(), "тест"))


def add_tele(st, oid, metric, values):
    for i, v in enumerate(values):
        st.x("INSERT INTO telemetry(object_id,ts,metric,value,quality) VALUES(?,?,?,?,?)",
             (oid, time.time() + i, metric, float(v), "наблюдение"))


# ============ Часть 1: юнит-тесты G4.1 ============

class TestScenarioUnit(unittest.TestCase):
    def test_01_scenario_marked_as_estimate(self):
        """DT-5: сценарий выполняется, результат маркирован «оценка», базовая линия не меняется."""
        st = mem_store(); add_obj(st, "OBJ-S", x=1, y=2)
        add_tele(st, "OBJ-S", "load_pct", [40, 42, 44, 46])
        sc = ScenarioService(st)
        rec = sc.run("OBJ-S", "param", {"metric": "load_pct", "delta_pct": 25, "limit": 60}, by="тест")
        self.assertEqual(rec["status"], "done")
        self.assertEqual(rec["marked"], "оценка (не факт)")
        self.assertAlmostEqual(rec["result"]["param_scenario"]["projected"], 57.5)
        self.assertFalse(rec["result"]["param_scenario"]["limit_breached"])
        # телеметрия и объекты не изменены (DGT-002)
        self.assertEqual(st.q("SELECT COUNT(*) c FROM telemetry WHERE object_id='OBJ-S'")[0]["c"], 4)

    def test_02_insufficient_data_honest_refusal(self):
        """При недостатке данных — честный отказ с запросом данных, а не догадка."""
        st = mem_store(); add_obj(st, "OBJ-S2", x=1, y=2)
        add_tele(st, "OBJ-S2", "rssi_dbm", [-70])  # одна точка
        rec = ScenarioService(st).run("OBJ-S2", "trend", {"metric": "rssi_dbm"}, by="тест")
        self.assertEqual(rec["status"], "insufficient_data")
        self.assertIn("request", rec)
        # сценарий НЕ записан как выполненный
        self.assertEqual(len(ScenarioService(st).list("OBJ-S2")), 0)

    def test_03_unknown_kind_rejected(self):
        """Незарегистрированный вид сценария отклоняется (тихая подмена запрещена)."""
        st = mem_store(); add_obj(st, "OBJ-S3")
        rec = ScenarioService(st).run("OBJ-S3", "magic", {}, by="тест")
        self.assertEqual(rec["status"], "error")
        self.assertIn("не поддержан", rec["error"])

    def test_04_dt5_requires_validated_scenario(self):
        """DT-5 присваивается только после независимой проверки сценария; не «через голову»."""
        st = mem_store(); tw = TwinService(st); sc = ScenarioService(st)
        add_obj(st, "OBJ-D5", x=5, y=6)
        st.x("""INSERT INTO calcs(run_id,function,model,mode,level,inputs_hash,payload,status,result_ref,passport,created,finished,created_by)
                VALUES('R1','RF-03','SPM','E','L1','h',?,'done',NULL,'{}',?,?,'т')""",
             (json.dumps({"stations": ["OBJ-D5"]}), time.time(), time.time()))
        add_tele(st, "OBJ-D5", "rssi_dbm", [-70, -71, -72])
        st.x("INSERT INTO audit(ts,user_login,role,action,object,class,result,prev_hash,hash) VALUES(?,?,?,?,?,?,?,?,?)",
             (time.time(), "т", "-", "протокол отклонений проект↔факт (0 расхождений)", "OBJ-D5", "data", "-", "", ""))
        self.assertEqual(tw.dt_level("OBJ-D5")["level"], "DT-4")
        rec = sc.run("OBJ-D5", "trend", {"metric": "rssi_dbm", "horizon": 3}, by="тест")
        self.assertEqual(rec["status"], "done")
        # сценарий есть, но не проверен независимо — DT-5 не присваивается
        self.assertEqual(tw.dt_level("OBJ-D5")["level"], "DT-4")
        v = sc.validate(rec["id"], "верификатор", "сверка с ручным расчётом тренда")
        self.assertEqual(v["status"], "validated")
        self.assertEqual(tw.dt_level("OBJ-D5")["level"], "DT-5")
        # валидация без основания отклоняется
        rec2 = sc.run("OBJ-D5", "degradation", {"mtbf_h": 5000, "mttr_h": 6, "factor": 0.5}, by="тест")
        self.assertEqual(sc.validate(rec2["id"], "в", "")["status"], "error")

    def test_05_degradation_uses_rel_kernel(self):
        """Сценарий деградации считает доступность ядром REL-01, а не «на глаз»."""
        st = mem_store(); add_obj(st, "OBJ-R")
        rec = ScenarioService(st).run("OBJ-R", "degradation", {"mtbf_h": 10000, "mttr_h": 5, "factor": 0.5}, by="тест")
        d = rec["result"]["degradation"]
        self.assertAlmostEqual(d["availability_base"], 10000 / 10005, places=5)
        self.assertAlmostEqual(d["availability_degraded"], 5000 / 5005, places=5)
        self.assertLess(d["delta"], 0)


class TestExplainUnit(unittest.TestCase):
    def test_01_explain_calc_structure(self):
        """AI-001: ответ содержит источники, версии, допущения, ограничения, статус."""
        passport = {"function": "RF-03", "model_id": "SPM", "model_version": "1.0.0", "mode": "Engineering",
                    "level": "L1", "inputs": {"stations": 2, "cell_m": 50}, "inputs_hash": "abc123def456",
                    "status": "done", "uncertainty_db": {"data": 2.0, "numeric": 0.1, "model_params": 1.5, "physical_model": 3.0},
                    "crs": "WGS84", "applicability": {"model_domain_ok": True}}
        e = explain_calc(passport)
        self.assertEqual(e["status"], "ok")
        for key in ("sources", "versions", "assumptions", "limitations", "confirmation_status"):
            self.assertIn(key, e)
        self.assertTrue(any("ТЗ §4.5.4 RF-03" in s.get("section", "") for s in e["sources"]))
        self.assertIn("данные: ±2.0 дБ", e["answer"])
        self.assertTrue(any("не подменяет расчётное ядро" in l for l in e["limitations"]))

    def test_02_explain_without_passport_requests_data(self):
        """При недостатке данных ИИ запрашивает данные (п. 4.10.2)."""
        e = explain_calc({})
        self.assertEqual(e["status"], "insufficient_data")
        self.assertIn("Запрашиваю данные", e["answer"])

    def test_03_explain_issue_and_unknown(self):
        e = explain_issue("loop", {"detail": "A→B→A"})
        self.assertEqual(e["status"], "ok")
        self.assertIn("A→B→A", e["answer"])
        self.assertTrue(e["limitations"])
        u = explain_issue("несуществующий")
        self.assertEqual(u["status"], "insufficient_data")

    def test_04_suggest_scenarios_data_driven(self):
        """Предложения сценариев — по фактической наполненности данных."""
        s = suggest_scenarios({"id": "OBJ-X"}, {"rssi_dbm": {"value": -70}}, has_calc=True)
        self.assertEqual(s["status"], "ok")
        kinds = {x["kind"] for x in s["suggestions"]}
        self.assertTrue(kinds <= set(SCENARIO_KINDS))
        empty = suggest_scenarios({"id": "OBJ-Y"}, {}, has_calc=False)
        self.assertTrue(any(x["data_needed"] for x in empty["suggestions"]))
        none = suggest_scenarios(None, {})
        self.assertEqual(none["status"], "insufficient_data")

    def test_05_policy_prohibitions_present(self):
        """Запреты §4.10.2 присутствуют в политике каждого объяснения."""
        self.assertTrue(any("не изменяет утверждённую конфигурацию" in p for p in AI_POLICY))
        self.assertTrue(any("не подтверждает техническую исправность" in p for p in AI_POLICY))
        self.assertTrue(any("не подменяет расчётное ядро" in p for p in AI_POLICY))


# ============ Часть 1б: юнит-тесты G4.2 (OPT-01/LCC-01) ============

class TestOptUnit(unittest.TestCase):
    def test_01_hard_constraint_blocks_best(self):
        """ENG-006: вариант, нарушающий обязательное ограничение, не может быть «лучшим»."""
        from afina.eng6.opt import optimize
        r = optimize(
            [{"id": "A", "cost": 100, "cover": 80}, {"id": "B", "cost": 10, "cover": 99}],
            [{"key": "cost", "name": "Стоимость", "direction": "min"},
             {"key": "cover", "name": "Покрытие", "direction": "max", "hard_min": 90}],
            [{"type": "numeric_bound", "field": "cost", "min": 50}])
        self.assertEqual(r["feasible_count"], 0)
        self.assertEqual(r["rejected_count"], 2)
        self.assertIsNone(r["best"])
        self.assertTrue(all(x["reason"] for x in r["rejected"]))

    def test_02_pareto_and_weights_separated(self):
        """Парето-фронт и взвешенное ранжирование — разные результаты; противоречие отмечено."""
        from afina.eng6.opt import optimize
        r = optimize(
            [{"id": "A", "cost": 100, "cover": 80}, {"id": "B", "cost": 60, "cover": 60},
             {"id": "C", "cost": 120, "cover": 70}],
            [{"key": "cost", "name": "Стоимость", "direction": "min", "weight": 3},
             {"key": "cover", "name": "Покрытие", "direction": "max", "weight": 1}])
        self.assertEqual(set(r["pareto_front"]), {"A", "B"})   # C доминируется A
        self.assertIn(r["best"]["id"], ("A", "B"))

    def test_03_unknown_constraint_type_rejected(self):
        from afina.eng6.opt import declarative_constraints
        with self.assertRaises(ValueError):
            declarative_constraints([{"type": "магия", "field": "x"}])

    def test_04_lcc_math(self):
        """LCC-01: TCO = CAPEX + PV(OPEX) − PV(утилизация); чувствительность ±20 %."""
        from afina.eng6.opt import lcc
        r = lcc(capex=1000.0, years=2, opex_per_year=100.0, discount=0.0, disposal=50.0)
        self.assertAlmostEqual(r["tco"], 1000 + 200 - 50)
        self.assertGreater(r["tco_sensitivity"]["plus20_opex"], r["tco"])
        self.assertLess(r["tco_sensitivity"]["minus20_opex"], r["tco"])


# ============ Часть 1в: юнит-тесты G4.3 (суррогаты MOD-006, 3D-загрузчики) ============

class TestSurrogateUnit(unittest.TestCase):
    @staticmethod
    def _train_set():
        from afina.eng6.surrogate import rf01_solver
        solver = rf01_solver("OKUMURA_HATA")
        return solver, [[f, d, hb, hm, solver([f, d, hb, hm])]
                        for f in (200, 500, 900, 1200, 1500) for d in (1, 3, 7, 12, 18)
                        for hb in (30, 60, 120) for hm in (1, 2, 4)]

    def test_01_train_predict_domain(self):
        from afina.eng6.surrogate import PolynomialSurrogate
        solver, pts = self._train_set()
        s = PolynomialSurrogate("SUR-1", "RF-01/OKUMURA_HATA", 2, ["f_mhz", "d_km", "h_bs", "h_ms"])
        tr = s.train(pts)
        self.assertEqual(tr["status"], "trained")
        self.assertLess(tr["rmse"], 6.0)          # полином-2 против Хаты: метры/единицы дБ, не «точная копия»
        self.assertIn("СУРРОГАТ", tr["marked"])   # всегда маркируется
        p = s.predict([900, 5, 60, 2])
        self.assertEqual(p["status"], "ok")
        self.assertLess(abs(p["value"] - solver([900, 5, 60, 2])), 10.0)
        o = s.predict([2600, 5, 60, 2])           # выход за обучающий диапазон
        self.assertEqual(o["status"], "out_of_domain")
        self.assertTrue(o["outside"])

    def test_02_degenerate_set_refused(self):
        """Постоянный признак — честный отказ: коэффициент не идентифицируется."""
        from afina.eng6.surrogate import PolynomialSurrogate
        s = PolynomialSurrogate("SUR-2", "X", 1, ["a", "b"])
        r = s.train([[1, 2, 3], [2, 2, 5], [3, 2, 7], [4, 2, 9]])
        self.assertEqual(r["status"], "insufficient_data")
        self.assertIn("не варьируются", r["error"])

    def test_03_compare_and_disable(self):
        from afina.eng6.surrogate import PolynomialSurrogate, PolynomialSurrogate as PS
        solver, pts = self._train_set()
        s = PolynomialSurrogate("SUR-3", "RF-01/OKUMURA_HATA", 2, ["f_mhz", "d_km", "h_bs", "h_ms"])
        s.train(pts)
        cmp = s.compare(solver, [[800, 4, 50, 2], [1200, 9, 100, 3], [300, 2, 40, 1]])
        self.assertEqual(cmp["status"], "compared")
        self.assertLess(cmp["rmse_independent"], 15.0)
        s.disabled = True
        self.assertEqual(s.predict([900, 5, 60, 2])["status"], "disabled")
        # round-trip сериализации сохраняет поведение
        s2 = PS.from_dict(s.to_dict())
        self.assertTrue(s2.disabled)

    def test_04_gltf_normals_uvs_indices(self):
        """G4 (остаток G3): glTF — NORMAL, TEXCOORD_0 и индексы читаются."""
        import base64, struct
        from afina.gis.models3d import load_gltf
        pos = struct.pack("<9f", 0, 0, 0, 1, 0, 0, 0, 1, 0)
        nrm = struct.pack("<9f", 0, 0, 1, 0, 0, 1, 0, 0, 1)
        uv = struct.pack("<6f", 0, 0, 1, 0, 0, 1)
        idx = struct.pack("<3H", 0, 1, 2)
        blob = idx + b"\x00\x00" + pos + nrm + uv   # выравнивание 4 байта
        uri = "data:application/octet-stream;base64," + base64.b64encode(blob).decode()
        g = {"buffers": [{"uri": uri, "byteLength": len(blob)}],
             "bufferViews": [
                 {"buffer": 0, "byteOffset": 0, "byteLength": 6},
                 {"buffer": 0, "byteOffset": 8, "byteLength": 36},
                 {"buffer": 0, "byteOffset": 44, "byteLength": 36},
                 {"buffer": 0, "byteOffset": 80, "byteLength": 24}],
             "accessors": [
                 {"bufferView": 1, "componentType": 5126, "count": 3, "type": "VEC3"},
                 {"bufferView": 2, "componentType": 5126, "count": 3, "type": "VEC3"},
                 {"bufferView": 3, "componentType": 5126, "count": 3, "type": "VEC2"},
                 {"bufferView": 0, "componentType": 5123, "count": 3, "type": "SCALAR"}],
             "meshes": [{"name": "tri", "primitives": [{"attributes": {"POSITION": 0, "NORMAL": 1, "TEXCOORD_0": 2},
                                                        "indices": 3}]}]}
        r = load_gltf(g)
        m = r["meshes"][0]
        self.assertEqual(m["normals"][0], [0.0, 0.0, 1.0])
        self.assertEqual(m["uvs"][1], [1.0, 0.0])
        self.assertEqual(m["indices"], [0, 1, 2])
        self.assertEqual(m["triangle_count"], 1)

    def test_05_ifc_extrusion_geometry(self):
        """G4 (остаток G3): IfcExtrudedAreaSolid → призма в абсолютных координатах."""
        from afina.gis.models3d import load_ifc_structure
        step = """ISO-10303-21;
HEADER;
ENDSEC;
DATA;
#10=IFCCARTESIANPOINT('',(0.,0.,0.));
#11=IFCAXIS2PLACEMENT3D(#10,$,$);
#12=IFCLOCALPLACEMENT($,#11);
#20=IFCCARTESIANPOINT('',(1.,1.));
#21=IFCAXIS2PLACEMENT2D(#20,$);
#22=IFCRECTANGLEPROFILEDEF(.AREA.,'Профиль',#21,4.0,0.3);
#23=IFCDIRECTION('',(0.,0.,1.));
#24=IFCEXTRUDEDAREASOLID(#22,#11,#23,3.0);
#25=IFCSHAPEREPRESENTATION($,'Body','SweptSolid',(#24));
#26=IFCPRODUCTDEFINITIONSHAPE($,$,(#25));
#5=IFCWALL('0w1',$,'Стена 1',$,#12,#26,'t');
ENDSEC;
END-ISO-10303-21;"""
        r = load_ifc_structure(step)
        self.assertEqual(r["geometry"]["solids_built"], 1)
        g = r["elements"]["walls"][0]["geometry"][0]
        self.assertEqual(g["type"], "extruded_prism")
        self.assertAlmostEqual(g["depth"], 3.0)
        self.assertEqual(len(g["vertices"]), 8)
        self.assertAlmostEqual(g["vertices"][0][0], -1.0)   # профиль 4.0 с центром в x=1
        self.assertAlmostEqual(g["vertices"][-1][2], 3.0)   # выдавливание вверх на 3.0


# ============ Часть 1г: юнит-тесты G4.4 (ВТ-01…08) ============

class TestVTUnit(unittest.TestCase):
    def test_01_stage_commission_rule(self):
        """ВТ-02: файлы ≠ этап; закрытие — только решением комиссии и при готовых материалах."""
        from afina.vt import VTService
        st = mem_store(); vt = VTService(st)
        st.x("INSERT INTO vt_stages VALUES('Э-1','К-1','Этап 1',NULL,0,0,0,0,'{}',NULL,NULL,NULL,?)", (time.time(),))
        r = vt.commission_close("Э-1", "комиссия", "протокол №1")
        self.assertEqual(r["status"], "refused")           # материалы не готовы — комиссия не подменяет комплектность
        for dim in ("изделие", "документация", "испытания", "комплект"):
            vt.mark_dimension("Э-1", dim, True, evidence="акт")
        r2 = vt.commission_close("Э-1", "комиссия", "")
        self.assertEqual(r2["status"], "error")            # без основания — нельзя
        r3 = vt.commission_close("Э-1", "комиссия", "протокол заседания №1")
        self.assertEqual(r3["status"], "closed")
        r4 = vt.mark_dimension("Э-1", "изделие", False)
        self.assertEqual(r4["status"], "error")            # после закрытия измерения не редактируются
        view = vt.stage_view(st.q("SELECT * FROM vt_stages WHERE id='Э-1'")[0])
        self.assertEqual(view["status"], "Закрыт решением комиссии")

    def test_02_metrology_blocks_approval(self):
        """ВТ-03: СИ с просроченной поверкой → результаты недопустимы для подтверждения."""
        from afina.vt import VTService
        st = mem_store(); vt = VTService(st)
        st.x("INSERT INTO vt_benches VALUES('СТ-1','Измеритель X','СИ','паспорт 123',?, 'зарегистрирован',?)",
             (time.time() - 86400, time.time()))   # поверка просрочена вчера
        reg = vt.register_test({"id": "И-1", "object_id": "OBJ-1", "bench_id": "СТ-1", "basis": "RF-03",
                                "results": {"coverage": 91}}, "инженер")
        self.assertIn("недопустимы", reg["metrology_warning"])
        ap = vt.approve_test("И-1", "верификатор", "основание")
        self.assertEqual(ap["status"], "refused")
        # действующая поверка — согласование проходит
        st.x("INSERT INTO vt_benches VALUES('СТ-2','Измеритель Y','СИ','паспорт 124',?, 'зарегистрирован',?)",
             (time.time() + 30 * 86400, time.time()))
        vt.register_test({"id": "И-2", "object_id": "OBJ-1", "bench_id": "СТ-2", "basis": "RF-03"}, "инженер")
        ap2 = vt.approve_test("И-2", "верификатор", "протокол сверки")
        self.assertEqual(ap2["status"], "approved")
        pp = vt.test_passport("И-2")
        for k in ("id", "object", "configuration", "basis", "conditions", "equipment",
                  "results", "deviations", "conclusion", "approval"):
            self.assertIn(k, pp)   # состав паспорта §4.8.5

    def test_03_readiness_no_skipping(self):
        """ВТ-05: уровни формальной готовности не пропускаются «через голову»."""
        from afina.vt import VTService
        st = mem_store(); vt = VTService(st)
        r0 = vt.readiness("P-1")
        self.assertEqual(r0["formal_level"], 0)            # нет объектов
        st.x("INSERT INTO objects(id,project,class,name,x,y,z,attrs,ts_code,dat_status,source,recorded_at,created_by) VALUES('O1','P-1','COM','n',1,2,0,'{}','TS-1','Введено','т',?,'т')", (time.time(),))
        r1 = vt.readiness("P-1")
        self.assertEqual(r1["formal_level"], 1)            # комплектность есть, документации нет
        st.x("INSERT INTO baselines VALUES('BL1','P-1','утверждена',?,'т','h','{}')", (time.time(),))
        r2 = vt.readiness("P-1")
        self.assertEqual(r2["formal_level"], 2)            # L3: реестр требований не загружен → оценка невозможна
        self.assertFalse(r2["gates"][2]["passed"])
        reqs = [{"id": "RF-03"}, {"id": "DGT-002"}]
        st.x("INSERT INTO vt_tests VALUES('И-9','O1','СТ-2','RF-03','','','','{}','{}','{}','[]','','Не согласован',?,'т')", (time.time(),))
        r3 = vt.readiness("P-1", reqs)
        self.assertEqual(r3["formal_level"], 2)            # DGT-002 не покрыто — L3 не пройден
        self.assertIn("DGT-002", r3["gates"][2]["uncovered_sample"])
        self.assertIn("оценка", r3["note"].lower())        # честная маркировка

    def test_04_conditions_and_factors(self):
        """ВТ-06: 10 факторов, сопоставление проект↔факт, оба значения сохраняются."""
        from afina.vt import VTService, FACTORS
        st = mem_store(); vt = VTService(st)
        st.x("INSERT INTO vt_conditions VALUES('У1','O1','Температура',25.0,41.5,'°C','измерение',?)", (time.time(),))
        st.x("INSERT INTO vt_conditions VALUES('У2','O1','Влажность',60.0,61.0,'%','измерение',?)", (time.time(),))
        r = vt.conditions_check("O1")
        by = {x["factor"]: x for x in r["factors"]}
        self.assertEqual(by["Температура"]["status"], "отклонение")
        self.assertEqual(by["Влажность"]["status"], "в допуске")
        self.assertEqual(len(r["missing_factors"]), len(FACTORS) - 2)

    def test_05_aging_and_unification(self):
        """ВТ-07/ВТ-04: риски старения с вариантами реакции; дубли и заимствование."""
        from afina.vt import VTService
        st = mem_store(); vt = VTService(st)
        st.x("INSERT INTO equipment VALUES('EQ-A','COM','Радио А','Завод-1','РФ',1990,2010,'УКВ',10,NULL,'измерено','эксплуатируется','открытый','источник','{}')")
        st.x("INSERT INTO equipment VALUES('EQ-B','COM','Радио А','Завод-1','РФ',2000,NULL,'УКВ',10,NULL,'измерено','эксплуатируется','открытый','источник','{}')")
        st.x("INSERT INTO equipment VALUES('EQ-C','COM','Радио C','Завод-1','РФ',2010,NULL,'КВ',10,NULL,'не измерено','эксплуатируется','открытый','источник','{}')")
        st.x("INSERT INTO objects(id,project,class,name,x,y,z,attrs,source,recorded_at,created_by) VALUES('O1','P-1','COM','n',1,2,0,?,'т',?,'т')",
             (json.dumps({"eq": "EQ-A"}), time.time()))
        ag = vt.aging()
        risks = {r["risk"] for r in ag["risks"]}
        self.assertIn("EOL", risks)
        self.assertIn("Единственный поставщик", risks)     # Завод-1 = 100 % каталога
        self.assertTrue(all(r["reactions"] for r in ag["risks"]))
        un = vt.unification()
        self.assertTrue(any(len(v) > 1 for v in un["duplicates"].values()))   # «Радио А» ×2
        self.assertEqual(un["reuse_top"][0]["equipment"], "EQ-A")
        self.assertTrue(any(c["equipment"] == "EQ-A" and c["affected_objects"] == ["O1"]
                            for c in un["eol_consequences"]))

    def test_06_panel_sections(self):
        """ВТ-08: панель руководителя — 8 разделов из первичных реестров."""
        from afina.vt import VTService
        st = mem_store()
        p = VTService(st).panel()
        for k in ("разработка", "документация", "испытания", "конфигурация",
                  "производство", "надёжность", "стоимость_сроки", "ИБ"):
            self.assertIn(k, p)
        self.assertIn("audit_chain", p["ИБ"])


# ============ Часть 1д: юнит-тесты G4.5 (геймификация §4.11) ============

class TestGameUnit(unittest.TestCase):
    def test_01_award_only_for_verified_result(self):
        """Баллы — за проверяемый результат; неподтверждённое основание отклоняется."""
        from afina.game import GameEngine
        st = mem_store(); g = GameEngine(st)
        r = g.award("u1", "zam_closed_verified", "ЗАМ-НЕТ")
        self.assertEqual(r["status"], "rejected")
        # реальное закрытое замечание с основанием — начисляется
        st.x("INSERT INTO zam VALUES('ЗАМ-1','дефект','Минорный','Орг','RF-03','Закрыто','устранено и проверено','u2',?)", (time.time(),))
        r2 = g.award("u1", "zam_closed_verified", "ЗАМ-1")
        self.assertEqual(r2["status"], "awarded")
        self.assertEqual(r2["points"], 4)
        # повторное начисление заблокировано
        r3 = g.award("u1", "zam_closed_verified", "ЗАМ-1")
        self.assertEqual(r3["status"], "duplicate")

    def test_02_forbidden_grounds(self):
        """Запрещённые основания (§4.11.3) отклоняются архитектурно."""
        from afina.game import GameEngine, FORBIDDEN
        g = GameEngine(mem_store())
        r = g.award("u1", "objects_count", "")
        self.assertEqual(r["status"], "rejected")
        self.assertEqual(r["forbidden_grounds"], FORBIDDEN)
        r2 = g.award("u1", "calc_launched_fast", "")
        self.assertEqual(r2["status"], "rejected")

    def test_03_revoke_with_explanation(self):
        """Отзыв ошибочных баллов — с объяснением и журналированием."""
        from afina.game import GameEngine
        st = mem_store(); g = GameEngine(st)
        st.x("INSERT INTO zam VALUES('ЗАМ-2','д','Минорный','О','R','Закрыто','основание','u2',?)", (time.time(),))
        a = g.award("u1", "zam_closed_verified", "ЗАМ-2")
        self.assertEqual(a["status"], "awarded")
        self.assertEqual(g.revoke(1, "v1", "")["status"], "error")     # без объяснения нельзя
        rv = g.revoke(1, "v1", "начислено ошибочно: замечание закрыл тот же пользователь")
        self.assertEqual(rv["status"], "revoked")
        sc = g.scores()["leaderboard"]
        self.assertEqual(sc[0]["total"], 0)
        self.assertEqual(sc[0]["revoked_n"], 1)
        audit = st.q("SELECT action FROM audit WHERE action LIKE 'отзыв%'")
        self.assertTrue(audit)                                        # корректировка зажурналирована

    def test_04_competency_levels(self):
        """Уровни компетенции §4.11.7; порог — сумма подтверждённых баллов."""
        from afina.game import GameEngine, COMPETENCY
        self.assertEqual(GameEngine.competency_of(0), f"0 {COMPETENCY[0]}")
        self.assertEqual(GameEngine.competency_of(60), f"2 {COMPETENCY[2]}")
        self.assertEqual(GameEngine.competency_of(10 ** 6), f"5 {COMPETENCY[5]}")

    def test_05_polygon_criteria_not_points(self):
        """Полигон: 8 этапов, переход по инженерным критериям; очки не влияют."""
        from afina.game import GameEngine, POLYGON_STAGES
        st = mem_store(); g = GameEngine(st)
        p = g.polygon("P-1")
        self.assertEqual(len(p["stages"]), len(POLYGON_STAGES))
        self.assertEqual(p["current_stage"], 1)          # пусто — этап 1 не пройден
        add_obj(st, "O-1", x=1, y=2)
        st.x("UPDATE objects SET dat_status='Введено', source='инвентаризация' WHERE id='O-1'")
        p2 = g.polygon("P-1")
        self.assertEqual(p2["current_stage"], 3)         # критерии 1–2 выполнены
        self.assertIn("ИНЖЕНЕРНЫМИ", p2["rule"])

    def test_06_readiness_indices_undefined(self):
        """§4.11.8: не рассчитанный показатель — «не определён», а не 0/100 %."""
        from afina.game import GameEngine
        st = mem_store(); g = GameEngine(st)
        st.x("INSERT INTO objects(id,project,class,name,x,y,z,attrs,ts_code,dat_status,source,recorded_at,created_by) VALUES('O-2','P-1','COM','n',1,2,NULL,'{}','TS-0','Не заполнено',NULL,?,'т')", (time.time(),))
        r = g.readiness_indices("O-2")
        self.assertIsNone(r["достоверность"])            # нет источника
        self.assertIsNone(r["эксплуатационная_готовность"])  # нет телеметрии
        self.assertIsNotNone(r["полнота_данных"])


class TestMissionsUnit(unittest.TestCase):
    def test_01_all_missions_deterministic_and_checked(self):
        from afina.game import Missions
        m = Missions()
        for mid in Missions.IDS:
            t1 = m.start(mid, 7)
            t2 = m.start(mid, 7)
            self.assertEqual(t1, t2, mid)                    # детерминированность по seed
            self.assertNotIn("answer", t1)                   # ответ клиенту не выдаётся
            self.assertNotIn("why", t1)
            internal, _ = m._fn(mid)(7)
            good = m.check(mid, 7, internal["answer"])
            self.assertTrue(good["correct"], mid)
            self.assertTrue(good["why"])                     # объяснение «почему верно»
            bad = m.check(mid, 7, "заведомо-неверный-ответ-42")
            if not isinstance(internal["answer"], (list, dict)):
                self.assertFalse(bad["correct"], mid)

    def test_02_hints_three_levels_no_answer_leak(self):
        from afina.game import Missions
        m = Missions()
        for mid in Missions.IDS:
            internal, _ = m._fn(mid)(3)
            ans = str(internal["answer"])
            hs = [m.hint(mid, 3, lv)["hint"] for lv in (1, 2, 3)]
            self.assertEqual(len(set(hs)), 3, mid)           # три разных уровня
            for h in hs:
                if len(ans) > 3:                             # короткие ответы («A», «4») не проверяем на утечку
                    self.assertNotIn(ans, h, f"{mid}: подсказка выдаёт ответ")

    def test_03_unknown_mission(self):
        from afina.game import Missions
        self.assertEqual(Missions().start("нет_такой", 1)["status"], "error")


# ============ Часть 2: сквозные HTTP-тесты G4 ============
PORT = 8496
BASE = f"http://127.0.0.1:{PORT}/api/v1"


def call(method, path, body=None, token=None, expect=None):
    data = json.dumps(body).encode() if body is not None else None
    path = urllib.parse.quote(path, safe="/?&=%")
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            code, res = r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        code, res = e.code, json.loads(e.read().decode() or "{}")
    if expect is not None:
        assert code == expect, f"{method} {path}: ожидался {expect}, получен {code}: {res}"
    return code, res


class TestG4E2E(unittest.TestCase):
    httpd = None

    @classmethod
    def setUpClass(cls):
        from afina.server.app import create_server
        from afina.catalog import seed
        cls.td = tempfile.TemporaryDirectory()
        db = os.path.join(cls.td.name, "g4.db")
        cls.httpd, cls.app = create_server(db_path=db, port=PORT)
        seed.build(cls.app.store)  # каталог средств связи и ОШС (п.14) — нужен ВТ-04/07
        cls.th = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.th.start()
        time.sleep(0.3)
        call("POST", "/register", {"login": "admin", "password": "s3cr3t!", "role": "Администратор системы"}, expect=200)
        cls.at = call("POST", "/login", {"login": "admin", "password": "s3cr3t!"}, expect=200)[1]["token"]
        call("POST", "/register", {"login": "eng1", "password": "p1", "role": "Инженер-расчётчик"}, token=cls.at, expect=200)
        call("POST", "/register", {"login": "ver1", "password": "p2", "role": "Инженер-верификатор"}, token=cls.at, expect=200)
        call("POST", "/register", {"login": "appr1", "password": "p3", "role": "Уполномоченный утверждающий"}, token=cls.at, expect=200)
        call("POST", "/register", {"login": "pm1", "password": "p5", "role": "Руководитель программы"}, token=cls.at, expect=200)
        cls.et = call("POST", "/login", {"login": "eng1", "password": "p1"}, expect=200)[1]["token"]
        cls.vt = call("POST", "/login", {"login": "ver1", "password": "p2"}, expect=200)[1]["token"]
        cls.ap = call("POST", "/login", {"login": "appr1", "password": "p3"}, expect=200)[1]["token"]
        cls.pt = call("POST", "/login", {"login": "pm1", "password": "p5"}, expect=200)[1]["token"]

    @classmethod
    def tearDownClass(cls):
        if cls.httpd:
            cls.httpd.shutdown(); cls.httpd.server_close()
        cls.td.cleanup()

    def test_01_scenario_e2e_and_dt5(self):
        call("POST", "/objects", {"id": "OBJ-G4", "class": "COM", "x": 10, "y": 20, "client_id": "g4-1"},
             token=self.et, expect=200)
        for i, v in enumerate([50, 52, 54, 56]):
            call("POST", "/telemetry", {"object_id": "OBJ-G4", "metric": "load_pct", "value": v, "ts": time.time() + i},
                 token=self.et, expect=200)
        r = call("POST", "/twins/OBJ-G4/scenario", {"kind": "param", "params": {"metric": "load_pct", "delta_pct": 20, "limit": 80}},
                 token=self.et, expect=200)[1]
        self.assertEqual(r["status"], "done")
        self.assertEqual(r["marked"], "оценка (не факт)")
        lst = call("GET", "/scenarios?object_id=OBJ-G4", token=self.et, expect=200)[1]["scenarios"]
        self.assertEqual(len(lst), 1)
        # валидация сценария — только роль с правом verify (RBAC)
        code, _ = call("POST", f"/scenarios/{lst[0]['id']}/validate", {"basis": "сверка"}, token=self.et)
        self.assertEqual(code, 403)
        v = call("POST", f"/scenarios/{lst[0]['id']}/validate", {"basis": "сверка с ручным расчётом"},
                 token=self.vt, expect=200)[1]
        self.assertEqual(v["status"], "validated")

    def test_02_ai_explain_suggest_and_disable(self):
        e = call("POST", "/ai/explain", {"passport": {"function": "RF-03", "model_id": "SPM", "model_version": "1.0.0",
                                                      "status": "done", "inputs": {}, "uncertainty_db": {"data": 2.0}}},
                 token=self.et, expect=200)[1]
        self.assertEqual(e["status"], "ok")
        self.assertTrue(e["limitations"])
        sg = call("POST", "/ai/suggest", {"object_id": "OBJ-G4"}, token=self.et, expect=200)[1]
        self.assertEqual(sg["status"], "ok")
        self.assertTrue(sg["suggestions"])
        # AI-003: отключение ИИ не ломает ГИС/ядро
        self.app.ai_enabled = False
        d = call("POST", "/ai/explain", {"passport": {}}, token=self.et, expect=200)[1]
        self.assertEqual(d["status"], "disabled")
        d2 = call("POST", "/ai/ask", {"q": "что такое RF-03?"}, token=self.et, expect=200)[1]
        self.assertEqual(d2["status"], "disabled")
        h = call("GET", "/twins/OBJ-G4", token=self.et, expect=200)[1]
        self.assertIn("dt", h)  # двойник работает без ИИ
        self.app.ai_enabled = True

    def test_03_opt01_lcc01_as_service(self):
        """G4.2: OPT-01 и LCC-01 — через очередь расчётов, с паспортом и аудитом."""
        j = call("POST", "/calc", {"function": "OPT-01",
                                   "variants": [{"id": "В1", "cost": 100, "cover": 80, "vendor": "РФ"},
                                                {"id": "В2", "cost": 40, "cover": 97, "vendor": "неизвестен"},
                                                {"id": "В3", "cost": 130, "cover": 75, "vendor": "РФ"}],
                                   "criteria": [{"key": "cost", "name": "Стоимость", "direction": "min", "weight": 2, "hard_max": 150},
                                                {"key": "cover", "name": "Покрытие %", "direction": "max", "weight": 1, "hard_min": 60}],
                                   "constraints": [{"type": "subset_of", "field": "vendor", "values": ["РФ"]}]},
                 token=self.et, expect=202)[1]
        for _ in range(50):
            st = call("GET", f"/calc/{j['job_id']}", token=self.et, expect=200)[1]
            if st["status"] in ("done", "error"): break
            time.sleep(0.1)
        self.assertEqual(st["status"], "done")
        res = st["result"]
        self.assertEqual(res["feasible_count"], 2)
        self.assertEqual(res["rejected"][0]["variant"]["id"], "В2")
        self.assertIn("вне допустимого множества", res["rejected"][0]["reason"])
        self.assertNotIn("В2", res["pareto_front"])
        self.assertEqual(res["passport"]["function"], "OPT-01")
        # LCC-01
        j2 = call("POST", "/calc", {"function": "LCC-01", "capex": 1000000, "years": 10,
                                    "opex_per_year": 120000, "discount": 0.08},
                  token=self.et, expect=202)[1]
        for _ in range(50):
            st2 = call("GET", f"/calc/{j2['job_id']}", token=self.et, expect=200)[1]
            if st2["status"] in ("done", "error"): break
            time.sleep(0.1)
        self.assertEqual(st2["status"], "done")
        self.assertIn("tco", st2["result"])
        self.assertIn("tco_sensitivity", st2["result"])
        # незарегистрированная функция по-прежнему отклоняется (тихая подмена запрещена)
        call("POST", "/calc", {"function": "MAGIC-99"}, token=self.et, expect=400)

    def test_04_cosim01_e2e(self):
        """G4.3: Co-Simulation через очередь — ID сценария, единицы, версии, ошибка решателя."""
        j = call("POST", "/calc", {"function": "COSIM-01", "scenario_id": "CS-DEMO-1",
                                   "seed": {"w_nom": 2500, "w_peak": 4200, "t_ambient_c": 33,
                                            "t_limit_c": 40, "cooling_w": 2400, "mtbf_h": 9000, "mttr_h": 5}},
                 token=self.et, expect=202)[1]
        for _ in range(60):
            st = call("GET", f"/calc/{j['job_id']}", token=self.et, expect=200)[1]
            if st["status"] in ("done", "error"): break
            time.sleep(0.1)
        self.assertEqual(st["status"], "done")
        res = st["result"]
        self.assertTrue(res["ok"])
        self.assertEqual(res["scenario_id"], "CS-DEMO-1")
        self.assertEqual(res["versions"]["PWR-01"], "1.0.0")
        self.assertTrue(res["data"]["t_cabinet_c"] > 33)
        self.assertIn("availability", res["data"])
        self.assertEqual(res["unit_checks"], "единицы согласованы на всех связках (ENG-004)")
        # неполный seed — ошибка решателя НЕ подменяется значением по умолчанию
        j2 = call("POST", "/calc", {"function": "COSIM-01", "seed": {"w_nom": 1000}},
                  token=self.et, expect=202)[1]
        for _ in range(60):
            st2 = call("GET", f"/calc/{j2['job_id']}", token=self.et, expect=200)[1]
            if st2["status"] in ("done", "error"): break
            time.sleep(0.1)
        r2 = st2.get("result") or {}
        self.assertFalse(r2.get("ok", True))
        self.assertIn("не хватает входов", str(r2.get("log")))

    def test_05_surrogate_service_e2e(self):
        """G4.3: суррогат как сервис — обучение, домен, сравнение, валидация, отключение."""
        from afina.eng6.surrogate import rf01_solver
        solver = rf01_solver("OKUMURA_HATA")
        pts = [[f, d, hb, hm, solver([f, d, hb, hm])]
               for f in (200, 700, 1300) for d in (1, 5, 12) for hb in (30, 80) for hm in (1.5, 3)]
        tr = call("POST", "/surrogate/train", {"id": "SUR-E2E", "model_ref": "RF-01/OKUMURA_HATA",
                                               "degree": 2, "feature_names": ["f_mhz", "d_km", "h_bs", "h_ms"],
                                               "points": pts}, token=self.et, expect=200)[1]
        self.assertEqual(tr["status"], "trained")
        self.assertIn("СУРРОГАТ", tr["marked"])
        p = call("POST", "/surrogate/predict", {"id": "SUR-E2E", "x": [700, 5, 80, 2]}, token=self.et, expect=200)[1]
        self.assertEqual(p["status"], "ok")
        o = call("POST", "/surrogate/predict", {"id": "SUR-E2E", "x": [3000, 5, 80, 2]}, token=self.et, expect=200)[1]
        self.assertEqual(o["status"], "out_of_domain")
        # валидация без сравнения невозможна (честный порядок)
        call("POST", "/surrogates/SUR-E2E/validate", {"basis": "хочу"}, token=self.vt, expect=400)
        c = call("POST", "/surrogates/SUR-E2E/compare", {"test_points": [[600, 4, 60, 2], [900, 8, 100, 3]]},
                 token=self.et, expect=200)[1]
        self.assertEqual(c["status"], "compared")
        v = call("POST", "/surrogates/SUR-E2E/validate", {"basis": f"RMSE(незав.)={c['rmse_independent']} ≤ порога"},
                 token=self.vt, expect=200)[1]
        self.assertEqual(v["status"], "Валидирована для указанной области")
        # отключение (возможность отключения §4.5.8) — роль Администратор данных
        call("POST", "/register", {"login": "dadm1", "password": "p4", "role": "Администратор данных"},
             token=self.at, expect=200)
        dtoken = call("POST", "/login", {"login": "dadm1", "password": "p4"}, expect=200)[1]["token"]
        call("POST", "/surrogates/SUR-E2E/disable", {"disabled": True}, token=dtoken, expect=200)
        d = call("POST", "/surrogate/predict", {"id": "SUR-E2E", "x": [700, 5, 80, 2]}, token=self.et, expect=200)[1]
        self.assertEqual(d["status"], "disabled")
        lst = call("GET", "/surrogates", token=self.et, expect=200)[1]
        self.assertEqual(lst["surrogates"][0]["disabled"], 1)

    def test_06_vt_profile_e2e(self):
        """G4.4: ВТ-01…08 через REST — роли, комиссия, метрология, панель."""
        call("POST", "/register", {"login": "dadm2", "password": "p6", "role": "Администратор данных"},
             token=self.at, expect=200)
        dmt = call("POST", "/login", {"login": "dadm2", "password": "p6"}, expect=200)[1]["token"]
        # ВТ-01: участники и передача комплекта
        call("POST", "/vt/participants", {"id": "ORG-1", "org": "НПО «Связь»", "role": "головной",
                                          "zone": "разработка РЭС"}, token=dmt, expect=200)
        tr = call("POST", "/vt/transfers", {"package_name": "комплект ПД-1", "from_org": "НПО «Связь»",
                                            "to_org": "ЗАКАЗЧИК", "category": "ВНУТРЕННЯЯ", "items": 3},
                  token=self.et, expect=200)[1]
        self.assertEqual(len(tr["sha256"]), 64)
        coop = call("GET", "/vt/cooperation", token=self.pt, expect=200)[1]
        self.assertEqual(len(coop["participants"]), 1)
        # ВТ-02: этап закрывает ТОЛЬКО уполномоченный и ТОЛЬКО при готовых материалах
        call("POST", "/vt/stages", {"id": "Э-100", "contract": "К-2026", "title": "Эскизный проект"},
             token=self.pt, expect=200)
        call("POST", "/vt/stages/Э-100/commission", {"basis": "протокол"}, token=self.ap, expect=409)  # материалы не готовы
        for dim in ("изделие", "документация", "испытания", "комплект"):
            call("POST", "/vt/stages/Э-100/mark", {"dimension": dim, "value": True, "evidence": "акт"},
                 token=self.et, expect=200)
        call("POST", "/vt/stages/Э-100/commission", {"basis": "протокол"}, token=self.et, expect=403)  # роль без approve
        call("POST", "/vt/stages/Э-100/commission", {"basis": "протокол заседания комиссии №4"},
             token=self.ap, expect=200)
        stages = call("GET", "/vt/stages", token=self.pt, expect=200)[1]["stages"]
        self.assertEqual(stages[0]["status"], "Закрыт решением комиссии")
        # ВТ-03: стенд с просроченной поверкой блокирует согласование
        call("POST", "/vt/benches", {"id": "СТ-100", "name": "Анализатор спектра", "kind": "СИ",
                                     "passport": "ПС-1", "calib_due": time.time() - 100}, token=dmt, expect=200)
        reg = call("POST", "/vt/tests", {"id": "И-100", "object_id": "OBJ-G4", "bench_id": "СТ-100",
                                         "basis": "RF-03", "results": {"coverage_pct": 92}}, token=self.et, expect=200)[1]
        self.assertIn("недопустимы", reg["metrology_warning"])
        call("POST", "/vt/tests/И-100/approve", {"basis": "х"}, token=self.vt, expect=409)
        pp = call("GET", "/vt/tests/И-100/passport", token=self.et, expect=200)[1]
        self.assertEqual(pp["id"], "И-100")
        self.assertIn("просрочена", pp["equipment"]["metrology"])
        # ВТ-04/07: унификация и старение по загруженному каталогу
        un = call("GET", "/vt/unification", token=self.et, expect=200)[1]
        self.assertIn("reuse_rate_pct", un)
        ag = call("GET", "/vt/aging", token=self.et, expect=200)[1]
        self.assertTrue(ag["risks"])   # каталог 1940–2036: EOL и неподтверждённые данные обязаны найтись
        self.assertTrue(all("reactions" in r for r in ag["risks"]))
        # ВТ-05/06/08
        rd = call("GET", "/vt/readiness", token=self.pt, expect=200)[1]
        self.assertIn(rd["formal_level"], (0, 1, 2, 3, 4))
        self.assertEqual(len(rd["gates"]), 4)
        call("POST", "/vt/conditions", {"object_id": "OBJ-G4", "factor": "Температура",
                                        "project_value": 25.0, "actual_value": 44.0, "unit": "°C"},
             token=self.et, expect=200)
        cc = call("GET", "/vt/conditions?object_id=OBJ-G4", token=self.et, expect=200)[1]
        self.assertEqual(cc["factors"][0]["status"], "отклонение")
        pn = call("GET", "/vt/panel", token=self.pt, expect=200)[1]
        for k in ("разработка", "документация", "испытания", "конфигурация",
                  "производство", "надёжность", "стоимость_сроки", "ИБ"):
            self.assertIn(k, pn)
        self.assertGreaterEqual(pn["производство"]["equipment"], 49)   # каталог п.14 загружен

    def test_07_game_and_missions_e2e(self):
        """G4.5: баллы за проверяемый результат, дубли, отзыв, миссии, полигон, индексы."""
        rules = call("GET", "/game/rules", token=self.et, expect=200)[1]
        self.assertIn("zam_closed_verified", rules["points"])
        self.assertEqual(len(rules["forbidden_grounds"]), 6)
        # запрещённое основание
        rj = call("POST", "/game/award", {"event": "objects_count"}, token=self.et, expect=200)[1]
        self.assertEqual(rj["status"], "rejected")
        # проверяемый результат: закрываем замечание с основанием → баллы
        call("POST", "/zam", {"id": "ЗАМ-G4", "title": "дефект топологии", "crit": "Минорный"}, token=self.et, expect=200)
        call("POST", "/zam/ЗАМ-G4/advance", {"to": "Закрыто", "basis": "устранено, повторная проверка выполнена"},
             token=self.et, expect=200)
        aw = call("POST", "/game/award", {"event": "zam_closed_verified", "ref": "ЗАМ-G4"}, token=self.et, expect=200)[1]
        self.assertEqual(aw["status"], "awarded")
        aw2 = call("POST", "/game/award", {"event": "zam_closed_verified", "ref": "ЗАМ-G4"}, token=self.et, expect=200)[1]
        self.assertEqual(aw2["status"], "duplicate")
        # отзыв: только роль verify и с объяснением
        call("POST", "/game/revoke", {"award_id": 1, "reason": "ошибка"}, token=self.et, expect=403)
        rv = call("POST", "/game/revoke", {"award_id": 1, "reason": "замечание закрыто без повторной проверки"},
                  token=self.vt, expect=200)[1]
        self.assertEqual(rv["status"], "revoked")
        sc = call("GET", "/game/scores", token=self.et, expect=200)[1]
        self.assertEqual(sum(x["total"] for x in sc["leaderboard"]), 0)
        # миссии: старт не выдаёт ответ, проверка объясняет «почему»
        ms = call("GET", "/game/missions", token=self.et, expect=200)[1]
        self.assertEqual(len(ms["missions"]), 9)
        t = call("POST", "/game/missions/собери_узел/start", {"seed": 7}, token=self.et, expect=200)[1]
        self.assertNotIn("answer", t)
        bad = call("POST", "/game/missions/собери_узел/check", {"seed": 7, "answer": "БП-999"}, token=self.et, expect=200)[1]
        self.assertFalse(bad["correct"])
        self.assertTrue(bad["why"])
        h1 = call("POST", "/game/missions/собери_узел/hint", {"seed": 7, "level": 1}, token=self.et, expect=200)[1]
        h2 = call("POST", "/game/missions/собери_узел/hint", {"seed": 7, "level": 2}, token=self.et, expect=200)[1]
        self.assertNotEqual(h1["hint"], h2["hint"])
        # полигон и индексы готовности ЦД
        pg = call("GET", "/game/polygon", token=self.et, expect=200)[1]
        self.assertEqual(len(pg["stages"]), 8)
        ix = call("GET", "/game/indices?object_id=OBJ-G4", token=self.et, expect=200)[1]
        for k in ("полнота_данных", "достоверность", "зрелость_модели", "совместимость", "эксплуатационная_готовность"):
            self.assertIn(k, ix)

    def test_08_analytics_and_demo_train(self):
        """G4.6: тренды из первичных реестров; demo-обучение суррогата на решателе RF-01."""
        t = call("GET", "/analytics/trends?days=7", token=self.pt, expect=200)[1]
        self.assertEqual(t["days"], 7)
        for k in ("объекты", "расчёты", "замечания", "изменения", "сценарии", "испытания", "иб_отказы"):
            self.assertIn(k, t["series"])
        self.assertGreater(t["totals"]["объекты"], 0)     # OBJ-G4 создан в test_01
        tr = call("POST", "/surrogate/demo_train", {"model_key": "OKUMURA_HATA"}, token=self.et, expect=200)[1]
        self.assertEqual(tr["status"], "trained")
        self.assertEqual(tr["n_points"], 225)
        self.assertLess(tr["rmse"], 6.0)
        p = call("POST", "/surrogate/predict", {"id": tr["id"], "x": [900, 6, 70, 2]}, token=self.et, expect=200)[1]
        self.assertEqual(p["status"], "ok")
        self.assertIn("СУРРОГАТ", p["marked"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
