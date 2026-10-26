#!/usr/bin/env python3
"""Смоук-испытание G4 «Расширение»: поднимает сервер в потоке, прогоняет сквозной
сценарий (сценарии DT-5, объяснения ИИ и AI-003, OPT-01/LCC-01, Co-Simulation,
суррогаты MOD-006, ВТ-01…08, геймификация и миссии, аналитика руководителя)
и формирует протокол. Запуск: python3 pmi/smoke_g4.py (из каталога src)."""
import json, os, sys, tempfile, threading, time, urllib.request, urllib.error, urllib.parse

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

PORT = 8481
STEPS = []


def main():
    from afina.server.app import create_server
    from afina.catalog import seed
    td = tempfile.mkdtemp()
    httpd, app = create_server(db_path=os.path.join(td, "smoke4.db"), port=PORT)
    seed.build(app.store)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.3)

    def get(p, tok=None, data=None):
        m = "POST" if data is not None else "GET"
        p = urllib.parse.quote(p, safe="/?&=%")
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}" + p,
                                     data=json.dumps(data).encode() if data is not None else None, method=m)
        req.add_header("Content-Type", "application/json")
        if tok: req.add_header("Authorization", "Bearer " + tok)
        try:
            return json.loads(urllib.request.urlopen(req, timeout=15).read().decode())
        except urllib.error.HTTPError as e:
            return {"HTTP": e.code, "body": json.loads(e.read().decode() or "{}")}

    def wait(jid, tok):
        for _ in range(120):
            st = get(f"/api/v1/calc/{jid}", tok=tok)
            if st.get("status") in ("done", "error"):
                return st
            time.sleep(0.2)
        return {"status": "timeout"}

    def ok(cond, text):
        STEPS.append({"step": len(STEPS) + 1, "check": text, "verdict": "PASS" if cond else "FAIL"})
        return cond

    try:
        page = urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=10).read().decode()
        ok("Расширение · G4" in page, f"клиент с вкладкой G4 раздаётся сервером ({len(page)} байт)")

        get("/api/v1/register", data={"login": "admin", "password": "x1", "role": "Администратор системы"})
        at = get("/api/v1/login", data={"login": "admin", "password": "x1"})["token"]
        for lg, pw, rl in (("des1", "p1", "Проектировщик"), ("eng1", "p2", "Инженер-расчётчик"),
                           ("ver1", "p3", "Инженер-верификатор"), ("appr1", "p4", "Уполномоченный утверждающий"),
                           ("pm1", "p5", "Руководитель программы"), ("dadm1", "p6", "Администратор данных")):
            get("/api/v1/register", at, {"login": lg, "password": pw, "role": rl})
        dt = get("/api/v1/login", data={"login": "des1", "password": "p1"})["token"]
        et = get("/api/v1/login", data={"login": "eng1", "password": "p2"})["token"]
        vr = get("/api/v1/login", data={"login": "ver1", "password": "p3"})["token"]
        ap = get("/api/v1/login", data={"login": "appr1", "password": "p4"})["token"]
        pm = get("/api/v1/login", data={"login": "pm1", "password": "p5"})["token"]
        dm = get("/api/v1/login", data={"login": "dadm1", "password": "p6"})["token"]
        ok(True, "роли зарегистрированы по матрице Приложения Н (6 участников сценария)")

        # ---- данные: объекты и телеметрия ----
        for i, (oid, x, y) in enumerate([("NODE-1", 500, 600), ("NODE-2", 2500, 1800), ("REP-1", 1400, 900)]):
            get("/api/v1/objects", dt, {"id": oid, "class": "COM", "name": oid, "x": x, "y": y, "client_id": f"g4-{i}"})
        for k, v in enumerate([40, 42, 44, 46]):
            get("/api/v1/telemetry", dt, {"object_id": "NODE-1", "metric": "load_pct", "value": v, "ts": time.time() + k})
        j = get("/api/v1/calc", et, {"function": "RF-03", "model": "OKUMURA_HATA", "width_m": 4000, "height_m": 3000,
                                     "cell_m": 150, "threshold_dbm": -95, "env": "urban",
                                     "stations": [{"id": "NODE-1", "x_m": 500, "y_m": 600, "h_ant_m": 30,
                                                   "f_mhz": 900, "tx_power_dbm": 43, "ant_gain_dbi": 17, "sector": False}]})["job_id"]
        st = wait(j, et)
        rf_run = (st.get("result") or {}).get("passport", {}).get("run_id")
        ok(st.get("status") == "done" and rf_run, f"регрессия G2: RF-03 через очередь выполнен, паспорт {rf_run}")

        get("/api/v1/twins/NODE-1/divergence", dt, {"project": {"load_pct": 38.0}})
        sc = get("/api/v1/twins/NODE-1/scenario", et, {"kind": "param", "params": {"metric": "load_pct", "delta_pct": 20, "limit": 70}})
        ok(sc.get("status") == "done" and sc.get("marked") == "оценка (не факт)",
           f"сценарий what-if {sc.get('id')}: projected={sc.get('result',{}).get('param_scenario',{}).get('projected')}, маркирован «оценка» (DT-5)")
        lev = get("/api/v1/twins/NODE-1", tok=dt)["dt"]["level"]
        ok(lev == "DT-4", f"до независимой проверки сценария уровень честно {lev} (DT-5 не выдаётся авансом)")
        get(f"/api/v1/scenarios/{sc['id']}/validate", vr, {"basis": "сверка с ручным расчётом 46·1.2=55.2"})
        lev2 = get("/api/v1/twins/NODE-1", tok=dt)["dt"]["level"]
        ok(lev2 == "DT-5", f"после независимой проверки (роль verify) уровень {lev2} — критерий выполнен, не «через голову»")
        bad = get("/api/v1/twins/NODE-1/scenario", et, {"kind": "магия", "params": {}})
        ok(bad.get("status") == "error", "незарегистрированный вид сценария отклонён (тихая подмена запрещена)")

        # ---- ИИ: объяснения, предложения, отключаемость ----
        ex = get("/api/v1/ai/explain", et, {"run_id": rf_run})
        ok(ex.get("status") == "ok" and ex.get("sources") and ex.get("limitations") and ex.get("confirmation_status"),
           "ИИ-объяснение RF-03: источники/версии/допущения/ограничения/статус подтверждения (AI-001)")
        sg = get("/api/v1/ai/suggest", et, {"object_id": "NODE-1"})
        ok(sg.get("status") == "ok" and sg.get("suggestions"), f"ИИ предложил сценариев: {len(sg.get('suggestions', []))} — по фактическим данным")
        app.ai_enabled = False
        d1 = get("/api/v1/ai/ask", et, {"q": "RF-03?"})
        d2 = get("/api/v1/ai/explain", et, {"run_id": rf_run})
        twin_ok = "dt" in get("/api/v1/twins/NODE-1", tok=dt)
        app.ai_enabled = True
        ok(d1.get("status") == "disabled" and d2.get("status") == "disabled" and twin_ok,
           "AI-003: при отключённом ИИ ответы честно «disabled», двойник и ГИС работают штатно")

        # ---- OPT-01 / LCC-01 ----
        jo = get("/api/v1/calc", et, {"function": "OPT-01",
                                      "variants": [{"id": "В1", "cost": 100, "cover": 80, "vendor": "РФ"},
                                                   {"id": "В2", "cost": 30, "cover": 99, "vendor": "неизвестен"},
                                                   {"id": "В3", "cost": 130, "cover": 74, "vendor": "РФ"}],
                                      "criteria": [{"key": "cost", "name": "Стоимость", "direction": "min", "weight": 2, "hard_max": 150},
                                                   {"key": "cover", "name": "Покрытие", "direction": "max", "weight": 1, "hard_min": 60}],
                                      "constraints": [{"type": "subset_of", "field": "vendor", "values": ["РФ"]}]})["job_id"]
        ro = wait(jo, et).get("result") or {}
        ok(ro.get("feasible_count") == 2 and ro["rejected"][0]["variant"]["id"] == "В2"
           and "В2" not in ro.get("pareto_front", []) and ro["best"]["id"] in ("В1", "В3"),
           "OPT-01 (ENG-006): «дешёвый и лучший по покрытию» В2 отклонён ограничением и НЕ попал ни в Парето, ни в ранжирование")
        jl = get("/api/v1/calc", et, {"function": "LCC-01", "capex": 1000000, "years": 10, "opex_per_year": 120000, "discount": 0.08})["job_id"]
        rl = wait(jl, et).get("result") or {}
        ok("tco" in rl and "tco_sensitivity" in rl, f"LCC-01: TCO={rl.get('tco')} с чувствительностью ±20 % OPEX")

        # ---- Co-Simulation ----
        jc = get("/api/v1/calc", et, {"function": "COSIM-01", "scenario_id": "CS-SMOKE-1",
                                      "seed": {"w_nom": 2500, "w_peak": 4200, "t_ambient_c": 31, "t_limit_c": 40,
                                               "cooling_w": 3000, "mtbf_h": 9000, "mttr_h": 5}})["job_id"]
        rc = wait(jc, et).get("result") or {}
        ok(rc.get("ok") and rc.get("scenario_id") == "CS-SMOKE-1" and rc.get("versions", {}).get("PWR-01") == "1.0.0"
           and isinstance(rc.get("unit_checks"), str),
           f"COSIM-01 (ENG-005): мощность→тепло→доступность = {rc.get('data', {}).get('availability')}, версии решателей зафиксированы")
        jc2 = get("/api/v1/calc", et, {"function": "COSIM-01", "seed": {"w_nom": 1000}})["job_id"]
        rc2 = wait(jc2, et).get("result") or {}
        ok(rc2.get("ok") is False and "не хватает входов" in json.dumps(rc2.get("log", []), ensure_ascii=False),
           "неполный seed: ошибка решателя НЕ подменяется значениями по умолчанию (§4.5.6)")

        # ---- суррогаты ----
        tr = get("/api/v1/surrogate/demo_train", et, {"model_key": "OKUMURA_HATA"})
        ok(tr.get("status") == "trained" and tr.get("n_points") == 225 and tr.get("rmse", 99) < 6.0,
           f"суррогат обучен НА РЕШАТЕЛЕ RF-01: {tr.get('n_points')} точек, RMSE {tr.get('rmse')} дБ, маркирован")
        sid = tr.get("id")
        pv = get("/api/v1/surrogate/predict", et, {"id": sid, "x": [900, 6, 70, 2]})
        od = get("/api/v1/surrogate/predict", et, {"id": sid, "x": [3000, 6, 70, 2]})
        ok(pv.get("status") == "ok" and "СУРРОГАТ" in pv.get("marked", "") and od.get("status") == "out_of_domain",
           f"предсказание {pv.get('value')} дБ маркировано; выход за домен → out_of_domain, значение не выдаётся как достоверное (MOD-006)")
        v0 = get(f"/api/v1/surrogates/{sid}/validate", vr, {"basis": "протокол"})
        cp = get(f"/api/v1/surrogates/{sid}/compare", et, {"test_points": [[600, 4, 60, 2], [1100, 9, 100, 3]]})
        v1 = get(f"/api/v1/surrogates/{sid}/validate", vr, {"basis": f"RMSE(незав.)={cp.get('rmse_independent')} ≤ 10 дБ"})
        ok(v0.get("HTTP") == 400 and v1.get("status") == "Валидирована для указанной области",
           "валидация суррогата БЕЗ сравнения с решателем отклонена; после сравнения — статус с фиксированной областью")
        get(f"/api/v1/surrogates/{sid}/disable", dm, {"disabled": True})
        pd = get("/api/v1/surrogate/predict", et, {"id": sid, "x": [900, 6, 70, 2]})
        ok(pd.get("status") == "disabled", "отключение суррогата ролью «Администратор данных» — предсказание честно отказывает (§4.5.8)")

        # ---- ВТ-профиль ----
        get("/api/v1/vt/participants", dm, {"id": "ORG-1", "org": "НПО «Связь»", "role": "головной", "zone": "РЭС"})
        get("/api/v1/vt/stages", pm, {"id": "Э-1", "contract": "К-2026", "title": "Эскизный проект"})
        c0 = get("/api/v1/vt/stages/Э-1/commission", ap, {"basis": "протокол"})
        for dim in ("изделие", "документация", "испытания", "комплект"):
            get("/api/v1/vt/stages/Э-1/mark", et, {"dimension": dim, "value": True, "evidence": f"акт-{dim}"})
        c1 = get("/api/v1/vt/stages/Э-1/commission", et, {"basis": "протокол"})
        c2 = get("/api/v1/vt/stages/Э-1/commission", ap, {"basis": "протокол заседания комиссии №7"})
        ok(c0.get("HTTP") == 409 and c1.get("HTTP") == 403 and c2.get("status") == "closed",
           "ВТ-02: комиссия отказала при неготовых материалах (409), чужая роль отклонена (403), закрытие — решением уполномоченного")
        get("/api/v1/vt/benches", dm, {"id": "СТ-П", "name": "Анализатор", "kind": "СИ", "passport": "ПС-9", "calib_due": time.time() - 10})
        reg = get("/api/v1/vt/tests", et, {"id": "И-П", "object_id": "NODE-1", "bench_id": "СТ-П", "basis": "RF-03", "results": {"cov": 91}})
        apr = get("/api/v1/vt/tests/И-П/approve", vr, {"basis": "протокол"})
        get("/api/v1/vt/benches", dm, {"id": "СТ-Д", "name": "Анализатор-2", "kind": "СИ", "passport": "ПС-10", "calib_due": time.time() + 30 * 86400})
        get("/api/v1/vt/tests", et, {"id": "И-Д", "object_id": "NODE-1", "bench_id": "СТ-Д", "basis": "RF-03"})
        apr2 = get("/api/v1/vt/tests/И-Д/approve", vr, {"basis": "протокол сверки"})
        pp = get("/api/v1/vt/tests/И-Д/passport", tok=et)
        ok("недопустимы" in reg.get("metrology_warning", "") and apr.get("HTTP") == 409 and apr2.get("status") == "approved"
           and pp.get("equipment", {}).get("metrology") == "действительна",
           "ВТ-03: просроченная поверка СИ → предупреждение и блокировка согласования (§4.8.4); паспорт испытания собран (§4.8.5)")
        rd = get("/api/v1/vt/readiness", tok=pm)
        ag = get("/api/v1/vt/aging", tok=et)
        un = get("/api/v1/vt/unification", tok=et)
        ok(0 <= rd.get("formal_level", -1) <= 4 and len(rd.get("gates", [])) == 4 and "оценка" in rd.get("note", "").lower()
           and ag.get("risks") and all("reactions" in r for r in ag["risks"]) and "reuse_rate_pct" in un,
           f"ВТ-05: формальная готовность {rd.get('formal_level')}/4 (оценка, не комиссия) · ВТ-07: рисков {len(ag.get('risks', []))}, у каждого варианты реакции · ВТ-04: дубли/заимствование/EOL")

        # ---- геймификация и обучение ----
        gj = get("/api/v1/game/award", et, {"event": "objects_count"})
        get("/api/v1/zam", dt, {"id": "ЗАМ-S4", "title": "дефект", "crit": "Минорный"})
        get("/api/v1/zam/ЗАМ-S4/advance", dt, {"to": "Закрыто", "basis": "устранено, повторная проверка ver1"})
        g1 = get("/api/v1/game/award", et, {"event": "zam_closed_verified", "ref": "ЗАМ-S4"})
        g2 = get("/api/v1/game/award", et, {"event": "zam_closed_verified", "ref": "ЗАМ-S4"})
        g3 = get("/api/v1/game/revoke", et, {"award_id": 1, "reason": "ошибка"})
        g4 = get("/api/v1/game/revoke", vr, {"award_id": 1, "reason": "закрытие без повторной проверки верификатором"})
        sc0 = get("/api/v1/game/scores", tok=et)
        ok(gj.get("status") == "rejected" and len(gj.get("forbidden_grounds", [])) == 6
           and g1.get("status") == "awarded" and g2.get("status") == "duplicate"
           and g3.get("HTTP") == 403 and g4.get("status") == "revoked" and sum(x["total"] for x in sc0["leaderboard"]) == 0,
           "§4.11.3: «количество объектов» отклонено (6 запрещённых оснований), балл выдан за проверяемый результат, дубль заблокирован, отзыв — только verify и с объяснением, суммы пересчитаны")
        ms = get("/api/v1/game/missions/собери_узел/start", et, {"seed": 7})
        ck = get("/api/v1/game/missions/собери_узел/check", et, {"seed": 7, "answer": "БП-999"})
        h1 = get("/api/v1/game/missions/собери_узел/hint", et, {"seed": 7, "level": 1})
        h2 = get("/api/v1/game/missions/собери_узел/hint", et, {"seed": 7, "level": 2})
        ok("answer" not in ms and ck.get("correct") is False and ck.get("why") and h1.get("hint") != h2.get("hint"),
           "миссия «Собери узел»: старт не выдаёт ответ, проверка объясняет ПОЧЕМУ, подсказки 3 уровней различаются (§4.11.6)")
        pg = get("/api/v1/game/polygon", tok=et)
        ix = get("/api/v1/game/indices?object_id=NODE-1", tok=et)
        ok(len(pg.get("stages", [])) == 8 and "ИНЖЕНЕРНЫМИ" in pg.get("rule", "")
           and all(k in ix for k in ("полнота_данных", "достоверность", "зрелость_модели", "совместимость", "эксплуатационная_готовность")),
           f"полигон: 8 этапов, переход по инженерным критериям (текущий {pg.get('current_stage')}); индексы готовности ЦД разделены (§4.11.8)")

        # ---- аналитика и сквозная целостность ----
        tr2 = get("/api/v1/analytics/trends?days=7", tok=pm)
        pn = get("/api/v1/vt/panel", tok=pm)
        ok(tr2.get("totals", {}).get("объекты", 0) >= 3 and tr2.get("totals", {}).get("расчёты", 0) >= 4
           and all(k in pn for k in ("разработка", "документация", "испытания", "конфигурация", "производство", "надёжность", "стоимость_сроки", "ИБ")),
           f"G4.6: тренды из первичных реестров (объекты {tr2['totals']['объекты']}, расчёты {tr2['totals']['расчёты']}); панель ВТ-08 — 8 разделов")
        v = get("/api/v1/audit/verify", tok=at)
        hz = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/healthz", timeout=5).read().decode())
        ok((v.get("ok") is True) and hz.get("external_calls") == 0,
           f"аудит-хэш-цепочка цела после всего прогона G4 (записей {v.get('entries')}); внешних обращений 0 (AUT-002)")
    finally:
        httpd.shutdown(); httpd.server_close()

    n_pass = sum(1 for s in STEPS if s["verdict"] == "PASS")
    proto = {"protocol": "ПРОТОКОЛ_SMOKE_G4", "title": "Смоук G4: сценарии DT-5, ИИ, OPT/CoSim/суррогаты, ВТ-профиль, геймификация, аналитика",
             "date": time.strftime("%Y-%m-%d %H:%M:%S"), "steps_total": len(STEPS), "steps_passed": n_pass,
             "verdict": "PASS" if n_pass == len(STEPS) else "FAIL", "steps": STEPS,
             "note": "сервер поднят в потоке, порт 8481; клиент — src/client/index.html (вкладка «Расширение · G4»); каталог п.14 загружен сидом"}
    out = os.path.join(os.path.dirname(__file__), "..", "..", "protocols",
                       f"ПРОТОКОЛ_SMOKE_G4_{time.strftime('%Y%m%d_%H%M%S')}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump(proto, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    for s in STEPS:
        print(("✓" if s["verdict"] == "PASS" else "✗"), f"{s['step']:2d}.", s["check"])
    print(f"{proto['verdict']}: {n_pass}/{len(STEPS)} → {os.path.normpath(out)}")
    return 0 if proto["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
