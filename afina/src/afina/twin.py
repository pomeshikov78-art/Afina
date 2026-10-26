"""Цифровые двойники (§4.6, DGT-001…005) — серверная логика.

Уровни DT-0…DT-5 присваиваются ПО ПРОВЕРЯЕМЫМ КРИТЕРИЯМ, а не по наличию функций.
Телеметрия обновляет только наблюдаемое состояние (DGT-002); паспортные и
идентифицированные параметры хранятся раздельно (DGT-005, EQP-002); прогноз —
оценка с горизонтом и неопределённостью (§4.6.3).
"""
import os
import time
from typing import Dict, List, Optional

VERSION = "1.0.0"

LAYERS = ["геометрический", "конструкционный", "функциональный", "электрический",
          "радиотехнический", "сетевой", "программный", "эксплуатационный",
          "измерительный", "расчётный", "документальный", "исторический"]

# проверяемые критерии уровней (§4.6.1)
DT_CRITERIA = {
    0: "цифровой паспорт: объект зарегистрирован (id, класс, происхождение)",
    1: "есть геометрия/координаты и структурные связи",
    2: "есть подключённые инженерные расчёты (паспорта)",
    3: "есть измерения/телеметрия и история эксплуатации",
    4: "оценка текущего состояния и протокол отклонений проект↔факт",
    5: "прогнозирование и сценарное моделирование (маркировано как оценка)",
}


class TwinService:
    def __init__(self, store):
        self.store = store

    # ---------- слои ----------
    def layers(self, object_id: str) -> List[Dict]:
        """Слои двойника: каждый со своей версией и статусом подтверждения при общем ID (§4.6.2)."""
        obj = self.store.q("SELECT * FROM objects WHERE id=?", (object_id,))
        if not obj:
            return []
        o = obj[0]
        has_geo = (o["x"] is not None and o["y"] is not None)
        calcs = self.store.q("SELECT COUNT(*) c FROM calcs WHERE payload LIKE ?", (f'%{object_id}%',))
        tele = self.store.q("SELECT COUNT(*) c FROM telemetry WHERE object_id=?", (object_id,))
        out = []
        for i, name in enumerate(LAYERS):
            filled = {
                "геометрический": has_geo,
                "электрический": bool(tele[0]["c"]) if tele else False,
                "измерительный": (tele[0]["c"] > 0) if tele else False,
                "расчётный": (calcs[0]["c"] > 0) if calcs else False,
                "документальный": True,
                "исторический": True,
            }.get(name, False)
            out.append({"layer": name, "version": f"v1.{i}" if filled else None,
                        "status": "подтверждён" if filled else "не заполнен",
                        "filled": filled})
        return out

    # ---------- наблюдаемое состояние (телеметрия) ----------
    def observed(self, object_id: str) -> Dict:
        rows = self.store.q("""SELECT metric, value, ts, quality FROM telemetry
                               WHERE object_id=? ORDER BY ts DESC LIMIT 500""", (object_id,))
        latest = {}
        for r in rows:
            if r["metric"] not in latest:
                latest[r["metric"]] = {"value": r["value"], "ts": r["ts"], "quality": r["quality"]}
        return {"object_id": object_id, "observed": latest,
                "note": "наблюдаемое состояние; базовая конфигурация НЕ изменяется (DGT-002)"}

    # ---------- уровень зрелости по критериям ----------
    def dt_level(self, object_id: str) -> Dict:
        obj = self.store.q("SELECT * FROM objects WHERE id=?", (object_id,))
        if not obj:
            return {"level": None, "error": "объект не найден"}
        o = obj[0]
        checks = {}
        checks[0] = True  # паспорт: объект зарегистрирован
        checks[1] = (o["x"] is not None and o["y"] is not None)
        calcs = self.store.q("SELECT COUNT(*) c FROM calcs WHERE payload LIKE ?", (f'%{object_id}%',))
        checks[2] = bool(calcs and calcs[0]["c"] > 0)
        tele = self.store.q("SELECT COUNT(*) c FROM telemetry WHERE object_id=?", (object_id,))
        checks[3] = bool(tele and tele[0]["c"] > 0)
        div = self.store.q("SELECT COUNT(*) c FROM audit WHERE object=? AND action LIKE '%отклонений%'", (object_id,))
        checks[4] = bool(div and div[0]["c"] > 0)
        # DT-5: сценарное моделирование работает И прошедшие сценарии независимо проверены
        # (прогноз без независимой проверки не выдаётся за достоверный — §4.10.2)
        scn = self.store.q("SELECT COUNT(*) c FROM scenarios WHERE object_id=? AND validated=1", (object_id,))
        checks[5] = bool(scn and scn[0]["c"] > 0)
        level = max([k for k, v in checks.items() if v], default=0)
        # уровень не присваивается «через голову»: все предыдущие критерии должны быть выполнены
        for k in range(level + 1):
            if not checks[k]:
                level = k - 1 if k > 0 else 0
                break
        return {"level": f"DT-{level}", "criteria": {f"DT-{k}": {"met": v, "text": DT_CRITERIA[k]} for k, v in checks.items()},
                "rule": "уровень присваивается по проверяемым критериям (DGT-004), не по наличию функций в меню"}

    # ---------- протокол расхождений проект ↔ факт (DGT-003) ----------
    def divergence(self, object_id: str, project_values: Dict) -> Dict:
        """project_values: {metric: паспортное/проектное значение}. Сравнение с наблюдаемым."""
        obs = self.observed(object_id)["observed"]
        rows = []
        for m, pv in project_values.items():
            ov = obs.get(m)
            if ov is None:
                rows.append({"metric": m, "project": pv, "observed": None,
                             "status": "нет наблюдений", "delta": None})
            else:
                delta = round(ov["value"] - pv, 4)
                rows.append({"metric": m, "project": pv, "observed": ov["value"], "delta": delta,
                             "observed_at": ov["ts"],
                             "status": "совпадает" if abs(delta) < 1e-9 else "расхождение"})
        proto = {"object_id": object_id, "created": time.time(), "rows": rows,
                 "divergences": [r for r in rows if r["status"] == "расхождение"],
                 "rule": "телеметрия, осмотр, документ и прогноз не подменяют друг друга (§6.4); оба значения сохраняются"}
        self.store.audit("система", "-", f"протокол отклонений проект↔факт ({len(proto['divergences'])} расхождений)",
                         object_id, "data", "DGT-003")
        return proto

    # ---------- калибровка/идентификация (DGT-005) ----------
    @staticmethod
    def calibrate_ls(points: List[tuple], passport: Optional[dict] = None) -> Dict:
        """Идентификация линейной модели y = a·x + b методом наименьших квадратов.

        Раздельное хранение: паспортные параметры НЕ изменяются; идентифицированные —
        отдельная запись с обучающими данными и проверкой на независимом наборе.
        При недостаточной информативности — честный отказ (запрет авто-изменения базовой линии).
        """
        if len(points) < 3:
            return {"status": "insufficient_data",
                    "note": "недостаточно информативности для надёжной оценки параметра — оценка не выдаётся (§4.6.4)"}
        n = len(points)
        sx = sum(p[0] for p in points); sy = sum(p[1] for p in points)
        sxx = sum(p[0] * p[0] for p in points); sxy = sum(p[0] * p[1] for p in points)
        den = n * sxx - sx * sx
        if abs(den) < 1e-12:
            return {"status": "insufficient_data", "note": "вырожденная выборка (x не варьируется)"}
        a = (n * sxy - sx * sy) / den
        b = (sy - a * sx) / n
        resid = [p[1] - (a * p[0] + b) for p in points]
        rmse = (sum(r * r for r in resid) / n) ** 0.5
        out = {"status": "identified", "a": round(a, 6), "b": round(b, 6), "rmse": round(rmse, 6),
               "train_points": n,
               "storage": "идентифицированные параметры хранятся ОТДЕЛЬНО от паспорта (DGT-005, EQP-002)",
               "validation_required": "проверка на независимом наборе обязательна до статуса VALIDATED"}
        if passport:
            out["passport"] = passport
            out["passport_unchanged"] = True
        return out

    # ---------- прогноз (оценка, не факт) ----------
    @staticmethod
    def predict_linear(history: List[tuple], horizon_steps: int = 12) -> Dict:
        """Линейный тренд-прогноз. ВСЕГДА маркируется как оценка (§4.6.3)."""
        cal = TwinService.calibrate_ls(history)
        if cal["status"] != "identified":
            return {"status": "insufficient_data", "note": cal["note"]}
        last_x = history[-1][0]
        preds = [{"x": last_x + i + 1, "y": round(cal["a"] * (last_x + i + 1) + cal["b"], 4)}
                 for i in range(horizon_steps)]
        return {"status": "forecast", "type": "оценка (не факт)", "model": "линейный тренд (МНК)",
                "horizon": horizon_steps, "points": preds,
                "uncertainty": f"RMSE обучающей выборки {cal['rmse']}; без независимой проверки достоверным не выдаётся",
                "requires_independent_validation": True}


# ================= G4.1: сценарный модуль (DT-5, §4.6.1, §4.7) =================
SCENARIO_KINDS = {
    "trend": "прогноз метрики по истории телеметрии (линейный тренд, МНК)",
    "param": "what-if: изменение наблюдаемой метрики на заданный процент",
    "degradation": "what-if: доступность при деградации MTBF/MTTR (ENG6+ REL-01)",
}


class ScenarioService:
    """Сценарное моделирование двойника. Результат ВСЕГДА маркируется «оценка»;
    базовая конфигурация, паспорт и телеметрия НЕ изменяются (DGT-002, §4.6.3)."""

    def __init__(self, store):
        self.store = store
        self.twin = TwinService(store)

    def run(self, object_id: str, kind: str, params: Dict, by: str = "система") -> Dict:
        import json as _json
        if kind not in SCENARIO_KINDS:
            return {"status": "error", "error": f"вид сценария «{kind}» не поддержан: {sorted(SCENARIO_KINDS)}",
                    "policy": "тихая подмена другим расчётом запрещена"}
        obj = self.store.q("SELECT * FROM objects WHERE id=?", (object_id,))
        if not obj:
            return {"status": "error", "error": "объект не найден — сценарий не запускается"}
        base = self.twin.observed(object_id)
        res: Dict = {"kind": kind, "description": SCENARIO_KINDS[kind]}

        if kind == "trend":
            metric = params.get("metric")
            rows = self.store.q("""SELECT ts, value FROM telemetry WHERE object_id=? AND metric=?
                                   ORDER BY ts""", (object_id, metric))
            if len(rows) < 3:
                return {"status": "insufficient_data",
                        "note": f"для метрики «{metric}» недостаточно наблюдений (<3) — прогноз не выдаётся (§4.6.4)",
                        "request": "загрузите историю телеметрии или укажите другую метрику"}
            hist = [(i + 1, r["value"]) for i, r in enumerate(rows)]
            fc = TwinService.predict_linear(hist, int(params.get("horizon", 6)))
            res["forecast"] = fc
            ok = fc.get("status") == "forecast"
        elif kind == "param":
            metric = params.get("metric")
            delta_pct = float(params.get("delta_pct", 0.0))
            ov = base["observed"].get(metric)
            if ov is None:
                return {"status": "insufficient_data",
                        "note": f"нет наблюдений метрики «{metric}» — what-if не выполняется",
                        "request": "подключите телеметрию или укажите наблюдаемую метрику"}
            new_v = ov["value"] * (1.0 + delta_pct / 100.0)
            limit = params.get("limit")
            res["param_scenario"] = {"metric": metric, "observed": ov["value"], "delta_pct": delta_pct,
                                     "projected": round(new_v, 4),
                                     "limit": limit,
                                     "limit_breached": (new_v > limit) if limit is not None else None}
            ok = True
        else:  # degradation
            from .eng6.rel import availability
            mtbf = float(params.get("mtbf_h", 8000.0)); mttr = float(params.get("mttr_h", 4.0))
            a0 = availability(mtbf, mttr)
            a1 = availability(mtbf * float(params.get("factor", 0.5)), mttr)
            res["degradation"] = {"mtbf_h": mtbf, "mttr_h": mttr, "availability_base": round(a0, 6),
                                  "factor": params.get("factor", 0.5), "availability_degraded": round(a1, 6),
                                  "delta": round(a1 - a0, 6)}
            ok = True

        sid = f"SCN-{int(time.time() * 1000) % 10 ** 9}-{os.urandom(2).hex().upper()}"
        record = {"id": sid, "object_id": object_id, "kind": kind, "params": params,
                  "base_observed": base["observed"], "result": res, "status": "done" if ok else res.get("forecast", {}).get("status", "error"),
                  "marked": "оценка (не факт)", "validated": 0, "created": time.time(), "created_by": by}
        if ok or res.get("forecast", {}).get("status") == "forecast":
            self.store.x("""INSERT INTO scenarios(id,object_id,kind,params,base_state,result,marked,validated,created,created_by)
                            VALUES(?,?,?,?,?,?,?,0,?,?)""",
                         (sid, object_id, kind, _json.dumps(params, ensure_ascii=False),
                          _json.dumps(base["observed"], ensure_ascii=False)[:4000],
                          _json.dumps(res, ensure_ascii=False)[:8000], record["marked"], record["created"], by))
        record["rule"] = ("сценарий не изменяет базовую линию и паспорт (DGT-002); "
                          "результат — оценка; DT-5 присваивается только после независимой проверки сценария (§4.10.2)")
        return record

    def list(self, object_id: Optional[str] = None) -> List[Dict]:
        import json as _json
        sql = "SELECT * FROM scenarios" + (" WHERE object_id=?" if object_id else "") + " ORDER BY created DESC LIMIT 200"
        rows = self.store.q(sql, (object_id,) if object_id else ())
        for r in rows:
            r["params"] = _json.loads(r.get("params") or "{}")
            r["result"] = _json.loads(r.get("result") or "{}")
        return rows

    def validate(self, scenario_id: str, validator: str, basis: str) -> Dict:
        """Фиксация независимой проверки сценария (Maker–Checker: валидирует роль с правом verify)."""
        rows = self.store.q("SELECT * FROM scenarios WHERE id=?", (scenario_id,))
        if not rows:
            return {"status": "error", "error": "сценарий не найден"}
        if not basis.strip():
            return {"status": "error", "error": "проверка требует основания (§12.1)"}
        self.store.x("UPDATE scenarios SET validated=1, validation_basis=? WHERE id=?", (basis, scenario_id))
        return {"status": "validated", "id": scenario_id, "validator": validator, "basis": basis,
                "note": "проверенный сценарий учитывается в критерии DT-5; статус результата остаётся «оценка»"}
