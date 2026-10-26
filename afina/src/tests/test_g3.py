"""Тесты G3: цифровые двойники (DGT-001…005), конфигурация (CFG-002…004),
уведомления (NTF-002/003), качество данных (DAT-001…006), 3D-загрузчики.

Часть 1 — юнит-тесты модулей на Store в памяти.
Часть 2 — сквозные HTTP-тесты новых эндпоинтов (порт 8497).
"""
import base64, hashlib, json, os, struct, sys, tempfile, threading, time, unittest
import urllib.request, urllib.error, urllib.parse

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from afina.server.core import Store
from afina.twin import TwinService, DT_CRITERIA
from afina.cfg import DependencyGraph, eval_rules, DEFAULT_RULES, CHANGE_STATES
from afina.ntf import NotifyService
from afina.dat import validate_object, find_duplicates, ConflictRegistry, \
    create_reproducibility_archive, migration_hash_check
from afina.gis import models3d


def mem_store():
    st = Store(":memory:")
    return st


def add_obj(st, oid, x=None, y=None, cls="COM", name=None, recorded_at=None):
    st.x("INSERT INTO objects(id,project,class,name,x,y,z,attrs,source,recorded_at,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
         (oid, "P-1", cls, name or oid, x, y, None, "{}", "тест", recorded_at, "тест"))


def add_calc(st, run_id, payload_str):
    st.x("""INSERT INTO calcs(run_id,function,model,mode,level,inputs_hash,payload,status,result_ref,passport,created,finished,created_by)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
         (run_id, "RF-03", "SPM", "Engineering", "L1", "h", payload_str, "done", None, "{}", time.time(), time.time(), "тест"))


# ============ Часть 1: юнит-тесты ============

class TestTwin(unittest.TestCase):
    def test_01_dt_level_by_criteria(self):
        """DGT-004: уровень присваивается по проверяемым критериям, не «через голову»."""
        st = mem_store()
        tw = TwinService(st)
        add_obj(st, "OBJ-A")  # без координат
        self.assertEqual(tw.dt_level("OBJ-A")["level"], "DT-0")
        add_obj(st, "OBJ-B", x=100, y=200)
        self.assertEqual(tw.dt_level("OBJ-B")["level"], "DT-1")
        add_calc(st, "RUN-1", json.dumps({"stations": ["OBJ-B"]}))
        self.assertEqual(tw.dt_level("OBJ-B")["level"], "DT-2")
        st.x("INSERT INTO telemetry(object_id,ts,metric,value,quality) VALUES(?,?,?,?,?)",
             ("OBJ-B", time.time(), "rssi_dbm", -71.5, "наблюдение"))
        self.assertEqual(tw.dt_level("OBJ-B")["level"], "DT-3")
        # критерии прозрачны
        lev = tw.dt_level("OBJ-B")
        self.assertTrue(all(k in lev["criteria"] for k in ("DT-0", "DT-1", "DT-2", "DT-3")))
        self.assertEqual(len(DT_CRITERIA), 6)

    def test_02_observed_does_not_change_baseline(self):
        """DGT-002: телеметрия обновляет наблюдаемое состояние, базовая линия не изменяется."""
        st = mem_store()
        tw = TwinService(st)
        add_obj(st, "OBJ-C", x=1, y=2)
        st.x("INSERT INTO telemetry(object_id,ts,metric,value) VALUES(?,?,?,?)", ("OBJ-C", time.time(), "u_v", 220.1))
        obs = tw.observed("OBJ-C")
        self.assertAlmostEqual(obs["observed"]["u_v"]["value"], 220.1)
        self.assertIn("DGT-002", obs["note"])
        row = st.q("SELECT x,y FROM objects WHERE id='OBJ-C'")[0]
        self.assertEqual((row["x"], row["y"]), (1, 2))  # паспорт/база не тронуты

    def test_03_divergence_protocol(self):
        """DGT-003: протокол расхождений сохраняет ОБА значения; отсутствие наблюдений ≠ ноль."""
        st = mem_store()
        tw = TwinService(st)
        add_obj(st, "OBJ-D")
        st.x("INSERT INTO telemetry(object_id,ts,metric,value) VALUES(?,?,?,?)", ("OBJ-D", time.time(), "power_w", 44.0))
        proto = tw.divergence("OBJ-D", {"power_w": 50.0, "height_m": 30.0})
        self.assertEqual(len(proto["rows"]), 2)
        by_m = {r["metric"]: r for r in proto["rows"]}
        self.assertEqual(by_m["power_w"]["status"], "расхождение")
        self.assertAlmostEqual(by_m["power_w"]["delta"], -6.0)
        self.assertEqual(by_m["height_m"]["status"], "нет наблюдений")  # EQP-001: не подменяется нулём
        self.assertIsNone(by_m["height_m"]["observed"])

    def test_04_calibration_separation_and_refusal(self):
        """DGT-005: идентифицированные параметры отдельно от паспорта; неинформативность → честный отказ."""
        short = TwinService.calibrate_ls([(1, 2), (2, 4)])
        self.assertEqual(short["status"], "insufficient_data")  # отказ, а не выдуманная оценка
        pts = [(1, 2.9), (2, 5.1), (3, 7.0), (4, 8.9), (5, 11.2)]
        res = TwinService.calibrate_ls(pts, passport={"a": 2.0, "b": 1.0})
        self.assertEqual(res["status"], "identified")
        self.assertAlmostEqual(res["a"], 2.03, places=1)
        self.assertTrue(res["passport_unchanged"])
        self.assertEqual(res["passport"], {"a": 2.0, "b": 1.0})  # паспорт НЕ изменён
        self.assertTrue(res["validation_required"].startswith("проверка"))

    def test_05_predict_marked_as_estimate(self):
        """§4.6.3: прогноз — оценка с горизонтом и неопределённостью, требует независимой проверки."""
        hist = [(1, 10.0), (2, 12.1), (3, 13.9), (4, 16.2)]
        p = TwinService.predict_linear(hist, horizon_steps=3)
        self.assertEqual(p["status"], "forecast")
        self.assertEqual(p["type"], "оценка (не факт)")
        self.assertTrue(p["requires_independent_validation"])
        self.assertEqual(len(p["points"]), 3)
        self.assertIn("RMSE", p["uncertainty"])
        short = TwinService.predict_linear([(1, 2)])
        self.assertEqual(short["status"], "insufficient_data")  # прогноз без данных не выдаётся


class TestCfg(unittest.TestCase):
    def test_01_impact_graph_bfs(self):
        """CFG-002: анализ влияния до утверждения — полный граф зависимостей."""
        g = DependencyGraph()
        g.add("OBJ-1", "CALC-1", "object", "calc")
        g.add("CALC-1", "DOC-1", "calc", "doc")
        g.add("OBJ-1", "TEST-1", "object", "test")
        imp = g.impact("OBJ-1")
        ids = {a["id"] for a in imp["affected"]}
        self.assertEqual(ids, {"CALC-1", "DOC-1", "TEST-1"})
        self.assertEqual(imp["counts"]["calc"], 1)
        paths = {a["id"]: a["path"] for a in imp["affected"]}
        self.assertIn("OBJ-1", paths["DOC-1"])  # транзитивный путь виден

    def test_02_rules_declarative(self):
        """CFG-004: правила — данные; отсутствующее поле НЕ трактуется как соответствие."""
        ok = eval_rules({"szz_margin_m": 12, "szz_required_m": 10, "f1_block_pct": 20,
                         "poe_drop_pct": 2, "redundancy_ok": True})
        self.assertEqual(ok["overall"], "PASS")
        bad = eval_rules({"szz_margin_m": 5, "szz_required_m": 10, "f1_block_pct": 55})
        self.assertEqual(bad["overall"], "FAIL")
        v_ids = {v["rule"] for v in bad["violations"]}
        self.assertIn("RULE-014", v_ids); self.assertIn("RULE-022", v_ids)
        # poe_drop_pct и redundancy_ok отсутствуют → предупреждение/нарушение, но НЕ «соответствует»
        missing = {p["rule"] for p in bad["passed"]}
        self.assertNotIn("RULE-031", missing); self.assertNotIn("RULE-047", missing)
        # у каждого правила: id, версия, класс, обоснование, автор (реестр)
        for r in DEFAULT_RULES:
            for k in ("id", "version", "class", "basis", "author"):
                self.assertIn(k, r)

    def test_03_change_states(self):
        self.assertEqual(CHANGE_STATES, ["Предложено", "Внесено", "Проверено", "Утверждено"])


class TestNtf(unittest.TestCase):
    def test_01_dedup_window(self):
        """NTF-002: лавина дубликатов подавляется группировкой в окне."""
        st = mem_store()
        ns = NotifyService(st)
        r1 = ns.notify("warn", "Обрыв ВОЛС", "событие 1", dedup_key="link-7")
        r2 = ns.notify("warn", "Обрыв ВОЛС", "событие 2", dedup_key="link-7")
        self.assertFalse(r1["deduplicated"]); self.assertTrue(r2["deduplicated"])
        rows = st.q("SELECT * FROM notifications WHERE dedup_key='link-7'")
        self.assertEqual(len(rows), 1)
        self.assertIn("сгруппировано", rows[0]["body"])
        # другой ключ — отдельное уведомление
        r3 = ns.notify("warn", "Обрыв ВОЛС", "событие 3", dedup_key="link-8")
        self.assertFalse(r3["deduplicated"])

    def test_02_lifecycle_separate_statuses(self):
        """§4.12: создание ≠ доставка ≠ просмотр ≠ исполнение."""
        st = mem_store()
        ns = NotifyService(st)
        nid = ns.notify("info", "Задача назначена")["id"]
        s1 = ns.advance(nid, "u1", "Проектировщик")
        self.assertEqual((s1["from"], s1["to"]), ("Создано", "В очереди"))
        s2 = ns.advance(nid, "u1", "Проектировщик")
        self.assertEqual(s2["to"], "Доставлено")

    def test_03_escalation(self):
        """NTF-003: необработанное критическое эскалируется по правилам."""
        st = mem_store()
        ns = NotifyService(st)
        nid = ns.notify("критическое", "Отказ СКЗИ", "")["id"]
        res = ns.escalation_scan()
        self.assertEqual(res["escalated"], [])  # свежее — не эскалируется
        st.x("UPDATE notifications SET created=created-7200 WHERE id=?", (nid,))  # 2 часа назад
        res = ns.escalation_scan(now=time.time())
        self.assertEqual(len(res["escalated"]), 1)
        self.assertEqual(res["escalated"][0]["to"], "дежурный")
        aud = st.q("SELECT action FROM audit WHERE class='sec' ORDER BY id DESC LIMIT 5")
        self.assertTrue(any("эскалация" in a["action"] for a in aud))


class TestDat(unittest.TestCase):
    def test_01_validation_blocks_publish(self):
        """DAT-002 + EQP-001: диапазоны, связанные параметры, состояния значений."""
        obj = {"id": "X1", "class": "COM", "name": "Р-1", "recorded_at": time.time(),
               "attrs": {"power_w": 5000, "масса_кг": 10, "транспортная_масса_кг": 5}}
        res = validate_object(obj)
        self.assertTrue(res["publish_blocked"])
        problems = " ".join(e["problem"] for e in res["errors"])
        self.assertIn("вне диапазона", problems)
        self.assertIn("противоречие связанных параметров", problems)
        # EQP-001: отсутствие значения — информационный вердикт, не ноль
        self.assertTrue(any("EQP-001" in i["problem"] for i in res["info"]))
        # устаревшие данные → предупреждение DAT/TS-8
        old = {"id": "X2", "class": "COM", "name": "Р-2", "recorded_at": time.time() - 200 * 86400, "attrs": {}}
        r2 = validate_object(old)
        self.assertTrue(any("актуальность истекла" in w["problem"] for w in r2["warnings"]))

    def test_02_duplicates_to_arbitration(self):
        """DAT: слияние дубликатов НЕ автоматическое (§6.1)."""
        objs = [{"id": "A", "name": "РЛС Дон-2Н", "class": "COM"},
                {"id": "B", "name": "рлс дон-2н", "class": "COM"},
                {"id": "C", "name": "Другое", "class": "COM"}]
        dups = find_duplicates(objs)
        self.assertEqual(len(dups), 1)
        self.assertEqual(sorted(dups[0]["candidates"]), ["A", "B"])
        self.assertIn("арбитраж", dups[0]["action"])

    def test_03_conflict_registry_and_arbitration(self):
        """DAT-004: конфликт хранит ВСЕ значения; решает уполномоченный с основанием."""
        st = mem_store()
        cr = ConflictRegistry(st)
        vals = [{"value": 50.0, "source_id": "S1", "recorded_at": time.time()},
                {"value": 44.0, "source_id": "S2", "recorded_at": time.time()}]
        reg = cr.register("OBJ-9", "power_w", vals)
        self.assertEqual(reg["status"], "КОНФЛИКТ")
        self.assertEqual(reg["values_preserved"], 2)
        # чужая роль — отказ
        bad = cr.resolve(reg["id"], "S2", "ivanov", "Проектировщик", "мне так кажется")
        self.assertIn("error", bad)
        # без основания — отказ даже уполномоченному
        bad2 = cr.resolve(reg["id"], "S2", "petrov", "Уполномоченный утверждающий", "")
        self.assertIn("error", bad2)
        # уполномоченный с основанием
        ok = cr.resolve(reg["id"], "S2", "petrov", "Уполномоченный утверждающий", "протокол измерения №14")
        self.assertEqual(ok["status"], "РЕШЕНО")
        row = st.q("SELECT * FROM conflicts WHERE id=?", (reg["id"],))[0]
        self.assertEqual(len(json.loads(row["vals"])), 2)  # проигравшее значение НЕ удалено

    def test_04_reproducibility_archive(self):
        """DAT-006: пакет воспроизводимости с контрольной суммой."""
        st = mem_store()
        content = json.dumps({"objects": 1}, sort_keys=True)
        h = hashlib.sha256(content.encode()).hexdigest()
        st.x("INSERT INTO baselines VALUES(?,?,?,?,?,?,?)", ("BL-T1", "P-1", "утверждена", time.time(), "tester", h, content))
        add_calc(st, "RUN-9", "{}")
        with tempfile.TemporaryDirectory() as td:
            res = create_reproducibility_archive(st, "BL-T1", td)
            self.assertTrue(os.path.exists(res["path"]))
            raw = open(res["path"], encoding="utf-8").read()
            self.assertEqual(hashlib.sha256(raw.encode()).hexdigest(), res["sha256"])
            pkg = json.loads(raw)
            self.assertEqual(pkg["baseline"]["id"], "BL-T1")
            self.assertIn("engine_versions", pkg)
            self.assertEqual(pkg["calcs"][0]["run_id"], "RUN-9")
            self.assertEqual(create_reproducibility_archive(st, "BL-NETU", td).get("error") is not None, True)

    def test_05_migration_hash(self):
        """DAT-005: хэши до/после миграции — исходники не повреждены."""
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "source.dat")
            open(p, "wb").write(b"\x01\x02\x03")
            before = {p: hashlib.sha256(b"\x01\x02\x03").hexdigest()}
            self.assertTrue(migration_hash_check([p], before)["ok"])
            open(p, "wb").write(b"\x01\x02\x04")  # повреждение
            res = migration_hash_check([p], before)
            self.assertFalse(res["ok"])
            self.assertEqual(res["mismatch"], [p])


class TestModels3D(unittest.TestCase):
    OBJ = "o Узел1\nv 0 0 0\nv 1 0 0\nv 1 1 0\nvn 0 0 1\nf 1 2 3\n"

    def test_01_obj_roundtrip(self):
        m = models3d.load_obj(self.OBJ)
        self.assertEqual(m["name"], "Узел1")
        self.assertEqual(m["vertex_count"], 3)
        self.assertEqual(m["faces"], [[0, 1, 2]])
        m2 = models3d.load_obj(models3d.export_obj(m))
        self.assertEqual(m2["vertices"], m["vertices"])  # round-trip (GIS-004)

    def test_02_gltf_embedded(self):
        data = struct.pack("<12f", 0, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0)
        b64 = base64.b64encode(data).decode()
        gltf = {"buffers": [{"uri": "data:application/octet-stream;base64," + b64}],
                "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(data)}],
                "accessors": [{"bufferView": 0, "componentType": 5126, "count": 4, "type": "VEC3"}],
                "meshes": [{"name": "квад", "primitives": [{"attributes": {"POSITION": 0}}]}]}
        res = models3d.load_gltf(gltf)
        self.assertEqual(res["meshes"][0]["vertex_count"], 4)
        self.assertAlmostEqual(res["meshes"][0]["vertices"][2][1], 1.0)

    def test_03_gltf_external_buffer_error(self):
        """Запрет тихой деградации: внешний буфер без резолвера — ошибка, не пустой меш."""
        gltf = {"buffers": [{"uri": "mesh.bin"}], "bufferViews": [], "accessors": [], "meshes": []}
        with self.assertRaises(ValueError):
            models3d.load_gltf(gltf)

    def test_04_citygml_lod1(self):
        xml = """<?xml version="1.0"?>
<core:CityModel xmlns:core="http://www.opengis.net/citygml/2.0"
 xmlns:bldg="http://www.opengis.net/citygml/building/2.0"
 xmlns:gml="http://www.opengis.net/gml">
 <cityMember>
  <bldg:Building>
   <gml:name>Корпус А</gml:name>
   <bldg:lod1Solid><gml:Solid><gml:exterior><gml:CompositeSurface>
    <gml:posList>0 0 0 10 0 0 10 10 0 0 10 0</gml:posList>
   </gml:CompositeSurface></gml:exterior></gml:Solid></bldg:lod1Solid>
  </bldg:Building>
 </cityMember>
</core:CityModel>"""
        res = models3d.load_citygml_lod1(xml)
        self.assertEqual(res["count"], 1)
        b = res["buildings"][0]
        self.assertEqual(b["name"], "Корпус А")
        self.assertEqual(len(b["footprint"]), 4)

    def test_05_ifc_structure(self):
        step = """ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('ViewDefinition [CoordinationView]'),'2;1');
ENDSEC;
DATA;
#1=IFCPROJECT('0pr',#99,'Демо-проект');
#2=IFCBUILDING('0b1',#99,'Корпус А');
#3=IFCBUILDINGSTOREY('0s1',#99,'Этаж 1',$,3.5);
#4=IFCBUILDINGSTOREY('0s2',#99,'Этаж 2',$,7.0);
#5=IFCWALL('0w1',#99,'Стена 1');
#6=IFCSLAB('0sl1',#99,'Плита перекрытия');
#7=IFCSPACE('0sp1',#99,'Помещение 101');
ENDSEC;
END-ISO-10303-21;"""
        res = models3d.load_ifc_structure(step)
        self.assertEqual(res["format"], "IFC")
        st = res["structure"]
        self.assertEqual(st["buildings"][0]["name"], "Корпус А")
        self.assertEqual(len(st["storeys"]), 2)
        self.assertAlmostEqual(st["storeys"][1]["elevation"], 7.0)
        self.assertEqual(st["walls"], 1)
        self.assertEqual(st["spaces"][0]["name"], "Помещение 101")
        # G4: геометрия — не заглушка-строка, а честный отчёт парсера
        self.assertEqual(res["geometry"]["solids_built"], 0)   # в этом файле геометрии нет
        self.assertIn("не поддержаны", res["geometry"]["note"])


# ============ Часть 2: сквозные HTTP-тесты G3-эндпоинтов ============
PORT = 8497
BASE = f"http://127.0.0.1:{PORT}/api/v1"


def call(method, path, body=None, token=None, expect=None):
    data = json.dumps(body).encode() if body is not None else None
    path = urllib.parse.quote(path, safe="/?&=%")
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            code, res = r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        code, res = e.code, json.loads(e.read().decode() or "{}")
    if expect is not None:
        assert code == expect, f"{method} {path}: ожидался {expect}, получен {code}: {res}"
    return code, res


class TestG3E2E(unittest.TestCase):
    httpd = None

    @classmethod
    def setUpClass(cls):
        from afina.server.app import create_server
        cls.td = tempfile.TemporaryDirectory()
        db = os.path.join(cls.td.name, "g3.db")
        cls.httpd, cls.app = create_server(db_path=db, port=PORT)
        cls.th = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.th.start()
        time.sleep(0.3)
        call("POST", "/register", {"login": "admin", "password": "s3cr3t!", "role": "Администратор системы"}, expect=200)
        cls.at = call("POST", "/login", {"login": "admin", "password": "s3cr3t!"}, expect=200)[1]["token"]
        call("POST", "/register", {"login": "des1", "password": "p1", "role": "Проектировщик"}, token=cls.at, expect=200)
        call("POST", "/register", {"login": "appr1", "password": "p3", "role": "Уполномоченный утверждающий"}, token=cls.at, expect=200)
        cls.dt = call("POST", "/login", {"login": "des1", "password": "p1"}, expect=200)[1]["token"]
        cls.ap = call("POST", "/login", {"login": "appr1", "password": "p3"}, expect=200)[1]["token"]

    @classmethod
    def tearDownClass(cls):
        if cls.httpd:
            cls.httpd.shutdown(); cls.httpd.server_close()
        cls.td.cleanup()

    def test_01_twin_endpoint(self):
        call("POST", "/objects", {"id": "OBJ-G3", "class": "COM", "name": "Узел G3", "x": 10, "y": 20,
                                  "client_id": "g3-1"}, token=self.dt, expect=200)
        call("POST", "/telemetry", {"object_id": "OBJ-G3", "metric": "rssi_dbm", "value": -68.0}, token=self.dt, expect=200)
        r = call("GET", "/twins/OBJ-G3", token=self.dt, expect=200)[1]
        self.assertIn("layers", r); self.assertIn("dt", r)
        self.assertTrue(r["dt"]["level"] in ("DT-1", "DT-2", "DT-3"))
        self.assertAlmostEqual(r["observed"]["observed"]["rssi_dbm"]["value"], -68.0)
        d = call("POST", "/twins/OBJ-G3/divergence", {"project": {"rssi_dbm": -60.0}}, token=self.dt, expect=200)[1]
        self.assertEqual(len(d["divergences"]), 1)

    def test_02_calibrate_predict(self):
        c = call("POST", "/twins/calibrate", {"points": [[1, 3], [2, 5], [3, 7], [4, 9]],
                                              "passport": {"a": 1.5}}, token=self.dt, expect=200)[1]
        self.assertEqual(c["status"], "identified")
        self.assertTrue(c["passport_unchanged"])
        p = call("POST", "/twins/predict", {"history": [[1, 3], [2, 5], [3, 7], [4, 9]], "horizon": 2},
                 token=self.dt, expect=200)[1]
        self.assertEqual(p["type"], "оценка (не факт)")
        short = call("POST", "/twins/calibrate", {"points": [[1, 2]]}, token=self.dt, expect=200)[1]
        self.assertEqual(short["status"], "insufficient_data")

    def test_03_change_flow_maker_checker(self):
        cr = call("POST", "/changes", {"title": "Поднять АФС на 5 м", "reason": "затенение", "target": "OBJ-G3"},
                  token=self.dt, expect=200)[1]
        cid = cr["id"]
        self.assertEqual(cr["status"], "Предложено")
        self.assertIn("impact", cr)
        r = call("POST", f"/changes/{cid}/advance", {}, token=self.dt, expect=200)[1]
        self.assertEqual(r["to"], "Внесено")
        self.assertTrue(r["version_new"].startswith("BL-CR-"))  # линия зафиксирована ДО изменения
        call("POST", f"/changes/{cid}/advance", {}, token=self.dt, expect=200)  # → Проверено
        # автор не утверждает своё изменение (SEC-005)
        code, res = call("POST", f"/changes/{cid}/advance", {}, token=self.dt)
        self.assertEqual(code, 403)
        self.assertIn("SEC-005", res["error"])
        r = call("POST", f"/changes/{cid}/advance", {}, token=self.ap, expect=200)[1]
        self.assertEqual(r["to"], "Утверждено")

    def test_04_rules_and_validate(self):
        rules = call("GET", "/rules", token=self.dt, expect=200)[1]
        self.assertTrue(all("version" in r for r in rules["rules"]))
        chk = call("POST", "/rules/check", {"ctx": {"szz_margin_m": 3, "szz_required_m": 10, "f1_block_pct": 10,
                                                    "poe_drop_pct": 1, "redundancy_ok": True}}, token=self.dt, expect=200)[1]
        self.assertEqual(chk["overall"], "FAIL")
        v = call("POST", "/validate", {"id": "X", "class": "COM", "name": "n", "attrs": {"power_w": 9e6}},
                 token=self.dt, expect=200)[1]
        self.assertTrue(v["publish_blocked"])

    def test_05_conflict_flow(self):
        c = call("POST", "/conflicts", {"object_id": "OBJ-G3", "field": "power_w",
                                        "values": [{"value": 50, "source_id": "S1"}, {"value": 44, "source_id": "S2"}]},
                 token=self.dt, expect=200)[1]
        self.assertEqual(c["status"], "КОНФЛИКТ")
        bad = call("POST", f"/conflicts/{c['id']}/resolve", {"winner": "S2", "basis": "?"}, token=self.dt, expect=200)[1]
        self.assertIn("error", bad)  # проектировщик не арбитр
        ok = call("POST", f"/conflicts/{c['id']}/resolve", {"winner": "S2", "basis": "протокол №14"},
                  token=self.ap, expect=200)[1]
        self.assertEqual(ok["status"], "РЕШЕНО")
        lst = call("GET", "/conflicts", token=self.dt, expect=200)[1]["conflicts"]
        self.assertEqual(len(lst[0]["values"]), 2)  # оба значения сохранены

    def test_06_archive_and_ntf(self):
        bl = call("POST", "/baselines", {"project": "P-1", "content": {"v": 1}}, token=self.ap, expect=200)[1]["id"]
        a = call("POST", f"/archive/{bl}", {}, token=self.ap, expect=200)[1]
        self.assertEqual(len(a["sha256"]), 64)
        self.assertTrue(os.path.exists(a["path"]))
        n1 = call("POST", "/ntf", {"kind": "warn", "title": "Дубли", "dedup_key": "k1"}, token=self.dt, expect=200)[1]
        n2 = call("POST", "/ntf", {"kind": "warn", "title": "Дубли", "dedup_key": "k1"}, token=self.dt, expect=200)[1]
        self.assertFalse(n1["deduplicated"]); self.assertTrue(n2["deduplicated"])
        esc = call("POST", "/escalation", {}, token=self.at, expect=200)[1]
        self.assertIn("escalated", esc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
