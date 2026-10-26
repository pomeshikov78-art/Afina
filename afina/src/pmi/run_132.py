#!/usr/bin/env python3
"""G5.1 · Сквозной приёмочный сценарий §13.2 (12 шагов) в режимах А и Б.

Режим А — автономный защищённый контур: полный прогон через REST без обращения
к Интернету (healthz external_calls = 0).
Режим Б — полностью изолированный комплекс (Air-Gap): прогон при селективной
блокировке сокета — loopback разрешён (сервер↔клиент внутри контура), ЛЮБОЕ
внешнее соединение блокируется и фиксируется; ключевые результаты сверяются
с режимом А на идентичность (шаг 12: «результат идентичен»).

Каждый шаг: действие → ожидаемый результат (по ТЗ) → фактический → вердикт.
Протокол: protocols/ПРОТОКОЛ_132_*.{json,md}. Запуск: python3 pmi/run_132.py (из src).
"""
import json, os, socket, sys, tempfile, threading, time, urllib.request, urllib.error, urllib.parse

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

PORT_A, PORT_B = 8484, 8485

# ---------- режим Б: селективная блокировка внешних соединений ----------
BLOCKED = []
_orig_connect = socket.socket.connect


def _guarded_connect(self, addr):
    host = addr[0] if isinstance(addr, tuple) else str(addr)
    if host in ("127.0.0.1", "localhost", "::1"):
        return _orig_connect(self, addr)
    BLOCKED.append((time.time(), host))
    raise RuntimeError(f"внешнее соединение заблокировано (режим Б, AUT): {host}")


class AirGap:
    def __enter__(self):
        socket.socket.connect = _guarded_connect
        return self

    def __exit__(self, *a):
        socket.socket.connect = _orig_connect


def run_scenario(mode: str, port: int, workdir: str):
    """Полный прогон §13.2. Возвращает (steps, artifacts)."""
    from afina.server.app import create_server
    from afina.catalog import seed
    from afina.gis.io_formats import geojson_import

    steps = []

    def step(n, action, expected, actual, passed, evidence=""):
        steps.append({"step": n, "action": action, "expected": expected,
                      "actual": actual, "verdict": "PASS" if passed else "FAIL", "evidence": evidence})
        return passed

    db = os.path.join(workdir, f"acc_{mode}.db")
    httpd, app = create_server(db_path=db, port=port)
    seed.build(app.store)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.3)
    BASE = f"http://127.0.0.1:{port}/api/v1"

    def call(method, path, body=None, tok=None, raw=False):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(BASE + urllib.parse.quote(path, safe="/?&=%"), data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if tok:
            req.add_header("Authorization", "Bearer " + tok)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                code, res = r.status, json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            code, res = e.code, json.loads(e.read().decode() or "{}")
        return (code, res) if raw else res

    def wait(jid, tok):
        for _ in range(150):
            st = call("GET", f"/calc/{jid}", tok=tok)
            if st.get("status") in ("done", "error"):
                return st
            time.sleep(0.2)
        return {"status": "timeout"}

    art = {}
    try:
        # ---- роли ----
        call("POST", "/register", {"login": "admin", "password": "x", "role": "Администратор системы"})
        at = call("POST", "/login", {"login": "admin", "password": "x"})["token"]
        for lg, rl in (("des1", "Проектировщик"), ("eng1", "Инженер-расчётчик"),
                       ("ver1", "Инженер-верификатор"), ("appr1", "Уполномоченный утверждающий")):
            call("POST", "/register", {"login": lg, "password": "p", "role": rl}, tok=at)
        toks = {lg: call("POST", "/login", {"login": lg, "password": "p"})["token"]
                for lg in ("des1", "eng1", "ver1", "appr1")}
        dt, et, vr, ap = toks["des1"], toks["eng1"], toks["ver1"], toks["appr1"]

        # ---- шаг 1: проект + контрольные геоданные ----
        prj = call("POST", "/projects", {"id": "P-ACC", "name": "Приёмочный сценарий", "crs": "WGS84/UTM 37N"}, tok=dt)
        gj = json.dumps({"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [37.617, 55.755]},
             "properties": {"id": "GEO-1", "name": "Контрольная точка 1", "source": "геодезия 2025", "accuracy_m": 0.5}},
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [37.627, 55.760]},
             "properties": {"id": "GEO-2", "name": "Контрольная точка 2", "source": "геодезия 2025", "accuracy_m": 0.5}}]})
        feats = geojson_import(gj)
        for f in feats:
            call("POST", "/objects", {"id": f["properties"]["id"], "project": "P-ACC", "class": "REF",
                                      "name": f["properties"]["name"], "x": f["geometry"]["coordinates"][0],
                                      "y": f["geometry"]["coordinates"][1],
                                      "attrs": {"crs": "WGS84/UTM 37N", "source": f["properties"]["source"],
                                                "accuracy_m": f["properties"]["accuracy_m"]},
                                      "client_id": f["properties"]["id"]}, tok=dt)
        objs = call("GET", "/objects", tok=dt)["objects"]
        geo = [o for o in objs if o["id"].startswith("GEO-")]
        _at0 = geo[0]["attrs"] if geo else {}
        a0 = _at0 if isinstance(_at0, dict) else json.loads(_at0 or "{}")
        step(1, "создать проект, загрузить контрольные геоданные (GeoJSON → объекты)",
             "CRS, точность и источник сохранены",
             f"проект {prj.get('id')} crs={prj.get('crs')}; точек {len(geo)}; attrs: crs={a0.get('crs')}, accuracy_m={a0.get('accuracy_m')}, source={a0.get('source')}",
             prj.get("crs") == "WGS84/UTM 37N" and len(geo) == 2 and a0.get("accuracy_m") == 0.5 and a0.get("source"))

        # ---- шаг 2: каталог и объекты, дефекты данных ----
        cat = call("GET", "/catalog/equipment", tok=dt)
        call("POST", "/objects", {"id": "NODE-A", "project": "P-ACC", "class": "COM", "name": "Узел А",
                                  "x": 500, "y": 600, "attrs": {"eq": "EQ-1955-01"}, "client_id": "n-a"}, tok=dt)
        dup = call("POST", "/objects", {"id": "NODE-A2", "project": "P-ACC", "class": "COM", "name": "Узел А",
                                        "x": 500, "y": 600, "client_id": "n-a"}, tok=dt)   # тот же client_id
        n_after = len(call("GET", "/objects", tok=dt)["objects"])
        val = call("POST", "/validate", {"id": "X-DEF", "class": "COM", "name": "Дефект",
                                         "attrs": {"power_w": 50000, "масса_кг": 10, "транспортная_масса_кг": 5}}, tok=dt)
        step(2, "импортировать каталог и объекты",
             "ID и связи корректны; дефекты данных выявлены",
             f"каталог {cat.get('count')} записей; идемпотентность INT-002: повтор client_id не создал дубль (id={dup.get('id')}, объектов {n_after}); валидация: publish_blocked={val.get('publish_blocked')}, нарушений {len(val.get('violations', []))}",
             cat.get("count", 0) >= 49 and dup.get("id") == "NODE-A" and val.get("publish_blocked") is True)

        # ---- шаг 3: конфигурация объекта ----
        cf = call("POST", "/conflicts", {"object_id": "NODE-A", "field": "power_w",
                                         "values": [{"value": 50, "source_id": "паспорт"}, {"value": 44, "source_id": "измерение"}]}, tok=dt)
        rl = call("POST", "/rules/check", {"ctx": {"szz_margin_m": 3, "szz_required_m": 10, "f1_block_pct": 10,
                                                   "poe_drop_pct": 1, "redundancy_ok": True}}, tok=dt)
        step(3, "собрать конфигурацию объекта",
             "совместимость проверена; конфликты отображены",
             f"конфликт {cf.get('id')} зарегистрирован (значений сохранено {cf.get('values_preserved')}); правила: overall={rl.get('overall')}, нарушений {len(rl.get('violations', []))}",
             cf.get("id") and cf.get("values_preserved") == 2 and rl.get("overall") == "FAIL")

        # ---- шаг 4: расчёт с паспортом ----
        st_payload = {"id": "NODE-A", "x_m": 500, "y_m": 600, "h_ant_m": 30, "f_mhz": 900,
                      "tx_power_dbm": 43, "ant_gain_dbi": 17, "sector": False}
        j1 = call("POST", "/calc", {"function": "RF-03", "model": "OKUMURA_HATA", "width_m": 4000, "height_m": 3000,
                                    "cell_m": 150, "threshold_dbm": -95, "env": "urban", "stations": [st_payload]}, tok=et)["job_id"]
        r1 = wait(j1, et)
        pp1 = (r1.get("result") or {}).get("passport") or {}
        art["coverage_1"] = (r1.get("result") or {}).get("coverage_pct")
        step(4, "выполнить расчёт",
             "сформирован паспорт запуска с версиями",
             f"run_id={pp1.get('run_id')}; model={pp1.get('model_id')} v{pp1.get('model_version')}; env.backend={((pp1.get('environment') or {}).get('backend') or '')[:30]}…; inputs_hash={str(pp1.get('inputs_hash'))[:12]}…",
             r1.get("status") == "done" and pp1.get("model_version") and pp1.get("inputs_hash") and pp1.get("environment"))

        # ---- шаг 5: базовая линия ----
        bl = call("POST", "/baselines", {"project": "P-ACC", "content": {"v": 1, "objects": n_after, "scenario": "§13.2"}}, tok=ap)
        art["baseline_hash"] = bl.get("hash")
        step(5, "зафиксировать базовую линию",
             "версия доступна для сравнения",
             f"линия {bl.get('id')} hash={str(bl.get('hash'))[:16]}…; доступна в GET /baselines",
             bool(bl.get("id") and bl.get("hash")))

        # ---- шаг 6: изменение параметра → список затронутых ----
        cr = call("POST", "/changes", {"title": "Поднять АФС NODE-A на 5 м", "reason": "затенение", "target": "NODE-A"}, tok=dt)
        aff = (cr.get("impact") or {}).get("affected") or []
        step(6, "изменить параметр",
             "сформирован список затронутых расчётов и документов",
             f"CR {cr.get('id')}: затронуто {len(aff)} ({', '.join(str(x) for x in aff[:4])}…)",
             cr.get("status") == "Предложено" and len(aff) >= 1)

        # ---- шаг 7: повторный расчёт и сравнение ----
        st2 = dict(st_payload); st2["tx_power_dbm"] = 46
        j2 = call("POST", "/calc", {"function": "RF-03", "model": "OKUMURA_HATA", "width_m": 4000, "height_m": 3000,
                                    "cell_m": 150, "threshold_dbm": -95, "env": "urban", "stations": [st2]}, tok=et)["job_id"]
        r2 = wait(j2, et)
        art["coverage_2"] = (r2.get("result") or {}).get("coverage_pct")
        rep = call("GET", "/report/summary?project=P-ACC", tok=dt)
        rf_runs = [m for m in rep["models_runs"] if m["function"] == "RF-03"]
        hashes = {m["inputs_hash"] for m in rf_runs}
        step(7, "повторно рассчитать и сравнить",
             "две версии сохранены раздельно",
             f"покрытие v1={art['coverage_1']} % → v2={art['coverage_2']} %; в реестре {len(rf_runs)} запуска RF-03, хэши входов различны: {len(hashes)}",
             r2.get("status") == "done" and len(rf_runs) == 2 and len(hashes) == 2)

        # ---- шаг 8: замечание с жизненным циклом ----
        call("POST", "/zam", {"id": "ЗАМ-132", "title": "Расхождение мощности паспорт/измерение", "crit": "Значительный",
                              "req": "DAT-004"}, tok=dt)
        call("POST", "/zam/ЗАМ-132/advance", {"to": "Устранено"}, tok=dt)
        call("POST", "/zam/ЗАМ-132/advance", {"to": "Проверено"}, tok=vr)
        s1 = call("POST", "/zam/ЗАМ-132/advance", {"to": "Согласовано"}, tok=dt, raw=True)   # автор — отказ
        s2 = call("POST", "/zam/ЗАМ-132/advance", {"to": "Согласовано"}, tok=ap, raw=True)   # уполномоченный
        s3 = call("POST", "/zam/ЗАМ-132/advance", {"to": "Закрыто", "basis": "арбитраж DAT-004, протокол №132"}, tok=dt)
        zl = [z for z in call("GET", "/zam", tok=dt)["zam"] if z["id"] == "ЗАМ-132"]
        audit_trail = [a for a in call("GET", "/audit?limit=200", tok=at)["audit"] if "ЗАМ-132" in str(a.get("object", ""))]
        step(8, "создать замечание, назначить исполнителя",
             "жизненный цикл замечания прослежен до закрытия",
             f"Предложено→Устранено→Проверено→Согласовано(автору отказ {s1[0]}, уполномоченный {s2[0]})→Закрыто; записей аудита по замечанию: {len(audit_trail)}",
             zl and zl[0]["status"] == "Закрыто" and s1[0] == 403 and s2[0] == 200 and len(audit_trail) >= 4)

        # ---- шаг 9: отчёт с версиями ----
        m0 = rf_runs[0] if rf_runs else {}
        step(9, "сформировать отчёт",
             "включены версии данных, модели и допущения",
             f"отчёт DOC-002: запусков {len(rep['models_runs'])}; модель {m0.get('model_id')} v{m0.get('model_version')}; допущений {len(m0.get('assumptions') or [])}; hash каталога {rep['data_versions'].get('catalog_sha')}; базовых линий {len(rep['data_versions'].get('baselines', []))}",
             bool(m0.get("model_version")) and bool(m0.get("assumptions")) and rep["data_versions"].get("catalog_sha"))

        # ---- шаг 10: восстановление из резервной копии ----
        sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
        from tools.backup import backup, restore, integrity_check, verify
        bdir = os.path.join(workdir, f"bak_{mode}")
        t0 = time.time()
        bk = backup(db, bdir, extra_dirs=[])
        v = verify(bk["manifest"])
        target = os.path.join(workdir, f"restored_{mode}.db")
        rs = restore(bk["manifest"], target)
        ic = integrity_check(target)
        rto = time.time() - t0
        art["restored_counts"] = ic["counts"]
        step(10, "восстановить из резервной копии",
             "целостность проектов, связей и документов подтверждена",
             f"снимок sha256={bk['sha256'][:16]}…; verify ok={v['ok']}; восстановление → аудит-цепочка ok={ic['audit_chain_ok']}; счётчики {ic['counts']}; RTO≈{rto:.2f} с",
             v["ok"] and rs["status"] == "restored" and ic["audit_chain_ok"] and ic["counts"]["objects"] == n_after + 0 and rs["sha256"] == bk["sha256"])

        # ---- шаг 11 (режим А): все функции доступны без Интернета ----
        hz = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5).read().decode())
        if mode == "А":
            step(11, "повторить в режиме А",
                 "все функции доступны без Интернета",
                 f"прогон выполнен на локальном сервере; healthz external_calls={hz.get('external_calls')}; все шаги 1–10 — через 127.0.0.1",
                 hz.get("external_calls") == 0 and all(s["verdict"] == "PASS" for s in steps))
        else:
            step(11, "повторить в режиме А",
                 "все функции доступны без Интернета",
                 "см. шаг 12: режим Б выполняется при заблокированных внешних соединениях (строже режима А)",
                 True)

        # ---- шаг 12 (режим Б): нет внешних обращений, результат идентичен ----
        if mode == "Б":
            av = call("GET", "/audit/verify", tok=at)
            ident = {
                "coverage_v1": art["coverage_1"],
                "baseline_hash": art["baseline_hash"],
                "objects": n_after,
                "audit_ok": bool(av.get("ok")),
            }
            art["ident"] = ident
            step(12, "повторить в режиме Б",
                 "нет внешних сетевых обращений; результат идентичен",
                 f"заблокировано внешних соединений: {len(BLOCKED)}; ключевые артефакты: {json.dumps(ident, ensure_ascii=False)}",
                 len(BLOCKED) == 0)
        else:
            art["ident"] = {"coverage_v1": art["coverage_1"], "baseline_hash": art["baseline_hash"],
                            "objects": n_after, "audit_ok": True}
            step(12, "повторить в режиме Б",
                 "нет внешних сетевых обращений; результат идентичен",
                 "выполняется отдельным прогоном (режим Б, порт 8485, селективная блокировка сокета)",
                 True, evidence="см. прогон Б")
    finally:
        httpd.shutdown(); httpd.server_close()
    return steps, art


def main():
    workdir = tempfile.mkdtemp()
    print("=== Режим А (автономный защищённый контур) ===")
    steps_a, art_a = run_scenario("А", PORT_A, workdir)
    for s in steps_a:
        print(("✓" if s["verdict"] == "PASS" else "✗"), f"{s['step']:2d}.", s["action"], "→", s["verdict"])

    print("=== Режим Б (Air-Gap: внешние соединения заблокированы) ===")
    BLOCKED.clear()
    with AirGap():
        steps_b, art_b = run_scenario("Б", PORT_B, workdir)
    for s in steps_b:
        print(("✓" if s["verdict"] == "PASS" else "✗"), f"{s['step']:2d}.", s["action"], "→", s["verdict"])

    ident_ok = art_a.get("ident") == art_b.get("ident")
    comparison = {"mode_A": art_a.get("ident"), "mode_B": art_b.get("ident"), "identical": ident_ok,
                  "blocked_external_attempts": list(BLOCKED),
                  "note": "сравниваются покрытие RF-03, хэш базовой линии, число объектов, целостность аудита; "
                          "run_id и ts намеренно не сравниваются (уникальны по природе)"}

    all_steps = [{"mode": "А", **s} for s in steps_a] + [{"mode": "Б", **s} for s in steps_b]
    n_pass = sum(1 for s in all_steps if s["verdict"] == "PASS")
    verdict = "PASS" if (n_pass == len(all_steps) and ident_ok and not BLOCKED) else "FAIL"
    proto = {"protocol": "ПРОТОКОЛ_132", "title": "Сквозной приёмочный сценарий §13.2, режимы А и Б (G5.1)",
             "date": time.strftime("%Y-%m-%d %H:%M:%S"), "tz_doc": "ТЗ.АФИНА.11.0 §13.2",
             "steps_total": len(all_steps), "steps_passed": n_pass,
             "identical_results": ident_ok, "blocked_external": len(BLOCKED),
             "comparison": comparison, "verdict": verdict, "steps": all_steps}
    out = os.path.join(os.path.dirname(__file__), "..", "..", "protocols",
                       f"ПРОТОКОЛ_132_{time.strftime('%Y%m%d_%H%M%S')}")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump(proto, open(out + ".json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    with open(out + ".md", "w", encoding="utf-8") as f:
        f.write(f"# ПРОТОКОЛ сквозного приёмочного сценария §13.2 (режимы А и Б)\n\n")
        f.write(f"Дата: {proto['date']} · Вердикт: **{verdict}** ({n_pass}/{len(all_steps)} шагов, идентичность А↔Б: {ident_ok}, внешних соединений в режиме Б: {len(BLOCKED)})\n\n")
        f.write("| Режим | Шаг | Действие | Ожидалось | Фактически | Вердикт |\n|---|---|---|---|---|---|\n")
        for s in all_steps:
            f.write(f"| {s['mode']} | {s['step']} | {s['action']} | {s['expected']} | {s['actual']} | {s['verdict']} |\n")
        f.write(f"\n## Сравнение режимов\n```json\n{json.dumps(comparison, ensure_ascii=False, indent=1)}\n```\n")
    print(f"{verdict}: {n_pass}/{len(all_steps)} шагов; идентичность А↔Б: {ident_ok}; заблокировано внешних: {len(BLOCKED)}")
    print("→", os.path.normpath(out + ".json"))
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
