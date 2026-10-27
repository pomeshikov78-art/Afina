"""REST API сервера «Афина» (stdlib http.server; целевой порт Go — G2+, контракт тот же).

Версионирование: /api/v1/... Идемпотентность: PUT/POST с client_id не создаёт дубликатов
(INT-002). Все действия — через RBAC и аудит. Тяжёлые расчёты — через очередь (ARC-003).
"""
import json, math, os, re, secrets, sys, time, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.parse
from urllib.parse import urlparse, parse_qs, unquote

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from afina.server.core import Store, ROLE_OPS
from afina.eng6 import rf, models
from afina.eng6.passport import Passport
from afina.eng6.runner import CalcQueue
from afina.ai import rag as ai_rag

API = "/api/v1"
VERSION = "1.0.0"
_START_TIME = None  # инициализируется при старте сервера (для afina_uptime_seconds)


class AfinaApp:
    def __init__(self, db_path="data/afina.db", queue_path=None, requirements_path=None,
                 tz_path=None, ai_enabled=True):
        self.store = Store(db_path)
        self.db_path = db_path
        self.ai_enabled = ai_enabled
        self.tz_path = tz_path
        self.requirements_path0 = requirements_path
        self.rag = None
        base = os.path.dirname(db_path) or "."
        self.queue = CalcQueue(persist_path=queue_path or os.path.join(base, "queue.json"), workers=1)
        self.requirements_path = requirements_path
        self._idempotency = {}
        self.queue.register("RF-03", self._rf03)
        self.queue.register("RF-07", self._rf07)
        self.queue.register("PWR-01", self._pwr01)
        self.queue.register("OPT-01", self._opt01)
        self.queue.register("LCC-01", self._lcc01)
        self.queue.register("COSIM-01", self._cosim01)

    # ---------- обработчики расчётов (реальное ядро ENG6+) ----------
    def _rf03(self, payload, progress_cb=None):
        stations = [rf.BaseStation(**s) for s in payload["stations"]]
        p = Passport.new("RF-03", payload.get("model", "SPM"), models.VERSION,
                         payload.get("mode", "Engineering"), payload.get("level", "L1"),
                         {"stations": payload["stations"], "grid": payload.get("cell_m", 50),
                          "model": payload.get("model", "SPM")},
                         crs=payload.get("crs", "WGS84/локальная"))
        progress_cb and progress_cb(0.1)
        r = rf.coverage_grid(stations, payload["width_m"], payload["height_m"],
                             payload.get("cell_m", 50), model_key=payload.get("model", "SPM"),
                             env=payload.get("env", "urban"), threshold_dbm=payload.get("threshold_dbm", -95.0))
        progress_cb and progress_cb(0.9)
        # даунсэмпл сетки для передачи клиенту (LOD отображения ≠ LOD расчёта, 3DM-004)
        step = max(1, len(r.rx_dbm) // 120)
        grid = [row[::step] for row in r.rx_dbm[::step]]
        p.finish(status="done", results_ref=None,
                 applicability={"model_domain_ok": True, "assumptions": r.assumptions}, uncertainty_db=r.uncertainty_db)
        res = {"coverage_pct": r.coverage_pct, "threshold_dbm": r.threshold_dbm,
               "grid": [[round(v, 1) for v in row] for row in grid], "grid_step": step,
               "cell_m": r.cell_m, "assumptions": r.assumptions,
               "uncertainty_db": r.uncertainty_db, "passport": p.to_dict()}
        self._save_calc(p, payload, res)
        return res

    def _rf07(self, payload, progress_cb=None):
        lb = rf.LinkBudget(**{k: v for k, v in payload.items() if k in rf.LinkBudget.__dataclass_fields__})
        p = Passport.new("RF-07", "LINK_BUDGET", "1.0.0", "Engineering", "L1", payload)
        res = lb.result(); res["passport"] = None
        p.finish(status="done", uncertainty_db={"data": 1.0, "numeric": 0.1, "model_params": 1.5, "physical_model": 1.0})
        res["passport"] = p.to_dict()
        self._save_calc(p, payload, res)
        return res

    def _pwr01(self, payload, progress_cb=None):
        from afina.eng6.pwr_thm import PowerSystem, Load
        ps = PowerSystem(loads=[Load(**l) for l in payload["loads"]],
                         ups_battery_wh=payload.get("ups_battery_wh", 20000),
                         gen_fuel_hours=payload.get("gen_fuel_hours", 8))
        p = Passport.new("PWR-01", "PWR_BALANCE", "1.0.0", "Engineering", "L1", payload)
        res = ps.balance(ambient_c=payload.get("ambient_c", 20))
        p.finish(status="done")
        res["passport"] = p.to_dict()
        self._save_calc(p, payload, res)
        return res

    def _opt01(self, payload, progress_cb=None):
        from afina.eng6.opt import optimize
        p = Passport.new("OPT-01", "OPT_MULTI", "1.0.0", "Engineering", "L1", payload)
        progress_cb and progress_cb(0.2)
        try:
            res = optimize(payload.get("variants", []), payload.get("criteria", []), payload.get("constraints", []))
        except ValueError as e:
            p.finish(status="error")
            return {"error": str(e), "passport": p.to_dict(),
                    "policy": "неподдержанная спецификация отклоняется, а не игнорируется (запрет тихой деградации)"}
        progress_cb and progress_cb(0.9)
        p.finish(status="done")
        res["passport"] = p.to_dict()
        self._save_calc(p, payload, res)
        return res

    def _lcc01(self, payload, progress_cb=None):
        from afina.eng6.opt import lcc
        p = Passport.new("LCC-01", "LCC_TCO", "1.0.0", "Engineering", "L1", payload)
        res = lcc(float(payload.get("capex", 0)), int(payload.get("years", 10)),
                  float(payload.get("opex_per_year", 0)), float(payload.get("discount", 0.08)),
                  float(payload.get("disposal", 0)))
        p.finish(status="done")
        res["passport"] = p.to_dict()
        self._save_calc(p, payload, res)
        return res

    def _cosim01(self, payload, progress_cb=None):
        """ENG-005: многофизическое сопряжение мощность→тепло→допустимость→доступность."""
        from afina.eng6.cosim import pwr_thm_rel_solvers, run_chain, check_units
        solvers, wiring, mappings = pwr_thm_rel_solvers()
        p = Passport.new("COSIM-01", "PWR-01→THM-01→REL-01", "1.0.0", "Engineering", "L1", payload)
        by_id = {s.id: s for s in solvers}
        problems = []
        for (a, b), mp in zip(wiring, mappings):
            problems += check_units(by_id[a], by_id[b], mp)
        progress_cb and progress_cb(0.3)
        sid = payload.get("scenario_id") or f"CS-{int(time.time() * 1000) % 10 ** 8}"
        res = run_chain(sid, solvers, wiring, mappings, payload.get("seed", {}))
        res["unit_checks"] = problems or "единицы согласованы на всех связках (ENG-004)"
        res["requirement"] = "единый ID сценария, версии решателей, контроль единиц и циклов (§4.5.6)"
        p.finish(status="done" if res["ok"] else "error",
                 warnings=problems or None)
        if res["ok"]:
            res["passport"] = p.to_dict()
            self._save_calc(p, payload, res)
        else:
            res["passport"] = p.to_dict()
        return res

    def _save_calc(self, p: Passport, payload, res):
        self.store.x("""INSERT OR REPLACE INTO calcs(run_id,function,model,mode,level,inputs_hash,payload,status,result_ref,passport,created,finished,created_by)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (p.run_id, p.function, p.model_id, p.mode, p.level, p.inputs_hash,
                      json.dumps(payload, ensure_ascii=False)[:8000], "done", None,
                      json.dumps(p.to_dict(), ensure_ascii=False), p.started_at, p.finished_at, "server"))

    # ---------- маршруты ----------
    def routes(self):
        R = []
        def add(m, pat, fn, op="read", auth=True):
            R.append((m, re.compile("^" + API + pat + "$"), fn, op, auth))
        add("POST", "/register", self.r_register, auth=False)
        add("POST", "/login", self.r_login, auth=False)
        add("GET", "/session", self.r_session)
        add("GET", "/objects", self.r_objects_list)
        add("POST", "/objects", self.r_object_create, op="edit_models_cond")
        add("POST", "/locks", self.r_lock, op="edit_models_cond")
        add("DELETE", "/locks", self.r_unlock, op="edit_models_cond")
        add("POST", "/calc", self.r_calc_submit, op="run_calc")
        add("GET", "/calc/(.*)", self.r_calc_get)
        add("GET", "/queue", self.r_queue)
        add("GET", "/notifications", self.r_ntf_list)
        add("POST", "/notifications/(\\d+)/advance", self.r_ntf_advance)
        add("GET", "/zam", self.r_zam_list)
        add("POST", "/zam", self.r_zam_create, op="edit_models_cond")
        add("POST", "/zam/([\\w-]+)/advance", self.r_zam_advance)
        add("POST", "/telemetry", self.r_telemetry)
        add("GET", "/audit", self.r_audit, op="audit_read")
        add("GET", "/audit/verify", self.r_audit_verify, op="audit_read")
        add("GET", "/export/objects", self.r_export_objects, op="export_limited")
        add("POST", "/baselines", self.r_baseline_fix, op="baseline_fix")
        add("GET", "/baselines", self.r_baseline_list)
        add("GET", "/catalog/equipment", self.r_catalog)
        add("GET", "/catalog/org_units", self.r_org_units)
        add("GET", "/requirements", self.r_requirements)
        add("GET", "/terrain", self.r_terrain)
        add("POST", "/ai/ask", self.r_ai_ask)
        add("GET", "/ai/anomalies", self.r_ai_anomalies)
        # ---- G3 ----
        add("GET", "/twins/([^/]+)", self.r_twin_get)
        add("POST", "/twins/([^/]+)/divergence", self.r_twin_divergence, op="edit_models_cond")
        add("POST", "/twins/calibrate", self.r_twin_calibrate, op="run_calc")
        add("POST", "/twins/predict", self.r_twin_predict, op="run_calc")
        add("GET", "/changes", self.r_changes_list)
        add("POST", "/changes", self.r_change_create, op="edit_models_cond")
        add("POST", "/changes/([\\w.-]+)/advance", self.r_change_advance)
        add("GET", "/rules", self.r_rules)
        add("POST", "/rules/check", self.r_rules_check)
        add("POST", "/validate", self.r_validate)
        add("GET", "/conflicts", self.r_conflicts_list)
        add("POST", "/conflicts", self.r_conflict_create, op="edit_models_cond")
        add("POST", "/conflicts/([\\w.-]+)/resolve", self.r_conflict_resolve)
        add("POST", "/archive/([\\w.\\-]+)", self.r_archive_create, op="baseline_fix")
        add("POST", "/ntf", self.r_ntf_create)
        add("POST", "/escalation", self.r_escalation)
        # ---- G4 ----
        add("POST", "/twins/([^/]+)/scenario", self.r_scenario_run, op="run_calc")
        add("GET", "/scenarios", self.r_scenarios_list)
        add("POST", "/scenarios/([\\w.-]+)/validate", self.r_scenario_validate, op="verify")
        add("POST", "/ai/explain", self.r_ai_explain)
        add("POST", "/ai/suggest", self.r_ai_suggest)
        add("POST", "/surrogate/train", self.r_surrogate_train, op="run_calc")
        add("GET", "/surrogates", self.r_surrogates_list)
        add("POST", "/surrogate/predict", self.r_surrogate_predict)
        add("POST", "/surrogates/([\\w.-]+)/compare", self.r_surrogate_compare, op="run_calc")
        add("POST", "/surrogates/([\\w.-]+)/validate", self.r_surrogate_validate, op="verify")
        add("POST", "/surrogates/([\\w.-]+)/disable", self.r_surrogate_disable, op="dict_edit")
        # ---- G4.4: военно-технический профиль (ВТ-01…08) ----
        add("GET", "/vt/cooperation", self.r_vt_cooperation)
        add("POST", "/vt/participants", self.r_vt_participant_add, op="dict_edit")
        add("POST", "/vt/transfers", self.r_vt_transfer_add, op="export_limited")
        add("GET", "/vt/stages", self.r_vt_stages_list)
        add("POST", "/vt/stages", self.r_vt_stage_create, op="assign")
        add("POST", "/vt/stages/([\\w.-]+)/mark", self.r_vt_stage_mark, op="tasks")
        add("POST", "/vt/stages/([\\w.-]+)/commission", self.r_vt_stage_commission, op="approve")
        add("GET", "/vt/benches", self.r_vt_benches_list)
        add("POST", "/vt/benches", self.r_vt_bench_add, op="dict_edit")
        add("POST", "/vt/tests", self.r_vt_test_register, op="tasks")
        add("GET", "/vt/tests/([\\w.-]+)/passport", self.r_vt_test_passport)
        add("POST", "/vt/tests/([\\w.-]+)/approve", self.r_vt_test_approve, op="verify")
        add("GET", "/vt/unification", self.r_vt_unification)
        add("GET", "/vt/readiness", self.r_vt_readiness)
        add("POST", "/vt/conditions", self.r_vt_condition_add, op="tasks")
        add("GET", "/vt/conditions", self.r_vt_conditions_check)
        add("GET", "/vt/aging", self.r_vt_aging)
        add("GET", "/vt/panel", self.r_vt_panel)
        # ---- G4.5: геймификация и обучение (§4.11) ----
        add("GET", "/game/rules", self.r_game_rules)
        add("POST", "/game/award", self.r_game_award)
        add("GET", "/game/scores", self.r_game_scores)
        add("POST", "/game/revoke", self.r_game_revoke, op="verify")
        add("GET", "/game/polygon", self.r_game_polygon)
        add("GET", "/game/indices", self.r_game_indices)
        add("GET", "/game/missions", self.r_game_missions)
        add("POST", "/game/missions/([\\w-]+)/start", self.r_mission_start)
        add("POST", "/game/missions/([\\w-]+)/check", self.r_mission_check)
        add("POST", "/game/missions/([\\w-]+)/hint", self.r_mission_hint)
        # ---- G4.6: аналитика руководителя ----
        add("GET", "/analytics/trends", self.r_analytics_trends)
        add("POST", "/surrogate/demo_train", self.r_surrogate_demo_train, op="run_calc")
        # ---- G5.1: проект и сводный отчёт (§13.2 шаги 1, 9; DOC-002) ----
        add("POST", "/projects", self.r_project_create, op="edit_models_cond")
        add("GET", "/projects", self.r_projects_list)
        add("GET", "/report/summary", self.r_report_summary)
        return R

    # ---------- обработчики ----------
    def r_register(self, s, body):
        users = s.app.store.q("SELECT COUNT(*) c FROM users")[0]["c"]
        tok = (s.headers.get("Authorization") or "").replace("Bearer ", "")
        sess = s.app.store.session(tok) if tok else None
        if users > 0 and not (sess and sess["role"] == "Администратор системы"):
            s.app.store.audit(sess["login"] if sess else "аноним", sess["role"] if sess else "-",
                              "ОТКАЗ регистрации пользователя", body.get("login", "?"), "sec", "требуется Администратор системы")
            return 403, {"error": "регистрация доступна администратору системы (первый пользователь — bootstrap)"}
        if body.get("role") not in ROLE_OPS:
            return 400, {"error": f"роль не из матрицы Приложения Н: {sorted(ROLE_OPS)}"}
        s.app.store.register_user(body["login"], body["password"], body["role"], body.get("display"))
        s.app.store.audit(body["login"], body["role"], "регистрация пользователя", body["login"], "sec")
        return 200, {"ok": True, "login": body["login"], "role": body["role"]}

    def r_login(self, s, body):
        r = s.app.store.login(body.get("login", ""), body.get("password", ""))
        if not r:
            s.app.store.audit(body.get("login", "?"), "-", "неуспешный вход", "auth", "sec", "отказано")
            return 401, {"error": "неверные учётные данные"}
        s.app.store.audit(r["login"], r["role"], "вход в систему", "сессия", "sec")
        return 200, r

    def r_session(self, s, body):
        return 200, {"login": s.user["login"], "role": s.user["role"], "display": s.user["display"]}

    def r_objects_list(self, s, body):
        rows = s.app.store.q("SELECT * FROM objects ORDER BY id")
        return 200, {"objects": [s.app.store.sanitize_for_export(r) for r in rows]}

    def r_object_create(self, s, body):
        oid = body.get("id") or ("OBJ-" + str(int(time.time() * 1000) % 10 ** 8))
        # идемпотентность INT-002: client_id
        cid = body.get("client_id")
        if cid:
            if cid in s.app._idempotency:
                return 200, s.app._idempotency[cid]
        lk = s.app.store.q("SELECT * FROM locks WHERE object_id=?", (oid,))
        if lk and lk[0]["user_login"] != s.user["login"]:
            return 409, {"error": f"объект заблокирован: {lk[0]['user_login']} (совместная сессия)"}
        s.app.store.x("""INSERT OR REPLACE INTO objects(id,project,class,name,x,y,z,attrs,ts_code,dat_status,source,recorded_at,created_by)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (oid, body.get("project", "P-1"), body.get("class", "COM"), body.get("name", oid),
                   body.get("x", 0), body.get("y", 0), body.get("z", 0),
                   json.dumps(body.get("attrs", {}), ensure_ascii=False),
                   body.get("ts_code", "TS-0"), body.get("dat_status", "Введено"),
                   body.get("source", s.user["login"]), time.time(), s.user["login"]))
        s.app.store.audit(s.user["login"], s.user["role"], "создание объекта", oid)
        out = {"id": oid, "ok": True}
        if cid: s.app._idempotency[cid] = out
        return 200, out

    def r_lock(self, s, body):
        oid = body["object_id"]
        ex = s.app.store.q("SELECT * FROM locks WHERE object_id=?", (oid,))
        if ex and ex[0]["user_login"] != s.user["login"]:
            s.app.store.audit(s.user["login"], s.user["role"], "ОТКАЗ блокировки: объект занят", oid, "sec", "конфликт сессий")
            return 409, {"error": f"уже заблокирован: {ex[0]['user_login']}"}
        s.app.store.x("INSERT OR REPLACE INTO locks(object_id,user_login,session,since) VALUES(?,?,?,?)",
                  (oid, s.user["login"], body.get("session", ""), time.time()))
        return 200, {"locked": oid, "by": s.user["login"]}

    def r_unlock(self, s, body):
        oid = body["object_id"]
        s.app.store.x("DELETE FROM locks WHERE object_id=? AND user_login=?", (oid, s.user["login"]))
        return 200, {"unlocked": oid}

    def r_calc_submit(self, s, body):
        fn = body.get("function")
        if fn not in ("RF-03", "RF-07", "PWR-01", "OPT-01", "LCC-01", "COSIM-01"):
            return 400, {"error": f"функция «{fn}» не зарегистрирована в очереди — запуск невозможен (тихая подмена запрещена)"}
        jid = s.app.queue.submit(fn, body)
        s.app.store.audit(s.user["login"], s.user["role"], f"постановка расчёта {fn} в очередь", jid)
        return 202, {"job_id": jid, "status": "queued"}

    def r_calc_get(self, s, body, job_id=None):
        snap = s.app.queue.jobs.get(job_id)
        if not snap:
            return 404, {"error": "задание не найдено"}
        from dataclasses import asdict
        d = asdict(snap)
        if d["status"] == "done" and d["result_ref"] and os.path.exists(d["result_ref"]):
            d["result"] = json.load(open(d["result_ref"], encoding="utf-8"))
        return 200, d

    def r_queue(self, s, body):
        return 200, s.app.queue.snapshot()

    def r_ntf_list(self, s, body):
        return 200, {"notifications": s.app.store.q("SELECT * FROM notifications ORDER BY id DESC LIMIT 200")}

    def r_ntf_advance(self, s, body, nid=None):
        LC = ["Создано", "В очереди", "Доставлено", "Просмотрено", "Подтверждено", "Исполнено", "Закрыто"]
        rows = s.app.store.q("SELECT * FROM notifications WHERE id=?", (int(nid),))
        if not rows: return 404, {"error": "нет такого уведомления"}
        cur = rows[0]["status"]
        nxt = LC[min(LC.index(cur) + 1, len(LC) - 1)] if cur in LC else "Просмотрено"
        s.app.store.x("UPDATE notifications SET status=? WHERE id=?", (nxt, int(nid)))
        s.app.store.audit(s.user["login"], s.user["role"], f"уведомление #{nid} → {nxt}", "NTF")
        return 200, {"id": int(nid), "status": nxt, "note": "доставка ≠ просмотр ≠ исполнение; уведомление не подтверждает изменение данных"}

    def r_zam_list(self, s, body):
        return 200, {"zam": s.app.store.q("SELECT * FROM zam ORDER BY id")}

    def r_zam_create(self, s, body):
        zid = body.get("id") or ("ЗАМ-" + str(int(time.time()) % 100000))
        s.app.store.x("INSERT OR REPLACE INTO zam(id,title,crit,org,req,status,basis,own_user,created) VALUES(?,?,?,?,?,?,?,?,?)",
                  (zid, body.get("title", ""), body.get("crit", "Минорный"), body.get("org", ""),
                   body.get("req", ""), "Предложено", body.get("basis", ""), s.user["login"], time.time()))
        s.app.store.x("INSERT INTO notifications(kind,title,body,created) VALUES(?,?,?,?)",
                  ("замечание", f"Новое замечание {zid}", body.get("title", ""), time.time()))
        s.app.store.audit(s.user["login"], s.user["role"], "создание замечания", zid)
        return 200, {"id": zid, "status": "Предложено"}

    def r_zam_advance(self, s, body, zid=None):
        CH = ["Предложено", "Устранено", "Проверено", "Согласовано", "Закрыто"]
        rows = s.app.store.q("SELECT * FROM zam WHERE id=?", (zid,))
        if not rows: return 404, {"error": "замечание не найдено"}
        z = rows[0]
        nxt = body.get("to") or (CH[min(CH.index(z["status"]) + 1, len(CH) - 1)] if z["status"] in CH else "Устранено")
        # Maker–Checker (SEC-005): согласование — только утверждающий и не автор
        if nxt == "Согласовано":
            if s.user["role"] != "Уполномоченный утверждающий":
                s.app.store.audit(s.user["login"], s.user["role"], "ОТКАЗ согласования замечания (роль)", zid, "sec", "SEC-005")
                return 403, {"error": "согласование доступно только роли «Уполномоченный утверждающий» (SEC-005); событие залогировано"}
            if z["own_user"] == s.user["login"]:
                s.app.store.audit(s.user["login"], s.user["role"], "ОТКАЗ согласования (автор устранения)", zid, "sec", "Maker–Checker")
                return 403, {"error": "нельзя согласовать замечание, которое вы создали/устраняли (Maker–Checker)"}
        if nxt == "Закрыто" and not body.get("basis"):
            return 400, {"error": "закрытие требует основания (§4.8.6)"}
        s.app.store.x("UPDATE zam SET status=?, basis=? WHERE id=?", (nxt, body.get("basis", z["basis"]), zid))
        s.app.store.audit(s.user["login"], s.user["role"], f"замечание {zid} → «{nxt}»", zid, "data", body.get("basis", ""))
        return 200, {"id": zid, "status": nxt}

    def r_telemetry(self, s, body):
        # DGT-002: телеметрия НЕ изменяет базовую конфигурацию — только наблюдения
        s.app.store.x("INSERT INTO telemetry(object_id,ts,metric,value,quality) VALUES(?,?,?,?,?)",
                  (body["object_id"], body.get("ts", time.time()), body["metric"], float(body["value"]),
                   body.get("quality", "наблюдение")))
        return 200, {"ok": True, "note": "записано как наблюдение; базовая линия не изменена (DGT-002)"}

    def r_audit(self, s, body):
        lim = int(s.query.get("limit", ["100"])[0])
        return 200, {"audit": s.app.store.q("SELECT ts,user_login,role,action,object,class,result FROM audit ORDER BY id DESC LIMIT ?", (lim,))}

    def r_audit_verify(self, s, body):
        return 200, s.app.store.audit_verify()

    def r_export_objects(self, s, body):
        cat = s.query.get("category", ["ВНУТРЕННЯЯ"])[0]
        rows = [s.app.store.sanitize_for_export(r, cat) for r in s.app.store.q("SELECT * FROM objects")]
        s.app.store.audit(s.user["login"], s.user["role"], f"экспорт объектов ({cat})", f"{len(rows)} записей", "sec", "SEC-004: закрытые поля исключены")
        return 200, {"category": cat, "count": len(rows), "objects": rows,
                     "note": "персональные данные и закрытые атрибуты исключены (SEC-004); перенос не создаёт сетевого моста (AUT-005)"}

    def r_baseline_fix(self, s, body):
        import hashlib as H
        content = json.dumps(body.get("content", {}), ensure_ascii=False, sort_keys=True)
        bid = body.get("id") or ("BL-" + time.strftime("%Y.%m") + "-" + secrets.token_hex(2).upper())
        s.app.store.x("INSERT INTO baselines(id,project,status,created,created_by,content_hash,content) VALUES(?,?,?,?,?,?,?)",
                  (bid, body.get("project", "P-1"), "утверждена", time.time(), s.user["login"],
                   H.sha256(content.encode()).hexdigest(), content))
        s.app.store.audit(s.user["login"], s.user["role"], "фиксация базовой линии", bid, "sec")
        return 200, {"id": bid, "hash": H.sha256(content.encode()).hexdigest(),
                     "note": "предыдущие версии воспроизводимы; телеметрия линию не изменяет (DGT-002)"}

    def r_baseline_list(self, s, body):
        return 200, {"baselines": s.app.store.q("SELECT id,project,status,created,created_by,content_hash FROM baselines ORDER BY created DESC")}

    def r_catalog(self, s, body):
        q = s.query.get("q", [""])[0].lower()
        cls = s.query.get("class", [""])[0]
        rows = s.app.store.q("SELECT * FROM equipment")
        if q:
            rows = [r for r in rows if q in (r["name"] + " " + r["maker"] + " " + r["id"] + " " + (r["bands"] or "")).lower()]
        if cls:
            rows = [r for r in rows if r["class"] == cls]
        y0 = s.query.get("from_year"); y1 = s.query.get("to_year")
        if y0: rows = [r for r in rows if (r["year_from"] or 0) >= int(y0[0])]
        if y1: rows = [r for r in rows if (r["year_to"] or r["year_from"] or 9999) <= int(y1[0])]
        return 200, {"count": len(rows), "equipment": rows}

    def r_org_units(self, s, body):
        return 200, {"org_units": s.app.store.q("SELECT * FROM org_units ORDER BY id")}

    def r_requirements(self, s, body):
        if s.app.requirements_path and os.path.exists(s.app.requirements_path):
            d = json.load(open(s.app.requirements_path, encoding="utf-8"))
            return 200, d
        return 200, {"note": "путь к Приложению А не настроен", "requirements": []}

    # ---------- рельеф для 3D/WebGL-клиента (демо-данные, маркированы) ----------
    def r_terrain(self, s, body):
        from afina.gis.mtw import HeightMatrix
        pts = []
        for gx in range(16):
            for gy in range(16):
                h = 120 + 55 * math.sin(gx / 2.7) * math.cos(gy / 3.4) + (45 if (gx == 3 and gy == 3) else 0) + (25 if (gx == 12 and gy == 9) else 0)
                pts.append((gx * 400.0, gy * 400.0, h))
        hm = HeightMatrix.from_points(pts, cell_m=200, source="демо-рельеф (синтетический, учебный)", accuracy_m=5.0)
        return 200, {"cell_m": hm.cell_m, "rows": hm.rows, "cols": hm.cols, "x0": hm.x0, "y0": hm.y0,
                     "data": [[round(v, 1) for v in row] for row in hm.data], "meta": hm.meta,
                     "note": "синтетический рельеф для демонстрации WebGL-конвейера; целевые данные — MTW/DB3D заказчика (G1.1)"}

    # ---------- ИИ-слой (§4.10) ----------
    def _ensure_rag(self):
        if self.rag is None:
            self.rag = ai_rag.RagIndex(enabled=self.ai_enabled)
            if not self.rag.enabled:
                return self.rag
            if self.tz_path and os.path.exists(self.tz_path):
                self.rag.add_markdown(self.tz_path, doc_id="ТЗ.АФИНА.11.0")
            if self.requirements_path0 and os.path.exists(self.requirements_path0):
                d = json.load(open(self.requirements_path0, encoding="utf-8"))
                for r in d.get("requirements", []):
                    self.rag.add_text("ПриложениеА", r["id"],
                                      f"{r['id']} ({r['priority']}): {r['requirement']} — критерий: {r['acceptance_criteria']}")
            rows = self.store.q("SELECT * FROM equipment LIMIT 200")
            for r in rows:
                self.rag.add_text("каталог_КСИМ", r["id"],
                                  f"{r['id']} {r['name']} · {r['maker']} · {r['country']} · {r['year_from']}–{r['year_to'] or 'н.в.'} · {r['bands']} · качество данных: {r['data_quality']}")
        return self.rag

    def r_ai_ask(self, s, body):
        if not s.app.ai_enabled:
            return 200, {"status": "disabled", "note": "ИИ-слой отключён (AI-003); ГИС и расчёты работают штатно"}
        idx = s.app._ensure_rag()
        a = idx.answer(body.get("q", ""))
        s.app.store.audit(s.user["login"], s.user["role"], "ИИ-запрос (локальный RAG)",
                          body.get("q", "")[:60], "data", a.get("status", ""))
        return 200, a

    def r_ai_anomalies(self, s, body):
        if not s.app.ai_enabled:
            return 200, {"status": "disabled", "count": 0, "anomalies": [],
                         "note": "ИИ-слой отключён (AI-003); сканирование аномалий недоступно до включения"}
        rows = s.app.store.q("SELECT * FROM equipment")
        found = ai_rag.scan_anomalies(rows)
        return 200, {"count": len(found), "anomalies": found,
                     "policy": "автоисправление запрещено: всё выносится на арбитраж (DAT-004)"}


    # ================= G3: цифровые двойники =================
    def r_twin_get(self, s, body, oid=None):
        from afina.twin import TwinService
        tw = TwinService(s.app.store)
        oid = urllib.parse.unquote(oid)
        return 200, {"object_id": oid, "layers": tw.layers(oid), "observed": tw.observed(oid),
                     "dt": tw.dt_level(oid)}

    def r_twin_divergence(self, s, body, oid=None):
        from afina.twin import TwinService
        oid = urllib.parse.unquote(oid)
        proto = TwinService(s.app.store).divergence(oid, body.get("project", {}))
        s.app.store.audit(s.user["login"], s.user["role"],
                          f"протокол отклонений проект↔факт: {len(proto['divergences'])} расхождений", oid, "data", "DGT-003")
        return 200, proto

    def r_twin_calibrate(self, s, body):
        from afina.twin import TwinService
        res = TwinService.calibrate_ls([tuple(p) for p in body.get("points", [])], body.get("passport"))
        s.app.store.audit(s.user["login"], s.user["role"], f"калибровка двойника: {res.get('status')}",
                          "-", "data", "паспорт не изменён (DGT-005)")
        return 200, res

    def r_twin_predict(self, s, body):
        from afina.twin import TwinService
        res = TwinService.predict_linear([tuple(p) for p in body.get("history", [])], int(body.get("horizon", 12)))
        return 200, res

    # ================= G3: изменения и конфигурация =================
    def r_changes_list(self, s, body):
        rows = s.app.store.q("SELECT * FROM changes ORDER BY created DESC LIMIT 100")
        for r in rows:
            r["affected"] = json.loads(r.get("affected") or "{}")
        return 200, {"changes": rows}

    def r_change_create(self, s, body):
        from afina.cfg import build_graph_from_store, CHANGE_STATES
        cid = f"CR-{int(time.time()*1000)%1000000}"
        g = build_graph_from_store(s.app.store)
        target = body.get("target", "-")
        impact = g.impact(target) if target in g.adj else {"affected": [], "counts": {}, "start": target}
        s.app.store.x("INSERT INTO changes VALUES(?,?,?,?,?,?,?,?,?)",
                      (cid, body.get("title", ""), body.get("reason", ""), CHANGE_STATES[0], s.user["login"],
                       time.time(), json.dumps(impact, ensure_ascii=False), None, None))
        s.app.store.audit(s.user["login"], s.user["role"], f"изменение {cid} предложено: {body.get('title','')[:50]}",
                          cid, "data", f"затронуто вершин: {len(impact.get('affected', []))}")
        return 200, {"id": cid, "status": CHANGE_STATES[0], "impact": impact,
                     "rule": "предварительный анализ влияния ДО утверждения (CFG-002); ключевые изменения не применяются без расчёта влияния"}

    def r_change_advance(self, s, body, cid=None):
        from afina.cfg import CHANGE_STATES
        cid = urllib.parse.unquote(cid)
        rows = s.app.store.q("SELECT * FROM changes WHERE id=?", (cid,))
        if not rows:
            return 404, {"error": "изменение не найдено"}
        cr = rows[0]
        i = CHANGE_STATES.index(cr["status"]) if cr["status"] in CHANGE_STATES else 0
        nxt = CHANGE_STATES[min(i + 1, len(CHANGE_STATES) - 1)]
        if nxt == "Утверждено" and (s.user["role"] != "Уполномоченный утверждающий" or s.user["login"] == cr["author"]):
            s.app.store.audit(s.user["login"], s.user["role"], f"ОТКАЗ утверждения изменения {cid}", cid, "sec", "SEC-005 Maker–Checker")
            return 403, {"error": "утверждает только Уполномоченный утверждающий и не автор изменения (SEC-005)"}
        vnew = cr["version_new"]
        if nxt == "Внесено":
            # базовая линия фиксируется ДО изменения; старые результаты сохраняются со своей конфигурацией (§4.8.3)
            import hashlib as H
            snapshot = json.dumps({"objects": len(s.app.store.q("SELECT id FROM objects")),
                                   "calcs": len(s.app.store.q("SELECT run_id FROM calcs")), "ts": time.time()}, sort_keys=True)
            vnew = f"BL-CR-{cid}"
            s.app.store.x("INSERT INTO baselines VALUES(?,?,?,?,?,?,?)",
                          (vnew, "P-1", "утверждена", time.time(), s.user["login"],
                           H.sha256(snapshot.encode()).hexdigest(), snapshot))
            s.app.store.x("UPDATE changes SET version_new=? WHERE id=?", (vnew, cid))
        if nxt == "Утверждено":
            s.app.store.x("UPDATE changes SET approver=? WHERE id=?", (s.user["login"], cid))
        s.app.store.x("UPDATE changes SET status=? WHERE id=?", (nxt, cid))
        s.app.store.audit(s.user["login"], s.user["role"], f"изменение {cid}: {cr['status']} → {nxt}", cid, "data",
                          f"версия конфигурации: {vnew or cr['version_new'] or '-'}")
        return 200, {"id": cid, "from": cr["status"], "to": nxt, "version_new": vnew or cr["version_new"]}

    def r_rules(self, s, body):
        from afina.cfg import DEFAULT_RULES
        return 200, {"rules": DEFAULT_RULES,
                     "note": "правила — данные с версией и обоснованием, не код (CFG-004); изменение правила не переписывает утверждённые результаты задним числом"}

    def r_rules_check(self, s, body):
        from afina.cfg import eval_rules
        res = eval_rules(body.get("ctx", {}), body.get("rules"))
        s.app.store.audit(s.user["login"], s.user["role"], f"проверка инженерных правил: {res['overall']}",
                          "-", "data", f"нарушений {len(res['violations'])}, предупреждений {len(res['warnings'])}")
        return 200, res

    # ================= G3: качество данных =================
    def r_validate(self, s, body):
        from afina.dat import validate_object
        return 200, validate_object(body)

    def r_conflicts_list(self, s, body):
        rows = s.app.store.q("SELECT * FROM conflicts ORDER BY created DESC LIMIT 100")
        for r in rows:
            r["values"] = json.loads(r.pop("vals", None) or "[]")
        return 200, {"conflicts": rows}

    def r_conflict_create(self, s, body):
        from afina.dat import ConflictRegistry
        res = ConflictRegistry(s.app.store).register(body.get("object_id", "-"), body.get("field", "-"), body.get("values", []))
        s.app.store.audit(s.user["login"], s.user["role"], f"конфликт {res['id']} зарегистрирован",
                          body.get("object_id", "-"), "data", "все значения сохранены (DAT-004)")
        return 200, res

    def r_conflict_resolve(self, s, body, cid=None):
        from afina.dat import ConflictRegistry
        cid = urllib.parse.unquote(cid)
        return 200, ConflictRegistry(s.app.store).resolve(cid, body.get("winner", "?"),
                                                          s.user["login"], s.user["role"], body.get("basis", ""))

    def r_archive_create(self, s, body, bid=None):
        from afina.dat import create_reproducibility_archive
        bid = urllib.parse.unquote(bid)
        out = os.path.join(os.path.dirname(os.path.abspath(s.app.db_path or "afina.db")), "archives")
        res = create_reproducibility_archive(s.app.store, bid, out)
        if "error" in res:
            return 404, res
        s.app.store.audit(s.user["login"], s.user["role"], f"архив воспроизводимости {bid}", bid, "data",
                          f"sha256:{res['sha256'][:16]}…")
        return 200, res

    # ================= G3: уведомления =================
    def r_ntf_create(self, s, body):
        from afina.ntf import NotifyService
        return 200, NotifyService(s.app.store).notify(body.get("kind", "info"), body.get("title", ""),
                                                      body.get("body", ""), body.get("dedup_key"))

    def r_escalation(self, s, body):
        from afina.ntf import NotifyService
        res = NotifyService(s.app.store).escalation_scan()
        if res["escalated"]:
            s.app.store.audit(s.user["login"], s.user["role"],
                              f"сканирование эскалации: {len(res['escalated'])} уведомлений эскалировано",
                              "NTF", "sec", "NTF-003")
        return 200, res

    # ================= G4.1: сценарный модуль (DT-5) =================
    def r_scenario_run(self, s, body, oid=None):
        from afina.twin import ScenarioService
        oid = urllib.parse.unquote(oid)
        rec = ScenarioService(s.app.store).run(oid, body.get("kind", ""), body.get("params", {}), s.user["login"])
        if rec.get("status") != "insufficient_data" and "error" not in rec:
            s.app.store.audit(s.user["login"], s.user["role"],
                              f"сценарий {rec.get('id', '?')} ({rec.get('kind')}): результат — оценка", oid, "data", "DT-5")
        return 200, rec

    def r_scenarios_list(self, s, body):
        from afina.twin import ScenarioService
        oid = s.query.get("object_id", [None])[0]
        return 200, {"scenarios": ScenarioService(s.app.store).list(oid)}

    def r_scenario_validate(self, s, body, sid=None):
        from afina.twin import ScenarioService
        res = ScenarioService(s.app.store).validate(sid, s.user["login"], body.get("basis", ""))
        if res.get("status") == "validated":
            s.app.store.audit(s.user["login"], s.user["role"], f"независимая проверка сценария {sid}", sid, "data",
                              body.get("basis", "")[:80])
        return 200, res

    # ================= G4.1: объяснения ИИ (§4.10) =================
    def r_ai_explain(self, s, body):
        from afina.ai.explain import explain_calc
        if not s.app.ai_enabled:
            return 200, {"status": "disabled", "note": "ИИ-слой отключён (AI-003); ГИС и инженерное ядро работают штатно"}
        run_id = body.get("run_id")
        passport = body.get("passport")
        if run_id and not passport:
            rows = s.app.store.q("SELECT passport FROM calcs WHERE run_id=?", (run_id,))
            if not rows or not rows[0]["passport"]:
                return 200, {"status": "insufficient_data", "answer": f"Запуск «{run_id}» не найден — запрашиваю данные.",
                             "sources": [], "versions": {}, "assumptions": [], "limitations": [], "confirmation_status": "не подтверждено"}
            passport = json.loads(rows[0]["passport"])
        rag = s.app._ensure_rag()
        out = explain_calc(passport or {}, rag=rag)
        s.app.store.audit(s.user["login"], s.user["role"], "ИИ-объяснение расчёта", run_id or "-", "data", out.get("status", ""))
        return 200, out

    def r_ai_suggest(self, s, body):
        from afina.ai.explain import suggest_scenarios
        from afina.twin import TwinService
        if not s.app.ai_enabled:
            return 200, {"status": "disabled", "note": "ИИ-слой отключён (AI-003); ГИС и инженерное ядро работают штатно"}
        oid = body.get("object_id", "")
        rows = s.app.store.q("SELECT * FROM objects WHERE id=?", (oid,))
        tw = TwinService(s.app.store)
        obs = tw.observed(oid)["observed"] if rows else {}
        calcs = s.app.store.q("SELECT COUNT(*) c FROM calcs WHERE payload LIKE ?", (f'%{oid}%',))
        out = suggest_scenarios(rows[0] if rows else None, obs, bool(calcs and calcs[0]["c"]))
        s.app.store.audit(s.user["login"], s.user["role"], "ИИ-предложение сценариев", oid or "-", "data", out.get("status", ""))
        return 200, out

    # ================= G4.3: суррогатные модели (MOD-006) =================
    def _load_surrogate(self, sid):
        from afina.eng6.surrogate import PolynomialSurrogate
        rows = self.store.q("SELECT * FROM surrogates WHERE id=?", (sid,))
        if not rows:
            return None, None
        sur = PolynomialSurrogate.from_dict(json.loads(rows[0]["payload"]))
        sur.disabled = bool(rows[0]["disabled"])   # колонка — источник истины (отключение без переобучения)
        sur.status = rows[0]["status"]
        return sur, rows[0]

    def r_surrogate_train(self, s, body):
        from afina.eng6.surrogate import PolynomialSurrogate
        sid = body.get("id") or f"SUR-{int(time.time() * 1000) % 10 ** 8}"
        sur = PolynomialSurrogate(sid, body.get("model_ref", "?"), int(body.get("degree", 2)),
                                  body.get("feature_names"))
        res = sur.train([list(map(float, p)) for p in body.get("points", [])])
        if res["status"] != "trained":
            return 200, res
        s.app.store.x("""INSERT OR REPLACE INTO surrogates(id,model_ref,kind,payload,status,disabled,created,created_by)
                         VALUES(?,?,?,?,?,0,?,?)""",
                      (sid, sur.model_ref, f"poly{sur.degree}", json.dumps(sur.to_dict(), ensure_ascii=False),
                       sur.status, time.time(), s.user["login"]))
        s.app.store.audit(s.user["login"], s.user["role"], f"обучение суррогата {sid} ({sur.model_ref})", sid, "data",
                          f"RMSE {res['rmse']}; статус «Проверяется» до независимого сравнения")
        return 200, res

    def r_surrogates_list(self, s, body):
        rows = s.app.store.q("SELECT id,model_ref,kind,status,disabled,created,created_by FROM surrogates ORDER BY created DESC")
        return 200, {"surrogates": rows, "mark": "СУРРОГАТ — не приравнивается к физическому решателю (§4.5.8)"}

    def r_surrogate_predict(self, s, body):
        sur, row = s.app._load_surrogate(body.get("id", ""))
        if not sur:
            return 404, {"error": "суррогат не найден"}
        return 200, sur.predict([float(v) for v in body.get("x", [])])

    def r_surrogate_compare(self, s, body, sid=None):
        from afina.eng6.surrogate import rf01_solver
        sur, row = s.app._load_surrogate(sid)
        if not sur:
            return 404, {"error": "суррогат не найден"}
        if not sur.model_ref.startswith("RF-01"):
            return 400, {"error": f"для model_ref «{sur.model_ref}» основной решатель сравнения не подключён — "
                                  "статус «Валидирована» без сравнения не выдаётся (честный отказ)"}
        solver = rf01_solver(body.get("model_key", "OKUMURA_HATA"), body.get("env", "urban"))
        try:
            res = sur.compare(solver, [list(map(float, x)) for x in body.get("test_points", [])])
        except Exception as e:
            return 400, {"error": f"сравнение не выполнено: {e!r} — результат основного решателя не подменяется"}
        s.app.store.x("UPDATE surrogates SET compare_result=? WHERE id=?",
                      (json.dumps({k: res[k] for k in ("status", "n_test", "rmse_independent", "max_abs_err")},
                                  ensure_ascii=False), sid))
        s.app.store.audit(s.user["login"], s.user["role"], f"сравнение суррогата {sid} с основным решателем",
                          sid, "data", f"RMSE(незав.) {res.get('rmse_independent')}")
        return 200, res

    def r_surrogate_validate(self, s, body, sid=None):
        sur, row = s.app._load_surrogate(sid)
        if not sur:
            return 404, {"error": "суррогат не найден"}
        if not row["compare_result"]:
            return 400, {"error": "валидация без сравнения с основным решателем невозможна (§4.5.8, §4.5.9)"}
        if not body.get("basis", "").strip():
            return 400, {"error": "требуется основание валидации"}
        s.app.store.x("UPDATE surrogates SET status=?, validated_by=?, validation_basis=? WHERE id=?",
                      ("Валидирована для указанной области", s.user["login"], body["basis"], sid))
        s.app.store.audit(s.user["login"], s.user["role"], f"суррогат {sid} → «Валидирована для указанной области»",
                          sid, "data", body["basis"][:80])
        return 200, {"status": "Валидирована для указанной области", "id": sid,
                     "domain": sur.domain, "note": "валидация действительна ТОЛЬКО внутри сохранённой области применения"}

    def r_surrogate_disable(self, s, body, sid=None):
        sur, row = s.app._load_surrogate(sid)
        if not sur:
            return 404, {"error": "суррогат не найден"}
        on = bool(body.get("disabled", True))
        s.app.store.x("UPDATE surrogates SET disabled=? WHERE id=?", (1 if on else 0, sid))
        s.app.store.audit(s.user["login"], s.user["role"], f"суррогат {sid}: disabled={on}", sid, "data",
                          "отключение суррогата не влияет на основное ядро (§4.5.8)")
        return 200, {"id": sid, "disabled": on}

    # ================= G4.4: военно-технический профиль (ВТ-01…08) =================
    def _vt(self, s):
        from afina.vt import VTService
        return VTService(s.app.store)

    def r_vt_cooperation(self, s, body):
        return 200, self._vt(s).cooperation_summary()

    def r_vt_participant_add(self, s, body):
        pid = body.get("id") or f"ORG-{int(time.time() * 1000) % 10 ** 7}"
        s.app.store.x("INSERT OR REPLACE INTO vt_participants VALUES(?,?,?,?,?,?,?)",
                      (pid, body.get("org", ""), body.get("role", ""), body.get("zone", ""),
                       body.get("contact", ""), time.time(), s.user["login"]))
        s.app.store.audit(s.user["login"], s.user["role"], f"участник кооперации {pid} зарегистрирован", pid, "data", body.get("zone", ""))
        return 200, {"id": pid, "ok": True}

    def r_vt_transfer_add(self, s, body):
        import hashlib as H
        tid = body.get("id") or f"ПД-{int(time.time() * 1000) % 10 ** 7}"
        pkg = json.dumps(body.get("package", {}), ensure_ascii=False, sort_keys=True)
        sha = H.sha256(pkg.encode()).hexdigest()
        s.app.store.x("INSERT OR REPLACE INTO vt_transfers VALUES(?,?,?,?,?,?,?,?,?)",
                      (tid, body.get("package_name", pkg[:40]), body.get("from_org", ""), body.get("to_org", ""),
                       body.get("category", "ВНУТРЕННЯЯ"), sha, int(body.get("items", 0)), time.time(), s.user["login"]))
        s.app.store.audit(s.user["login"], s.user["role"], f"передача комплекта данных {tid} → {body.get('to_org','?')}",
                          tid, "sec", f"sha256:{sha[:16]}…; категория {body.get('category','ВНУТРЕННЯЯ')} (SEC-004)")
        return 200, {"id": tid, "sha256": sha,
                     "note": "перенос не создаёт сетевого моста (AUT-005); закрытые поля исключаются на экспорте (SEC-004)"}

    def r_vt_stages_list(self, s, body):
        rows = s.app.store.q("SELECT * FROM vt_stages ORDER BY created DESC")
        vt = self._vt(s)
        return 200, {"stages": [vt.stage_view(r) for r in rows]}

    def r_vt_stage_create(self, s, body):
        sid = body.get("id") or f"ЭТАП-{int(time.time() * 1000) % 10 ** 7}"
        s.app.store.x("INSERT OR REPLACE INTO vt_stages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (sid, body.get("contract", ""), body.get("title", ""), body.get("due"),
                       0, 0, 0, 0, "{}", None, None, None, time.time()))
        s.app.store.audit(s.user["login"], s.user["role"], f"этап {sid} открыт: {body.get('title','')[:50]}", sid, "data")
        return 200, {"id": sid, "status": "Открыт"}

    def r_vt_stage_mark(self, s, body, sid=None):
        res = self._vt(s).mark_dimension(sid, body.get("dimension", ""), bool(body.get("value", True)),
                                         body.get("evidence", ""))
        if res.get("status") == "error":
            return 400, res
        s.app.store.audit(s.user["login"], s.user["role"],
                          f"этап {sid}: измерение «{body.get('dimension')}» = {body.get('value')}", sid, "data",
                          body.get("evidence", "")[:60])
        return 200, res

    def r_vt_stage_commission(self, s, body, sid=None):
        res = self._vt(s).commission_close(sid, s.user["login"], body.get("basis", ""))
        if res.get("status") == "error":
            return 400, res
        if res.get("status") == "refused":
            s.app.store.audit(s.user["login"], s.user["role"], f"ОТКАЗ закрытия этапа {sid}: материалы не готовы", sid, "data", "ВТ-02")
            return 409, res
        s.app.store.audit(s.user["login"], s.user["role"], f"этап {sid} ЗАКРЫТ решением комиссии", sid, "data",
                          body.get("basis", "")[:80])
        return 200, res

    def r_vt_benches_list(self, s, body):
        vt = self._vt(s)
        rows = s.app.store.q("SELECT * FROM vt_benches ORDER BY id")
        return 200, {"benches": [{**r, "metrology": vt.bench_status(r)} for r in rows]}

    def r_vt_bench_add(self, s, body):
        bid = body.get("id") or f"СТЕНД-{int(time.time() * 1000) % 10 ** 7}"
        s.app.store.x("INSERT OR REPLACE INTO vt_benches VALUES(?,?,?,?,?,?,?)",
                      (bid, body.get("name", ""), body.get("kind", ""), body.get("passport", ""),
                       body.get("calib_due"), body.get("status", "зарегистрирован"), time.time()))
        s.app.store.audit(s.user["login"], s.user["role"], f"стенд/СИ {bid} внесён в реестр", bid, "data",
                          f"поверка до {body.get('calib_due')}")
        return 200, {"id": bid, "ok": True}

    def r_vt_test_register(self, s, body):
        res = self._vt(s).register_test(body, s.user["login"])
        s.app.store.audit(s.user["login"], s.user["role"], f"испытание {res.get('id')} зарегистрировано",
                          res.get("id", "-"), "data", res.get("metrology_warning", "") or "СИ в норме")
        return 200, res

    def r_vt_test_passport(self, s, body, tid=None):
        p = self._vt(s).test_passport(tid)
        return (200, p) if p else (404, {"error": "испытание не найдено"})

    def r_vt_test_approve(self, s, body, tid=None):
        res = self._vt(s).approve_test(tid, s.user["login"], body.get("basis", ""))
        if res.get("status") == "error":
            return 400, res
        if res.get("status") == "refused":
            s.app.store.audit(s.user["login"], s.user["role"], f"ОТКАЗ согласования испытания {tid}: метрология", tid, "data", "§4.8.4")
            return 409, res
        s.app.store.audit(s.user["login"], s.user["role"], f"заключение испытания {tid} согласовано", tid, "data",
                          body.get("basis", "")[:60])
        return 200, res

    def r_vt_unification(self, s, body):
        return 200, self._vt(s).unification()

    def r_vt_readiness(self, s, body):
        reqs = None
        if s.app.requirements_path and os.path.exists(s.app.requirements_path):
            reqs = json.load(open(s.app.requirements_path, encoding="utf-8")).get("requirements", [])
        project = s.query.get("project", ["P-1"])[0]
        res = self._vt(s).readiness(project, reqs)
        s.app.store.audit(s.user["login"], s.user["role"], f"оценка формальной готовности {project}: уровень {res['formal_level']}/4",
                          project, "data", "ВТ-05: оценка, не решение комиссии")
        return 200, res

    def r_vt_condition_add(self, s, body):
        cid = body.get("id") or f"УСЛ-{int(time.time() * 1000) % 10 ** 7}"
        s.app.store.x("INSERT OR REPLACE INTO vt_conditions VALUES(?,?,?,?,?,?,?,?)",
                      (cid, body.get("object_id", "-"), body.get("factor", ""),
                       body.get("project_value"), body.get("actual_value"), body.get("unit", ""),
                       body.get("source", s.user["login"]), time.time()))
        return 200, {"id": cid, "ok": True}

    def r_vt_conditions_check(self, s, body):
        oid = s.query.get("object_id", [""])[0]
        if not oid:
            return 400, {"error": "укажите object_id"}
        return 200, self._vt(s).conditions_check(oid)

    def r_vt_aging(self, s, body):
        return 200, self._vt(s).aging()

    def r_vt_panel(self, s, body):
        return 200, self._vt(s).panel()

    # ================= G4.5: геймификация и обучение (§4.11) =================
    def _game(self, s):
        from afina.game import GameEngine
        return GameEngine(s.app.store)

    def r_game_rules(self, s, body):
        from afina.game import POINTS, FORBIDDEN
        return 200, {"points": POINTS, "forbidden_grounds": FORBIDDEN,
                     "note": "правила — данные с версией; визуальные награды отключаются без потери инженерной функциональности (§4.11.2)"}

    def r_game_award(self, s, body):
        res = self._game(s).award(s.user["login"], body.get("event", ""), body.get("ref", ""), body.get("body"))
        if res["status"] == "awarded":
            s.app.store.audit(s.user["login"], s.user["role"],
                              f"начисление {res['points']} баллов: {res['event']}", res.get("dedup_key", ""), "data", res["basis"])
        return 200, res

    def r_game_scores(self, s, body):
        return 200, self._game(s).scores()

    def r_game_revoke(self, s, body):
        res = self._game(s).revoke(int(body.get("award_id", 0)), s.user["login"], body.get("reason", ""))
        return 200 if res["status"] == "revoked" else 400, res

    def r_game_polygon(self, s, body):
        project = s.query.get("project", ["P-1"])[0]
        return 200, self._game(s).polygon(project)

    def r_game_indices(self, s, body):
        oid = s.query.get("object_id", [""])[0]
        if not oid:
            return 400, {"error": "укажите object_id"}
        return 200, self._game(s).readiness_indices(oid)

    def r_game_missions(self, s, body):
        from afina.game import Missions
        return 200, {"missions": Missions.IDS,
                     "note": "изолированные синтетические данные, трёхуровневые подсказки, объяснение «почему верно» (§4.11.6)"}

    def r_mission_start(self, s, body, mid=None):
        from afina.game import Missions
        mid = urllib.parse.unquote(mid)
        res = Missions().start(mid, int(body.get("seed", 42)))
        return 200 if res["status"] == "ok" else 404, res

    def r_mission_check(self, s, body, mid=None):
        from afina.game import Missions
        mid = urllib.parse.unquote(mid)
        res = Missions().check(mid, int(body.get("seed", 42)), body.get("answer"))
        if res.get("correct"):
            s.app.store.audit(s.user["login"], s.user["role"], f"учебная миссия «{mid}» пройдена (seed {body.get('seed', 42)})",
                              mid, "data", "учебный режим: на производственные реестры не влияет")
        return 200, res

    def r_mission_hint(self, s, body, mid=None):
        from afina.game import Missions
        mid = urllib.parse.unquote(mid)
        return 200, Missions().hint(mid, int(body.get("seed", 42)), int(body.get("level", 1)))

    # ================= G4.6: аналитика руководителя =================
    def r_analytics_trends(self, s, body):
        days = max(1, min(int(s.query.get("days", ["30"])[0]), 365))
        since = time.time() - days * 86400

        def series(sql):
            return {r["d"]: r["c"] for r in s.app.store.q(sql, (since,))}
        ser = {
            "объекты": series("SELECT strftime('%Y-%m-%d', recorded_at, 'unixepoch') d, COUNT(*) c FROM objects WHERE recorded_at>=? GROUP BY d"),
            "расчёты": series("SELECT strftime('%Y-%m-%d', created, 'unixepoch') d, COUNT(*) c FROM calcs WHERE created>=? GROUP BY d"),
            "замечания": series("SELECT strftime('%Y-%m-%d', created, 'unixepoch') d, COUNT(*) c FROM zam WHERE created>=? GROUP BY d"),
            "изменения": series("SELECT strftime('%Y-%m-%d', created, 'unixepoch') d, COUNT(*) c FROM changes WHERE created>=? GROUP BY d"),
            "сценарии": series("SELECT strftime('%Y-%m-%d', created, 'unixepoch') d, COUNT(*) c FROM scenarios WHERE created>=? GROUP BY d"),
            "испытания": series("SELECT strftime('%Y-%m-%d', created, 'unixepoch') d, COUNT(*) c FROM vt_tests WHERE created>=? GROUP BY d"),
            "иб_отказы": series("SELECT strftime('%Y-%m-%d', ts, 'unixepoch') d, COUNT(*) c FROM audit WHERE ts>=? AND (class='sec' OR action LIKE 'ОТКАЗ%') GROUP BY d"),
        }
        totals = {k: sum(v.values()) for k, v in ser.items()}
        return 200, {"days": days, "series": ser, "totals": totals,
                     "note": "агрегаты первичных реестров; каждый показатель раскрывается до записей (ВТ-08)"}

    def r_surrogate_demo_train(self, s, body):
        """Демонстрационное обучение суррогата НА ПРОВЕРЕННОМ основном решателе RF-01
        (сетка внутри домена применимости — MOD-003 соблюдается ядром)."""
        from afina.eng6.surrogate import PolynomialSurrogate, rf01_solver
        model_key = body.get("model_key", "OKUMURA_HATA")
        sid = body.get("id", f"SUR-RF01-{model_key}")
        solver = rf01_solver(model_key, body.get("env", "urban"))
        pts = []
        try:
            for f in (200, 500, 900, 1200, 1500):
                for d in (1, 3, 7, 12, 18):
                    for hb in (30, 60, 120):
                        for hm in (1, 2, 4):
                            pts.append([f, d, hb, hm, solver([f, d, hb, hm])])
        except Exception as e:
            return 400, {"error": f"основной решатель отклонил сетку (MOD-003): {e!r} — суррогат не обучается"}
        sur = PolynomialSurrogate(sid, f"RF-01/{model_key}", 2, ["f_mhz", "d_km", "h_bs", "h_ms"])
        res = sur.train(pts)
        if res["status"] != "trained":
            return 200, res
        s.app.store.x("""INSERT OR REPLACE INTO surrogates(id,model_ref,kind,payload,status,disabled,created,created_by)
                         VALUES(?,?,?,?,?,0,?,?)""",
                      (sid, sur.model_ref, "poly2", json.dumps(sur.to_dict(), ensure_ascii=False),
                       sur.status, time.time(), s.user["login"]))
        s.app.store.audit(s.user["login"], s.user["role"], f"демо-обучение суррогата {sid} на решателе RF-01", sid, "data",
                          f"{len(pts)} точек, RMSE {res['rmse']} дБ; статус «Проверяется»")
        return 200, res

    # ================= G5.1: проекты и сводный отчёт (§13.2, DOC-002) =================
    def r_project_create(self, s, body):
        pid = body.get("id") or f"P-{int(time.time()) % 100000}"
        s.app.store.x("INSERT OR REPLACE INTO projects(id,name,crs,created,created_by,baseline) VALUES(?,?,?,?,?,?)",
                      (pid, body.get("name", pid), body.get("crs", "WGS84"), time.time(), s.user["login"], None))
        s.app.store.audit(s.user["login"], s.user["role"], f"проект {pid} создан (CRS: {body.get('crs', 'WGS84')})", pid, "data")
        return 200, {"id": pid, "crs": body.get("crs", "WGS84"), "ok": True}

    def r_projects_list(self, s, body):
        return 200, {"projects": s.app.store.q("SELECT * FROM projects ORDER BY created")}

    def r_report_summary(self, s, body):
        project = s.query.get("project", ["P-1"])[0]
        calcs = s.app.store.q("SELECT run_id,function,model,mode,level,inputs_hash,passport,created FROM calcs ORDER BY created")
        models = []
        for c in calcs:
            try:
                pj = json.loads(c["passport"] or "{}")
            except Exception:
                pj = {}
            models.append({"run_id": c["run_id"], "function": c["function"], "model_id": pj.get("model_id", c["model"]),
                           "model_version": pj.get("model_version"), "mode": pj.get("mode"), "level": pj.get("level"),
                           "inputs_hash": pj.get("inputs_hash", c["inputs_hash"]),
                           "crs": pj.get("crs"), "environment": pj.get("environment"),
                           "uncertainty_db": pj.get("uncertainty_db"),
                           "assumptions": (pj.get("applicability") or {}).get("assumptions")})
        rep = {"project": project, "generated": time.time(), "generator": "Афина NextGen · DOC-002",
               "data_versions": {
                   "objects": s.app.store.q("SELECT COUNT(*) c FROM objects WHERE project=?", (project,))[0]["c"],
                   "baselines": s.app.store.q("SELECT id,content_hash,created,created_by FROM baselines WHERE project=? ORDER BY created", (project,)),
                   "catalog_sha": None},
               "models_runs": models,
               "requirements_ref": (os.path.basename(s.app.requirements_path) if s.app.requirements_path else None),
               "note": "отчёт включает версии данных, моделей и допущения (§13.2 шаг 9); допущения берутся из паспортов запусков, не сочиняются"}
        import hashlib as H
        cat = s.app.store.q("SELECT COUNT(*) c FROM equipment")[0]["c"]
        if cat:
            rep["data_versions"]["catalog_sha"] = H.sha256(json.dumps(s.app.store.q("SELECT id,year_from FROM equipment ORDER BY id"), ensure_ascii=False).encode()).hexdigest()[:16]
        return 200, rep


class Handler(BaseHTTPRequestHandler):
    app: "AfinaApp" = None
    routes = None

    def log_message(self, *a):  # тишина в stderr; структурированный лог — в аудит
        pass

    def _metrics(self):
        """Prometheus text exposition 0.0.4 — единая программа (UNIF-19).

        Без аутентификации (как /healthz и /metrics линии B): предполагается
        закрытый контур; в проде — за TLS/ingress. Отказ отдельного счётчика
        не рвёт выдачу: метрики независимы, ошибка фиксируется комментарием.
        """
        import time as _t
        st = self.app.store
        out = []

        def fam(name, help_, type_, samples):
            out.append(f"# HELP {name} {help_}")
            out.append(f"# TYPE {name} {type_}")
            out.extend(samples)

        def safe(fn):
            try:
                fn()
            except Exception as e:
                out.append(f"# afina_metric_error {type(e).__name__}: {e}")

        def m_up():
            up = (_t.time() - _START_TIME) if _START_TIME else 0.0
            fam("afina_up", "Платформа Афина жива", "gauge", ["afina_up 1"])
            fam("afina_uptime_seconds", "Аптайм процесса, с", "counter",
                [f"afina_uptime_seconds {up:.3f}"])
            fam("afina_build_info", "Версия платформы", "gauge",
                [f'afina_build_info{{version="{VERSION}"}} 1'])
        safe(m_up)

        def m_counts():
            n_obj = st.q("SELECT COUNT(*) c FROM objects")[0]["c"]
            fam("afina_objects_total", "Объектов обстановки", "gauge", [f"afina_objects_total {n_obj}"])
            n_users = st.q("SELECT COUNT(*) c FROM users")[0]["c"]
            fam("afina_users_total", "Пользователей", "gauge", [f"afina_users_total {n_users}"])
            n_audit = st.q("SELECT COUNT(*) c FROM audit")[0]["c"]
            fam("afina_audit_entries_total", "Записей мастер-журнала аудита", "counter",
                [f"afina_audit_entries_total {n_audit}"])
        safe(m_counts)

        def m_calcs():
            rows = st.q("SELECT status, COUNT(*) c FROM calcs GROUP BY status")
            fam("afina_calcs_total", "Расчётов по статусам", "gauge",
                [f'afina_calcs_total{{status="{r["status"]}"}} {r["c"]}' for r in rows] or ['afina_calcs_total 0'])
        safe(m_calcs)

        def m_queue():
            snap = self.app.queue.snapshot()
            counts = {}
            for j in snap.get("jobs", []):
                counts[j["status"]] = counts.get(j["status"], 0) + 1
            fam("afina_queue_jobs", "Заданий очереди расчётов по статусам (ARC-003)", "gauge",
                [f'afina_queue_jobs{{status="{k}"}} {v}' for k, v in sorted(counts.items())]
                or ['afina_queue_jobs 0'])
        safe(m_queue)

        body = ("\n".join(out) + "\n").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization,Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS")
        self.end_headers()
        self.wfile.write(b)

    def do_OPTIONS(self):
        self._json(204, {})

    def _dispatch(self, method):
        u = urlparse(self.path)
        self.query = parse_qs(unquote(u.query)) if u.query else {}
        u_path = unquote(u.path)
        if method == "GET" and u_path in ("/", "/index.html"):
            p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "client", "index.html")
            if os.path.exists(p):
                data = open(p, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers(); self.wfile.write(data)
                return
        if method == "GET" and u_path == "/healthz":
            return self._json(200, {"status": "ok", "version": VERSION, "external_calls": 0})
        if method == "GET" and u_path == "/metrics":
            return self._metrics()
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n).decode() or "{}") if n else {}
        for m, rx, fn, op, auth in self.routes:
            if m != method: continue
            mt = rx.match(u_path)
            if not mt: continue
            self.user = None
            if auth:
                tok = (self.headers.get("Authorization") or "").replace("Bearer ", "")
                self.user = self.app.store.session(tok)
                if not self.user:
                    return self._json(401, {"error": "сессия недействительна — требуется вход"})
                base_op = op[:-5] if op.endswith("_cond") else op
                if not self.app.store.can(self.user["role"], op) and not self.app.store.can(self.user["role"], base_op):
                    self.app.store.audit(self.user["login"], self.user["role"], f"ОТКАЗ RBAC: {op}", u.path, "sec", "запрещено")
                    return self._json(403, {"error": f"операция «{op}» недоступна для роли «{self.user['role']}» (SEC-001); событие залогировано"})
            try:
                args = mt.groups()
                code, res = fn(self, body, *args) if args else fn(self, body)
                self._json(code, res)
            except Exception as e:
                import traceback; traceback.print_exc()
                self._json(500, {"error": repr(e), "policy": "ошибка не подменяется упрощённым результатом (запрет тихой деградации)"})
            return
        self._json(404, {"error": "маршрут не найден", "path": u.path})

    def do_GET(self): self._dispatch("GET")
    def do_POST(self): self._dispatch("POST")
    def do_PUT(self): self._dispatch("PUT")
    def do_DELETE(self): self._dispatch("DELETE")


def create_server(db_path="data/afina.db", host="127.0.0.1", port=8470, requirements_path=None,
                  tz_path=None, ai_enabled=True):
    global _START_TIME
    if _START_TIME is None:
        import time as _t
        _START_TIME = _t.time()          # для afina_uptime_seconds (/metrics, UNIF-19)
    app = AfinaApp(db_path=db_path, requirements_path=requirements_path, tz_path=tz_path, ai_enabled=ai_enabled)
    Handler.app = app
    Handler.routes = app.routes()
    httpd = ThreadingHTTPServer((host, port), Handler)
    return httpd, app


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/afina.db")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8470)
    ap.add_argument("--requirements", default=None)
    ap.add_argument("--tz", default=None, help="путь к Афина_Техническое_задание.md для индекса RAG")
    ap.add_argument("--no-ai", action="store_true", help="полностью отключить ИИ-слой (AI-003); ГИС и расчёты работают штатно")
    a = ap.parse_args()
    httpd, app = create_server(a.db, a.host, a.port, a.requirements, tz_path=a.tz, ai_enabled=not a.no_ai)
    print(f"Афина API v{VERSION}: http://{a.host}:{a.port}{API} (БД: {a.db}) — локально, без внешних обращений"
          + ("" if not a.no_ai else " [ИИ-слой ОТКЛЮЧЁН: AI-003]"))
    httpd.serve_forever()
