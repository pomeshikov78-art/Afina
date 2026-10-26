"""Военно-технический профиль (§4.9, Приложение Ж): ВТ-01…ВТ-08.

Ключевые правила ТЗ, зашитые в логику:
- ВТ-02: наличие всех файлов в Системе НЕ означает выполнения этапа контракта;
  этап закрывается ТОЛЬКО решением уполномоченного органа.
- ВТ-03: если СИ не соответствует условиям применения (просрочена поверка),
  Система предупреждает о недопустимости использования его результатов.
- ВТ-05: выдаётся оценка ФОРМАЛЬНОЙ готовности, комиссия не подменяется;
  уровни не пропускаются «через голову».
- ВТ-07: каждый риск сопровождается вариантами реакции, а не приговором.
"""
import json
import time
from typing import Dict, List, Optional

VERSION = "1.0.0"

STAGE_STATUS = ["Открыт", "Материалы готовы", "Закрыт решением комиссии"]
FACTORS = ["Температура", "Влажность", "Пыль", "Влага", "Вибрация", "Удар",
           "ЭМС", "Питание", "Охлаждение", "Хранение"]
REACTIONS = {
    "EOL": ["последний закуп (last-time buy)", "поиск альтернативы в каталоге",
            "модернизация узла", "стратегический запас ЗИП"],
    "Единственный поставщик": ["квалификация второго поставщика", "унификация с аналогом",
                               "страховой запас", "пересмотр архитектуры узла"],
    "Отсутствие документации": ["запрос у держателя подлинника", "восстановление по данным эксплуатации",
                               "инженерное обследование с протоколом", "регистрация допуска «ограниченно применимо»"],
    "Данные не подтверждены": ["натурное измерение", "сверка с паспортом изделия",
                               "регистрация значения как «не подтверждено» до проверки"],
}


class VTService:
    def __init__(self, store):
        self.store = store

    # ================= ВТ-01: военно-техническая кооперация =================
    def cooperation_summary(self) -> Dict:
        parts = self.store.q("SELECT * FROM vt_participants ORDER BY id")
        transfers = self.store.q("SELECT * FROM vt_transfers ORDER BY created DESC LIMIT 100")
        zam = self.store.q("SELECT org, status, crit, COUNT(*) c FROM zam GROUP BY org, status, crit")
        by_org: Dict[str, Dict] = {}
        for z in zam:
            o = z["org"] or "—"
            by_org.setdefault(o, {"открыто": 0, "закрыто": 0, "критических_открыто": 0})
            if z["status"] in ("Закрыто", "Согласовано"):
                by_org[o]["закрыто"] += z["c"]
            else:
                by_org[o]["открыто"] += z["c"]
                if z["crit"] == "Критический":
                    by_org[o]["критических_открыто"] += z["c"]
        total = sum(v["открыто"] + v["закрыто"] for v in by_org.values())
        closed = sum(v["закрыто"] for v in by_org.values())
        return {
            "participants": parts, "zones": sorted({p["zone"] for p in parts if p["zone"]}),
            "transfers": transfers,
            "inter_org_zam": by_org,
            "summary_readiness_pct": round(100.0 * closed / total, 1) if total else None,
            "note": "сводная готовность — оценка по замечаниям и передачам; сроки этапов — в ВТ-02",
        }

    # ================= ВТ-02: контроль исполнения этапов =================
    def stage_view(self, row: Dict) -> Dict:
        dims = {"изделие": bool(row["product"]), "документация": bool(row["docs"]),
                "испытания": bool(row["tests"]), "комплект": bool(row["kit"])}
        status = row["commission"] or ("Материалы готовы" if all(dims.values()) else "Открыт")
        overdue = bool(row["due"] and not row["commission"] and row["due"] < time.time())
        return {**row, "dimensions": dims, "status": status, "overdue": overdue,
                "rule": "наличие всех файлов НЕ означает выполнения этапа; закрытие — только решением комиссии (§4.9)"}

    def mark_dimension(self, stage_id: str, dimension: str, value: bool, evidence: str = "") -> Dict:
        col = {"изделие": "product", "документация": "docs", "испытания": "tests", "комплект": "kit"}.get(dimension)
        if not col:
            return {"status": "error", "error": f"измерение «{dimension}» не из списка: изделие/документация/испытания/комплект"}
        rows = self.store.q("SELECT * FROM vt_stages WHERE id=?", (stage_id,))
        if not rows:
            return {"status": "error", "error": "этап не найден"}
        if rows[0]["commission"]:
            return {"status": "error", "error": "этап закрыт комиссией — измерения не редактируются; изменения через новый этап"}
        ev = json.loads(rows[0]["evidence"] or "{}")
        if evidence:
            ev[dimension] = evidence
        self.store.x(f"UPDATE vt_stages SET {col}=?, evidence=? WHERE id=?", (1 if value else 0, json.dumps(ev, ensure_ascii=False), stage_id))
        return self.stage_view(self.store.q("SELECT * FROM vt_stages WHERE id=?", (stage_id,))[0])

    def commission_close(self, stage_id: str, by: str, basis: str) -> Dict:
        """Закрытие этапа решением уполномоченного органа (роль проверяется на эндпоинте)."""
        rows = self.store.q("SELECT * FROM vt_stages WHERE id=?", (stage_id,))
        if not rows:
            return {"status": "error", "error": "этап не найден"}
        if not basis.strip():
            return {"status": "error", "error": "решение комиссии требует основания (протокол/документ)"}
        st = self.stage_view(rows[0])
        if not all(st["dimensions"].values()):
            return {"status": "refused",
                    "error": f"материалы этапа не готовы: {st['dimensions']} — решение комиссии не подменяет комплектность",
                    "rule": "комиссия закрывает этап, но Система честно показывает, чего не хватает"}
        self.store.x("UPDATE vt_stages SET commission=?, commission_by=?, commission_basis=? WHERE id=?",
                     (STAGE_STATUS[2], by, basis, stage_id))
        return {"status": "closed", "id": stage_id, "commission_by": by, "basis": basis,
                "note": "готовности изделия/документации/испытаний/комплекта разделены с решением комиссии (§4.9)"}

    # ================= ВТ-03: испытательная база и метрология =================
    def bench_status(self, bench: Dict, now: Optional[float] = None) -> Dict:
        now = now or time.time()
        due = bench.get("calib_due")
        if due is None:
            return {"state": "неизвестен", "warning": "срок поверки не указан — результаты НЕ применимы для подтверждения требования (§4.8.4)"}
        if due < now:
            return {"state": "просрочена поверка",
                    "warning": "СИ не соответствует условиям применения: результаты недопустимы для подтверждения требования (§4.8.4)"}
        return {"state": "действительна", "warning": None}

    def register_test(self, body: Dict, by: str) -> Dict:
        tid = body.get("id") or f"ИСП-{int(time.time() * 1000) % 10 ** 7}"
        bench_id = body.get("bench_id")
        warning = None
        if bench_id:
            b = self.store.q("SELECT * FROM vt_benches WHERE id=?", (bench_id,))
            warning = self.bench_status(b[0])["warning"] if b else f"стенд «{bench_id}» не зарегистрирован в реестре (§4.8.4)"
        self.store.x("""INSERT OR REPLACE INTO vt_tests(id,object_id,bench_id,basis,config_ref,method_ref,software,
                        conditions,raw,results,deviations,conclusion,approval,created,created_by)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (tid, body.get("object_id", "-"), bench_id, body.get("basis", ""),
                      body.get("config_ref", ""), body.get("method_ref", ""), body.get("software", ""),
                      json.dumps(body.get("conditions", {}), ensure_ascii=False),
                      json.dumps(body.get("raw", {}), ensure_ascii=False),
                      json.dumps(body.get("results", {}), ensure_ascii=False),
                      json.dumps(body.get("deviations", []), ensure_ascii=False),
                      body.get("conclusion", ""), "Не согласован", time.time(), by))
        out = {"id": tid, "approval": "Не согласован"}
        if warning:
            out["metrology_warning"] = warning
        return out

    def test_passport(self, tid: str) -> Optional[Dict]:
        """Цифровой паспорт испытания (§4.8.5): ID · объект · конфигурация · основание ·
        условия · оборудование · результаты · отклонения · заключение и статус согласования."""
        rows = self.store.q("SELECT * FROM vt_tests WHERE id=?", (tid,))
        if not rows:
            return None
        t = rows[0]
        bench = self.store.q("SELECT * FROM vt_benches WHERE id=?", (t["bench_id"],)) if t["bench_id"] else []
        met = self.bench_status(bench[0]) if bench else {"state": "стенд не указан", "warning": "привязка к прибору обязательна (§4.8.4)"}
        return {"id": t["id"], "object": t["object_id"], "configuration": t["config_ref"],
                "basis": t["basis"], "conditions": json.loads(t["conditions"] or "{}"),
                "equipment": {"bench_id": t["bench_id"], "metrology": met["state"], "warning": met["warning"],
                              "method": t["method_ref"], "software": t["software"]},
                "results": {"raw": json.loads(t["raw"] or "{}"), "processed": json.loads(t["results"] or "{}")},
                "deviations": json.loads(t["deviations"] or "[]"), "conclusion": t["conclusion"],
                "approval": t["approval"], "created": t["created"], "created_by": t["created_by"]}

    def approve_test(self, tid: str, by: str, basis: str) -> Dict:
        if not basis.strip():
            return {"status": "error", "error": "согласование заключения требует основания"}
        rows = self.store.q("SELECT * FROM vt_tests WHERE id=?", (tid,))
        if not rows:
            return {"status": "error", "error": "испытание не найдено"}
        if rows[0]["bench_id"]:
            b = self.store.q("SELECT * FROM vt_benches WHERE id=?", (rows[0]["bench_id"],))
            w = self.bench_status(b[0])["warning"] if b else None
            if w:
                return {"status": "refused", "error": f"согласование отклонено: {w}"}
        self.store.x("UPDATE vt_tests SET approval=? WHERE id=?", (f"Согласовано: {by} · {basis}", tid))
        return {"status": "approved", "id": tid, "by": by}

    # ================= ВТ-04: стандартизация и унификация =================
    def unification(self) -> Dict:
        eq = self.store.q("SELECT * FROM equipment")
        objs = self.store.q("SELECT id, attrs FROM objects")
        # дубли номенклатуры: одинаковое нормализованное имя
        by_name: Dict[str, List[str]] = {}
        for e in eq:
            key = "".join(ch for ch in (e["name"] or "").lower() if ch.isalnum())
            by_name.setdefault(key, []).append(e["id"])
        duplicates = {k: v for k, v in by_name.items() if len(v) > 1}
        # анализ заимствования: какое средство в скольких объектах использовано
        usage: Dict[str, int] = {}
        for o in objs:
            a = o["attrs"] or ""
            for e in eq:
                if e["id"] and e["id"] in a:
                    usage[e["id"]] = usage.get(e["id"], 0) + 1
        reuse = sorted(({"equipment": k, "objects": v} for k, v in usage.items()),
                       key=lambda x: -x["objects"])
        # последствия снятия с производства
        y = time.localtime().tm_year
        eol = [e for e in eq if e["year_to"] and e["year_to"] < y]
        consequences = []
        for e in eol:
            used_in = [o["id"] for o in objs if e["id"] and e["id"] in (o["attrs"] or "")]
            consequences.append({"equipment": e["id"], "name": e["name"], "year_to": e["year_to"],
                                 "affected_objects": used_in,
                                 "reactions": REACTIONS["EOL"]})
        return {"duplicates": duplicates, "reuse_top": reuse[:20],
                "reuse_rate_pct": round(100.0 * len(usage) / len(eq), 1) if eq else None,
                "eol_consequences": consequences,
                "note": "каталог типовых решений = наиболее заимствуемые позиции (reuse_top)"}

    # ================= ВТ-05: готовность к предъявлению =================
    def readiness(self, project: str = "P-1", requirements: Optional[List[dict]] = None) -> Dict:
        objs = self.store.q("SELECT * FROM objects WHERE project=?", (project,))
        incomplete = [o["id"] for o in objs if (o["x"] is None or o["y"] is None or (o["dat_status"] or "Не заполнено") == "Не заполнено")]
        gate1 = {"passed": bool(objs) and not incomplete, "objects": len(objs), "incomplete": incomplete,
                 "text": "комплектность: все объекты проекта имеют координаты и статус данных не «Не заполнено»"}
        baselines = self.store.q("SELECT COUNT(*) c FROM baselines WHERE project=? AND status='утверждена'", (project,))
        zam_closed_nobasis = self.store.q("SELECT COUNT(*) c FROM zam WHERE status='Закрыто' AND (basis IS NULL OR basis='')")
        gate2 = {"passed": baselines[0]["c"] > 0 and zam_closed_nobasis[0]["c"] == 0,
                 "baselines": baselines[0]["c"], "closed_zam_without_basis": zam_closed_nobasis[0]["c"],
                 "text": "документация: утверждённая базовая линия есть; закрытые замечания имеют основание"}
        covered, uncov = 0, []
        reqs = requirements or []
        if reqs:
            hay = json.dumps(self.store.q("SELECT basis FROM vt_tests") +
                             self.store.q("SELECT req FROM zam") +
                             self.store.q("SELECT title, reason FROM changes"), ensure_ascii=False)
            for r in reqs:
                if r.get("id") and r["id"] in hay:
                    covered += 1
                else:
                    uncov.append(r["id"])
            gate3 = {"passed": covered == len(reqs), "covered": covered, "total": len(reqs),
                     "uncovered_sample": uncov[:20],
                     "text": "подтверждение требований: каждое требование реестра связано с испытанием/замечанием/изменением"}
        else:
            gate3 = {"passed": False, "covered": 0, "total": 0,
                     "text": "подтверждение требований: реестр требований (Приложение А) не загружен — оценка невозможна"}
        comm = self.store.q("SELECT COUNT(*) c FROM vt_stages WHERE commission IS NOT NULL AND commission != ''")
        av = self.store.audit_verify()
        gate4 = {"passed": comm[0]["c"] > 0 and av.get("ok", False),
                 "stages_closed_by_commission": comm[0]["c"], "audit_chain_valid": av.get("ok"),
                 "text": "процедурная готовность: есть этап, закрытый решением комиссии; хэш-цепочка аудита действительна"}
        gates = [gate1, gate2, gate3, gate4]
        level = 0
        for g in gates:
            if g["passed"]:
                level += 1
            else:
                break
        return {"project": project, "formal_level": level, "max_level": 4, "gates": gates,
                "note": "ОЦЕНКА ФОРМАЛЬНОЙ готовности (§4.9, ВТ-05): уровни не пропускаются «через голову»; "
                        "решение о предъявлении принимает комиссия — Система её не подменяет"}

    # ================= ВТ-06: условия эксплуатации =================
    def conditions_check(self, object_id: str) -> Dict:
        rows = self.store.q("SELECT * FROM vt_conditions WHERE object_id=? ORDER BY factor", (object_id,))
        out = []
        for r in rows:
            pv, av = r["project_value"], r["actual_value"]
            status = "нет факта" if av is None else ("соответствует" if pv is None or abs(av - pv) < 1e-9 else
                                                     ("в допуске" if abs(av - pv) <= abs(pv) * 0.1 else "отклонение"))
            out.append({**r, "status": status})
        missing = [f for f in FACTORS if f not in {r["factor"] for r in rows}]
        return {"object_id": object_id, "factors": out, "missing_factors": missing,
                "note": "10 факторов §4.9 ВТ-06; сопоставление с проектом — оба значения сохраняются (DAT-004)"}

    # ================= ВТ-07: техническое старение и риски =================
    def aging(self) -> Dict:
        eq = self.store.q("SELECT * FROM equipment")
        y = time.localtime().tm_year
        risks = []
        makers: Dict[str, int] = {}
        for e in eq:
            makers[e["maker"]] = makers.get(e["maker"], 0) + 1
        total = len(eq) or 1
        for e in eq:
            if e["year_to"] and e["year_to"] < y and (e["status_kind"] or "") != "снят с производства":
                risks.append({"equipment": e["id"], "risk": "EOL", "detail": f"снят с производства {e['year_to']}, статус «{e['status_kind']}»",
                              "reactions": REACTIONS["EOL"]})
            if (e["data_quality"] or "").startswith("не "):
                risks.append({"equipment": e["id"], "risk": "Данные не подтверждены",
                              "detail": f"качество данных: {e['data_quality']}", "reactions": REACTIONS["Данные не подтверждены"]})
        singles = [m for m, c in makers.items() if c / total > 0.4]
        for m in singles:
            risks.append({"equipment": None, "risk": "Единственный поставщик",
                          "detail": f"«{m}» — {makers[m]} из {total} позиций каталога ({round(100*makers[m]/total)}%)",
                          "reactions": REACTIONS["Единственный поставщик"]})
        nodoc = [e["id"] for e in eq if not e["source"]]
        if nodoc:
            risks.append({"equipment": nodoc, "risk": "Отсутствие документации",
                          "detail": f"{len(nodoc)} позиций без указания источника", "reactions": REACTIONS["Отсутствие документации"]})
        return {"as_of_year": y, "risks": risks,
                "note": "каждый риск — с вариантами реакции (§4.9 ВТ-07); прогнозная техника до 2036 маркирована в каталоге честно"}

    # ================= ВТ-08: панель руководителя =================
    def panel(self) -> Dict:
        def c(sql, args=()):
            r = self.store.q(sql, args)
            return r[0]["c"] if r else 0
        dev = {"objects": c("SELECT COUNT(*) c FROM objects"),
               "changes_open": c("SELECT COUNT(*) c FROM changes WHERE status NOT IN ('Утверждено','Отклонено')"),
               "changes_approved": c("SELECT COUNT(*) c FROM changes WHERE status='Утверждено'")}
        docs = {"baselines": c("SELECT COUNT(*) c FROM baselines"),
                "zam_open": c("SELECT COUNT(*) c FROM zam WHERE status NOT IN ('Закрыто')"),
                "zam_closed": c("SELECT COUNT(*) c FROM zam WHERE status='Закрыто'")}
        tests = {"registered": c("SELECT COUNT(*) c FROM vt_tests"),
                 "approved": c("SELECT COUNT(*) c FROM vt_tests WHERE approval LIKE 'Согласовано%'"),
                 "benches": c("SELECT COUNT(*) c FROM vt_benches")}
        cfg = {"conflicts_open": c("SELECT COUNT(*) c FROM conflicts WHERE status='Открыт'"),
               "scenarios": c("SELECT COUNT(*) c FROM scenarios"),
               "scenarios_validated": c("SELECT COUNT(*) c FROM scenarios WHERE validated=1")}
        prod = {"equipment": c("SELECT COUNT(*) c FROM equipment"),
                "eol": c("SELECT COUNT(*) c FROM equipment WHERE year_to IS NOT NULL AND year_to < ?", (time.localtime().tm_year,))}
        rel = {"degradation_scenarios": c("SELECT COUNT(*) c FROM scenarios WHERE kind='degradation'"),
               "rel_calcs": c("SELECT COUNT(*) c FROM calcs WHERE function IN ('REL-01','COSIM-01') AND status='done'")}
        cost = {"lcc_calcs": c("SELECT COUNT(*) c FROM calcs WHERE function IN ('LCC-01','OPT-01') AND status='done'"),
                "stages_overdue": c("SELECT COUNT(*) c FROM vt_stages WHERE due IS NOT NULL AND due < ? AND (commission IS NULL OR commission='')", (time.time(),))}
        sec = {"audit_chain": self.store.audit_verify().get("ok"),
               "rbac_denials": c("SELECT COUNT(*) c FROM audit WHERE action LIKE 'ОТКАЗ%'"),
               "sec_events": c("SELECT COUNT(*) c FROM audit WHERE class='sec'")}
        return {"разработка": dev, "документация": docs, "испытания": tests, "конфигурация": cfg,
                "производство": prod, "надёжность": rel, "стоимость_сроки": cost, "ИБ": sec,
                "generated": time.time(),
                "note": "агрегаты из первичных реестров; каждый показатель кликом раскрывается до записей (ВТ-08)"}
