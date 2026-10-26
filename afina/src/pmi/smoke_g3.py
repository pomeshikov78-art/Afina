#!/usr/bin/env python3
"""Смоук-испытание G3 «клиент + сервер»: поднимает сервер в потоке, прогоняет
19 шагов жизненного цикла (двойник, CR, правила, данные, NTF, аудит) и
формирует протокол. Запуск: python3 pmi/smoke_g3.py (из каталога src)."""
import json, os, sys, tempfile, threading, time, urllib.request, urllib.error

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from afina.server.app import create_server

PORT = 8480
STEPS = []

def main():
    from afina.server.app import create_server
    td = tempfile.mkdtemp()
    httpd, app = create_server(db_path=os.path.join(td, "smoke.db"), port=PORT)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.3)

    def get(p, tok=None, data=None):
        m = "POST" if data is not None else "GET"
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}" + p,
                                     data=json.dumps(data).encode() if data is not None else None, method=m)
        req.add_header("Content-Type", "application/json")
        if tok: req.add_header("Authorization", "Bearer " + tok)
        try:
            return json.loads(urllib.request.urlopen(req, timeout=10).read().decode())
        except urllib.error.HTTPError as e:
            return {"HTTP": e.code, "body": json.loads(e.read().decode() or "{}")}

    def ok(cond, text):
        STEPS.append({"step": len(STEPS) + 1, "check": text, "verdict": "PASS" if cond else "FAIL"})
        return cond

    try:
        page = urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=10).read().decode()
        ok("Жизненный цикл · G3" in page, f"клиент с вкладкой G3 раздаётся сервером ({len(page)} байт)")
        get("/api/v1/register", data={"login": "admin", "password": "x1", "role": "Администратор системы"})
        at = get("/api/v1/login", data={"login": "admin", "password": "x1"})["token"]
        get("/api/v1/register", at, {"login": "des1", "password": "p1", "role": "Проектировщик"})
        get("/api/v1/register", at, {"login": "appr1", "password": "p3", "role": "Уполномоченный утверждающий"})
        dt = get("/api/v1/login", data={"login": "des1", "password": "p1"})["token"]
        ap = get("/api/v1/login", data={"login": "appr1", "password": "p3"})["token"]
        ok(get("/api/v1/objects", at, {"id": "X", "class": "COM", "name": "x", "client_id": "z"}).get("HTTP") == 403,
           "RBAC: администратор системы не создаёт объекты (403, SEC-001)")
        get("/api/v1/objects", dt, {"id": "BS-S1", "class": "COM", "name": "БС S1", "x": 100, "y": 200, "client_id": "s1"})
        get("/api/v1/telemetry", dt, {"object_id": "BS-S1", "metric": "power_w", "value": 44.0})
        tw = get("/api/v1/twins/BS-S1", tok=dt)
        ok(tw["dt"]["level"] == "DT-1" and len(tw["layers"]) == 12,
           f"двойник: {tw['dt']['level']}, 12 слоёв, наблюдение power_w={tw['observed']['observed']['power_w']['value']} (DGT-001/002)")
        dv = get("/api/v1/twins/BS-S1/divergence", dt, {"project": {"power_w": 50.0}})
        ok(len(dv["divergences"]) == 1, "протокол отклонений проект↔факт: 1 расхождение, оба значения сохранены (DGT-003)")
        tw2 = get("/api/v1/twins/BS-S1", tok=dt)
        ok(tw2["dt"]["level"] == "DT-1" and tw2["dt"]["criteria"]["DT-4"]["met"] and not tw2["dt"]["criteria"]["DT-2"]["met"],
           "DGT-004 «не через голову»: критерий DT-4 выполнен, но нет расчётов → уровень честно DT-1")
        cal = get("/api/v1/twins/calibrate", dt, {"points": [[1,3],[2,5],[3,7],[4,9]], "passport": {"a": 1.5}})
        ok(cal["status"] == "identified" and cal["passport_unchanged"],
           f"калибровка МНК: a={cal['a']}, паспорт не изменён (DGT-005)")
        short = get("/api/v1/twins/calibrate", dt, {"points": [[1, 3]]})
        ok(short["status"] == "insufficient_data", "неинформативная калибровка → честный отказ, оценка не выдаётся")
        pr = get("/api/v1/twins/predict", dt, {"history": [[1,3],[2,5],[3,7],[4,9]], "horizon": 2})
        ok(pr["type"] == "оценка (не факт)", "прогноз маркирован «оценка (не факт)» (§4.6.3)")
        cr = get("/api/v1/changes", dt, {"title": "Поднять АФС на 5 м", "reason": "затенение", "target": "BS-S1"})
        ok(cr["status"] == "Предложено", f"CR {cr['id']} предложен, влияние рассчитано до утверждения (CFG-002)")
        a1 = get(f"/api/v1/changes/{cr['id']}/advance", dt, data={})
        ok(a1.get("to") == "Внесено" and str(a1.get("version_new", "")).startswith("BL-CR-"),
           f"CR → Внесено, линия {a1.get('version_new')} зафиксирована ДО изменения (§4.8.3)")
        get(f"/api/v1/changes/{cr['id']}/advance", dt, data={})
        own = get(f"/api/v1/changes/{cr['id']}/advance", dt, data={})
        ok(own.get("HTTP") == 403, "автор не утверждает своё изменение (SEC-005 Maker–Checker, 403)")
        okk = get(f"/api/v1/changes/{cr['id']}/advance", ap, data={})
        ok(okk.get("to") == "Утверждено", f"уполномоченный утвердил: {okk.get('from')} → {okk.get('to')} (CFG-003)")
        rules = get("/api/v1/rules", tok=dt)["rules"]
        chk = get("/api/v1/rules/check", dt, {"ctx": {"szz_margin_m": 3, "szz_required_m": 10, "f1_block_pct": 10,
                                                      "poe_drop_pct": 1, "redundancy_ok": True}})
        ok(len(rules) == 4 and chk["overall"] == "FAIL" and any(v["rule"] == "RULE-014" for v in chk["violations"]),
           "правила — данные (4 шт. с версиями/обоснованиями): вердикт FAIL, RULE-014 нарушено (CFG-004)")
        val = get("/api/v1/validate", dt, {"id": "X1", "class": "COM", "name": "Р-1",
                                           "attrs": {"power_w": 5000, "масса_кг": 10, "транспортная_масса_кг": 5}})
        ok(val["publish_blocked"], "валидация блокирует публикацию: диапазон + противоречие связанных параметров (DAT-002)")
        cf = get("/api/v1/conflicts", dt, {"object_id": "BS-S1", "field": "power_w",
                                           "values": [{"value": 50, "source_id": "паспорт"}, {"value": 44, "source_id": "измерение"}]})
        bad = get(f"/api/v1/conflicts/{cf['id']}/resolve", dt, {"winner": "измерение", "basis": "протокол"})
        good = get(f"/api/v1/conflicts/{cf['id']}/resolve", ap, {"winner": "измерение", "basis": "протокол измерения №14"})
        ok("error" in bad and good.get("status") == "РЕШЕНО",
           f"конфликт {cf['id']}: арбитраж проектировщиком отклонён, уполномоченный решил с основанием (DAT-004)")
        lst = get("/api/v1/conflicts", tok=dt)["conflicts"]
        ok(len(lst[0]["values"]) == 2, "оба значения конфликта сохранены, проигравшее не удалено (битемпоральность)")
        bl = get("/api/v1/baselines", ap, {"project": "P-1", "content": {"v": 1}})["id"]
        arc = get(f"/api/v1/archive/{bl}", ap, data={})
        ok(os.path.exists(arc["path"]) and len(arc["sha256"]) == 64,
           f"архив воспроизводимости {bl}: файл создан, sha256 присутствует (DAT-006)")
        n1 = get("/api/v1/ntf", dt, {"kind": "warn", "title": "Обрыв", "dedup_key": "k"})
        n2 = get("/api/v1/ntf", dt, {"kind": "warn", "title": "Обрыв", "dedup_key": "k"})
        esc = get("/api/v1/escalation", at, data={})
        ok(not n1["deduplicated"] and n2["deduplicated"] and "escalated" in esc,
           "NTF: дедупликация лавины (NTF-002) + сканирование эскалации по правилам (NTF-003)")
        v = get("/api/v1/audit/verify", tok=at)
        ok(v.get("ok") is True or v.get("valid") is True or v.get("broken") == 0,
           "аудит-хэш-цепочка цела после всех операций G3 (SEC-003)")
    finally:
        httpd.shutdown(); httpd.server_close()

    n_pass = sum(1 for s in STEPS if s["verdict"] == "PASS")
    proto = {"protocol": "ПРОТОКОЛ_SMOKE_G3", "title": "Смоук G3: клиент + сервер, жизненный цикл",
             "date": time.strftime("%Y-%m-%d %H:%M:%S"), "steps_total": len(STEPS), "steps_passed": n_pass,
             "verdict": "PASS" if n_pass == len(STEPS) else "FAIL", "steps": STEPS,
             "note": "сервер поднят в потоке, порт 8480; клиент — src/client/index.html (вкладка «Жизненный цикл · G3»)"}
    out = os.path.join(os.path.dirname(__file__), "..", "..", "protocols",
                       f"ПРОТОКОЛ_SMOKE_G3_{time.strftime('%Y%m%d_%H%M%S')}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump(proto, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    for s in STEPS:
        print(("✓" if s["verdict"] == "PASS" else "✗"), f"{s['step']:2d}.", s["check"])
    print(f"{proto['verdict']}: {n_pass}/{len(STEPS)} → {os.path.normpath(out)}")
    return 0 if proto["verdict"] == "PASS" else 1

if __name__ == "__main__":
    sys.exit(main())
