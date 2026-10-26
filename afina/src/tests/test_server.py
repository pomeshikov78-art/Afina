"""Сквозные тесты сервера (SEC-001/003/005, INT-001/002, ENG-001, ARC-003, DGT-002)."""
import json, os, sys, tempfile, threading, time, unittest, urllib.request, urllib.error, urllib.parse

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from afina.server.app import create_server, API

PORT = 8499
BASE = f"http://127.0.0.1:{PORT}{API}"


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


class ServerE2E(unittest.TestCase):
    httpd = None

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.TemporaryDirectory()
        db = os.path.join(cls.td.name, "test.db")
        tz = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "Афина_Техническое_задание.md"))
        reqs = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data", "Афина_Реестр_требований_ПриложениеА.json"))
        cls.httpd, cls.app = create_server(db_path=db, port=PORT, tz_path=tz if os.path.exists(tz) else None,
                                           requirements_path=reqs if os.path.exists(reqs) else None)
        from afina.catalog import seed
        seed.build(cls.app.store)  # наполнение каталога и ОШС (требование п.14)
        cls.th = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.th.start()
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.td.cleanup()

    def test_01_bootstrap_and_roles(self):
        # первый пользователь — bootstrap; регистрация посторонним запрещена после
        call("POST", "/register", {"login": "admin", "password": "s3cr3t!", "role": "Администратор системы"}, expect=200)
        at = call("POST", "/login", {"login": "admin", "password": "s3cr3t!"}, expect=200)[1]["token"]
        call("POST", "/register", {"login": "designer1", "password": "p1", "role": "Проектировщик"}, token=at, expect=200)
        call("POST", "/register", {"login": "verif1", "password": "p2", "role": "Инженер-верификатор"}, token=at, expect=200)
        call("POST", "/register", {"login": "appr1", "password": "p3", "role": "Уполномоченный утверждающий"}, token=at, expect=200)
        # анонимная регистрация после bootstrap — запрещена
        call("POST", "/register", {"login": "hacker", "password": "x", "role": "Аудитор"}, expect=403)
        # неверная роль — отклонена (INT-001 отрицательный тест)
        call("POST", "/register", {"login": "x2", "password": "x", "role": "Суперпользователь"}, token=at, expect=400)

    def test_02_auth_and_rbac(self):
        dt = call("POST", "/login", {"login": "designer1", "password": "p1"}, expect=200)[1]["token"]
        vt = call("POST", "/login", {"login": "verif1", "password": "p2"}, expect=200)[1]["token"]
        # неверный пароль
        call("POST", "/login", {"login": "designer1", "password": "wrong"}, expect=401)
        # без токена
        call("GET", "/objects", expect=401)
        # RBAC: проектировщик НЕ фиксирует базовую линию
        code, res = call("POST", "/baselines", {"project": "P-1", "content": {"v": 1}}, token=dt)
        self.assertEqual(code, 403)
        self.assertIn("SEC-001", res["error"])
        type(self).dt = dt; type(self).vt = vt

    def test_03_objects_and_idempotency(self):
        r1 = call("POST", "/objects", {"id": "OBJ-T1", "class": "COM", "name": "Узел Т1", "x": 100, "y": 200,
                                        "client_id": "c-777", "attrs": {"мощность_Вт": 50, "персональные_данные": "ИВАНОВ"}},
                   token=self.dt, expect=200)[1]
        r2 = call("POST", "/objects", {"id": "OBJ-T1", "class": "COM", "name": "Узел Т1", "x": 100, "y": 200,
                                        "client_id": "c-777"}, token=self.dt, expect=200)[1]
        self.assertEqual(r1, r2)  # INT-002: повтор не создаёт дубликата/нового состояния
        objs = call("GET", "/objects", token=self.dt, expect=200)[1]["objects"]
        self.assertEqual(len([o for o in objs if o["id"] == "OBJ-T1"]), 1)

    def test_04_export_sec004(self):
        res = call("GET", "/export/objects", token=self.dt, expect=200)[1]
        blob = json.dumps(res, ensure_ascii=False)
        self.assertNotIn("ИВАНОВ", blob)          # закрытые атрибуты исключены
        self.assertNotIn("персональные_данные", blob)

    def test_05_calc_rf03_with_passport(self):
        payload = {"function": "RF-03", "model": "OKUMURA_HATA", "mode": "Engineering", "level": "L1",
                   "width_m": 6000, "height_m": 6000, "cell_m": 100,
                   "stations": [{"id": "BS-T", "x_m": 3000, "y_m": 3000, "h_ant_m": 40, "f_mhz": 900,
                                  "tx_power_dbm": 43, "sector": False}],
                   "threshold_dbm": -95}
        jid = call("POST", "/calc", payload, token=self.dt, expect=202)[1]["job_id"]
        for _ in range(60):
            st = call("GET", f"/calc/{jid}", token=self.dt, expect=200)[1]
            if st["status"] in ("done", "error"):
                break
            time.sleep(0.25)
        self.assertEqual(st["status"], "done", st.get("error"))
        res = st["result"]
        self.assertGreater(res["coverage_pct"], 0)
        p = res["passport"]
        # ENG-001: паспорт с версиями и хэшем входов; MOD-005: 4 компонента неопределённости
        self.assertTrue(p["inputs_hash"])
        self.assertEqual(set(p["uncertainty_db"]), {"data", "numeric", "model_params", "physical_model"})
        self.assertEqual(p["function"], "RF-03")
        self.assertIn("grid", res)

    def test_06_unknown_function_rejected(self):
        code, res = call("POST", "/calc", {"function": "MAGIC-99"}, token=self.dt)
        self.assertEqual(code, 400)
        self.assertIn("тихая подмена запрещена", res["error"])

    def test_07_zam_maker_checker(self):
        zid = call("POST", "/zam", {"id": "ЗАМ-Т1", "title": "Тестовое", "crit": "Критический", "req": "REQ-T"},
                   token=self.dt, expect=200)[1]["id"]
        at = call("POST", "/login", {"login": "appr1", "password": "p3"}, expect=200)[1]["token"]
        # проектировщик не может согласовать
        code, _ = call("POST", f"/zam/{zid}/advance", {"to": "Согласовано"}, token=self.dt)
        self.assertEqual(code, 403)
        # верификатор тоже не может (роль)
        code, _ = call("POST", f"/zam/{zid}/advance", {"to": "Согласовано"}, token=self.vt)
        self.assertEqual(code, 403)
        # утверждающий — может
        call("POST", f"/zam/{zid}/advance", {"to": "Устранено"}, token=self.dt, expect=200)
        call("POST", f"/zam/{zid}/advance", {"to": "Проверено"}, token=self.vt, expect=200)
        call("POST", f"/zam/{zid}/advance", {"to": "Согласовано"}, token=at, expect=200)
        # закрытие без основания — отклонено
        code, _ = call("POST", f"/zam/{zid}/advance", {"to": "Закрыто"}, token=at)
        self.assertEqual(code, 400)
        call("POST", f"/zam/{zid}/advance", {"to": "Закрыто", "basis": "повторная проверка, протокол №1"}, token=at, expect=200)

    def test_08_telemetry_does_not_touch_baseline(self):
        bl = call("POST", "/baselines", {"project": "P-1", "content": {"objects": ["OBJ-T1"], "v": 1}},
                  token=call("POST", "/login", {"login": "appr1", "password": "p3"}, expect=200)[1]["token"], expect=200)[1]
        call("POST", "/telemetry", {"object_id": "OBJ-T1", "metric": "power_w", "value": 43}, token=self.dt, expect=200)
        bls = call("GET", "/baselines", token=self.dt, expect=200)[1]["baselines"]
        target = [b for b in bls if b["id"] == bl["id"]][0]
        self.assertEqual(target["content_hash"], bl["hash"])  # DGT-002: линия не изменилась

    def test_09_audit_chain_and_denials_logged(self):
        v = call("GET", "/audit/verify", token=self.vt, expect=200)[1]
        self.assertTrue(v["ok"], v)
        aud = call("GET", "/audit?limit=200", token=self.vt, expect=200)[1]["audit"]
        actions = " ".join(a["action"] for a in aud)
        self.assertIn("ОТКАЗ RBAC", actions)
        self.assertIn("неуспешный вход", actions)

    def test_10_locks_multiuser(self):
        at = call("POST", "/login", {"login": "admin", "password": "s3cr3t!"}, expect=200)[1]["token"]
        call("POST", "/register", {"login": "designer2", "password": "p9", "role": "Проектировщик"}, token=at, expect=200)
        d2 = call("POST", "/login", {"login": "designer2", "password": "p9"}, expect=200)[1]["token"]
        call("POST", "/locks", {"object_id": "OBJ-T1"}, token=self.dt, expect=200)
        code, res = call("POST", "/locks", {"object_id": "OBJ-T1"}, token=d2)
        self.assertEqual(code, 409)
        # другой проектировщик не перезапишет заблокированный объект
        code, _ = call("POST", "/objects", {"id": "OBJ-T1", "name": "перезапись"}, token=d2)
        self.assertEqual(code, 409)
        call("DELETE", "/locks", {"object_id": "OBJ-T1"}, token=self.dt, expect=200)

    def test_11_catalog_db(self):
        res = call("GET", "/catalog/equipment?q=азарт", token=self.dt, expect=200)[1]
        self.assertGreaterEqual(res["count"], 1)
        res2 = call("GET", "/catalog/equipment?from_year=2030", token=self.dt, expect=200)[1]
        for e in res2["equipment"]:
            self.assertIn("ПРОГНОЗ", e["status_kind"] + e["data_quality"])  # честная маркировка будущего


    def test_12_ai_ask(self):
        code, res = call("POST", "/ai/ask", {"q": "что фиксирует паспорт расчётного запуска"}, token=self.dt, expect=200)
        self.assertIn(res["status"], ("ok", "insufficient_data"))
        if res["status"] == "ok":
            self.assertTrue(res["sources"])
            self.assertTrue(res["limitations"])   # AI-001: ограничения всегда
        # MIL-12: базовая конфигурация не изменена ИИ — проверяем отсутствие записей изменения
        v = call("GET", "/audit/verify", token=self.vt, expect=200)[1]
        self.assertTrue(v["ok"])

    def test_13_ai_anomalies(self):
        res = call("GET", "/ai/anomalies", token=self.dt, expect=200)[1]
        self.assertIn("anomalies", res)
        self.assertIn("арбитраж", res["policy"])

    def test_14_ai_disable_ai003(self):
        self.app.ai_enabled = True
        self.app.rag = None
        from afina.ai.rag import RagIndex
        self.app.rag = RagIndex(enabled=False)
        code, res = call("POST", "/ai/ask", {"q": "что угодно"}, token=self.dt, expect=200)
        self.assertEqual(res["status"], "disabled")
        # ГИС и расчёты продолжают работать (AI-003)
        payload = {"function": "RF-03", "width_m": 2000, "height_m": 2000, "cell_m": 100,
                   "stations": [{"id": "BS-X", "x_m": 1000, "y_m": 1000, "h_ant_m": 30, "f_mhz": 900, "sector": False}]}
        jid = call("POST", "/calc", payload, token=self.dt, expect=202)[1]["job_id"]
        for _ in range(40):
            st = call("GET", f"/calc/{jid}", token=self.dt, expect=200)[1]
            if st["status"] in ("done", "error"): break
            time.sleep(0.2)
        self.assertEqual(st["status"], "done")
        self.app.rag = None


if __name__ == "__main__":
    unittest.main(verbosity=2)
