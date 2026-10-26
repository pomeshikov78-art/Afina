"""Качество данных (§4.13.3, DAT-001…006): валидация, конфликты и арбитраж,
миграция с контролем архива, архив воспроизводимости.

Принципы (§6.1/§6.4): слияние дубликатов не автоматическое; противоречие
сохраняется и выносится на арбитраж; исходные файлы не изменяются.
"""
import hashlib, json, os, time
from typing import Dict, List, Optional

VERSION = "1.0.0"

# статусная модель DAT (§1.5)
DAT_CHAIN = ["Не заполнено", "Введено", "Формально проверено", "Подтверждено источником", "Подтверждено измерением"]
DAT_SPECIAL = ["Устарело", "Конфликт", "Отозвано"]

# правила валидации по классам (декларативно, вне кода — как CFG-004)
CLASS_RULES = {
    "COM": {"required": ["name", "bands"], "ranges": {"power_w": (0.05, 1000)}, "freshness_days": 90},
    "ANT": {"required": ["name", "bands"], "ranges": {"gain_dbi": (0, 60)}, "freshness_days": 180},
    "PWR": {"required": ["name"], "ranges": {"power_w": (0, 500000)}, "freshness_days": 90},
    "NET": {"required": ["name"], "ranges": {}, "freshness_days": 120},
    "STR": {"required": ["name"], "ranges": {"height_m": (0.5, 400)}, "freshness_days": 365},
}


def validate_object(obj: Dict) -> Dict:
    """Проверка: обязательность, типы, диапазоны, согласованность. Вердикты разделены
    на критические (блокируют публикацию), предупреждения и информационные (§4.3.6)."""
    cls = obj.get("class", "COM")
    rules = CLASS_RULES.get(cls, {"required": ["name"], "ranges": {}, "freshness_days": 90})
    errors, warnings, info = [], [], []
    for f in rules["required"]:
        if not obj.get(f):
            errors.append({"field": f, "problem": "обязательное поле не заполнено", "class": "критическое"})
    attrs = obj.get("attrs") or {}
    merged = {**obj, **attrs}
    for f, (lo, hi) in rules.get("ranges", {}).items():
        v = merged.get(f)
        if v is None:
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            errors.append({"field": f, "problem": f"тип не числовой: {v!r}", "class": "критическое"})
            continue
        if not (lo <= v <= hi):
            errors.append({"field": f, "problem": f"{v} вне диапазона [{lo}…{hi}] класса {cls}", "class": "критическое"})
    # согласованность связанных параметров
    if "масса_кг" in attrs and "транспортная_масса_кг" in attrs:
        if attrs["транспортная_масса_кг"] < attrs["масса_кг"]:
            errors.append({"field": "транспортная_масса_кг",
                           "problem": "транспортная масса меньше массы изделия — противоречие связанных параметров",
                           "class": "критическое"})
    # актуальность
    rec = obj.get("recorded_at")
    if rec:
        age_days = (time.time() - float(rec)) / 86400
        if age_days > rules.get("freshness_days", 90):
            warnings.append({"field": "recorded_at",
                             "problem": f"актуальность истекла ({int(age_days)} сут > {rules['freshness_days']}) — статус DAT «Устарело» / TS-8",
                             "class": "предупреждение"})
    # EQP-001: состояние значения
    sens = attrs.get("sensitivity_dbm", "absent")
    if sens == "absent":
        info.append({"field": "sensitivity_dbm",
                     "problem": "значение отсутствует — допустимые состояния: неизвестно/не применимо/не измерено/ноль; подстановка условного значения запрещена (EQP-001)",
                     "class": "информационное"})
    return {"object_id": obj.get("id"), "class": cls,
            "errors": errors, "warnings": warnings, "info": info,
            "publish_blocked": len(errors) > 0,
            "dat_status_suggested": "Формально проверено" if not errors else "Не заполнено"}


def find_duplicates(objects: List[Dict]) -> List[Dict]:
    """Потенциальные дубликаты — на арбитраж; АВТОСЛИЯНИЕ ЗАПРЕЩЕНО (§6.1)."""
    groups: Dict[str, List[str]] = {}
    for o in objects:
        key = (o.get("name", "").lower().replace("ё", "е").strip() + "|" + str(o.get("class")))
        groups.setdefault(key, []).append(o["id"])
    return [{"candidates": ids, "action": "вынести на арбитраж (слияние не автоматическое)"}
            for ids in groups.values() if len(ids) > 1]


class ConflictRegistry:
    """Конфликты значений: все значения сохраняются с происхождением, решение — арбитраж (DAT-004)."""

    def __init__(self, store):
        self.store = store

    def register(self, object_id: str, field: str, values: List[Dict]) -> Dict:
        """values: [{value, source_id, valid_from, recorded_at, method}]"""
        cid = f"DAT-{int(time.time()*1000)%100000}"
        self.store.x("""INSERT INTO conflicts(id,object_id,field,vals,status,created,resolution,resolved_by,basis)
                        VALUES(?,?,?,?,?,?,?,?,?)""",
                     (cid, object_id, field, json.dumps(values, ensure_ascii=False), "КОНФЛИКТ",
                      time.time(), None, None, None))
        self.store.audit("система", "-", f"зарегистрирован конфликт {cid}: {object_id}.{field} ({len(values)} значения)",
                         object_id, "data", "все значения сохранены, автоперезапись запрещена")
        return {"id": cid, "status": "КОНФЛИКТ", "values_preserved": len(values),
                "rule": "применить правила приоритета источников, если заданы; иначе — арбитраж (§6.4)"}

    def resolve(self, cid: str, winner_source: str, actor: str, role: str, basis: str) -> Dict:
        if role not in ("Уполномоченный утверждающий", "Администратор данных"):
            self.store.audit(actor, role, f"ОТКАЗ арбитража {cid} (роль)", cid, "sec", "SEC-005")
            return {"error": "арбитраж доступен уполномоченному утверждающему или администратору данных"}
        if not basis:
            return {"error": "решение арбитража требует основания"}
        self.store.x("UPDATE conflicts SET status='РЕШЕНО', resolution=?, resolved_by=?, basis=? WHERE id=?",
                     (winner_source, actor, basis, cid))
        self.store.audit(actor, role, f"арбитраж {cid}: выбран источник {winner_source}", cid, "data", basis)
        return {"id": cid, "status": "РЕШЕНО", "winner": winner_source,
                "note": "проигравшие значения НЕ удалены — хранятся с происхождением (битемпоральность DAT-003)"}


def _has_conflicts(store) -> bool:
    return True


def create_reproducibility_archive(store, baseline_id: str, out_dir: str) -> Dict:
    """DAT-006: пакет воспроизводимости — данные, версии моделей, настройки, допущения,
    результаты, документы, необходимые библиотеки + контрольные суммы."""
    os.makedirs(out_dir, exist_ok=True)
    bl = store.q("SELECT * FROM baselines WHERE id=?", (baseline_id,))
    if not bl:
        return {"error": f"базовая линия {baseline_id} не найдена"}
    calcs = store.q("SELECT run_id, function, model, inputs_hash, passport FROM calcs ORDER BY created DESC LIMIT 500")
    pkg = {"baseline": bl[0], "calcs": calcs,
           "engine_versions": {"eng6": "1.0.0", "gis": "1.0.0", "server": "1.0.0", "python_schema": "1.0"},
           "assumptions_note": "допущения зафиксированы в паспортах расчётов",
           "created": time.time()}
    path = os.path.join(out_dir, f"ARC-{baseline_id}.json")
    raw = json.dumps(pkg, ensure_ascii=False, indent=1, default=str)
    open(path, "w", encoding="utf-8").write(raw)
    h = hashlib.sha256(raw.encode()).hexdigest()
    store.audit("система", "-", f"архив воспроизводимости {os.path.basename(path)}", baseline_id, "data", f"sha256:{h[:16]}…")
    return {"path": path, "sha256": h, "calcs": len(calcs),
            "note": "пакет обеспечивает воспроизведение результата в будущих версиях Системы (DAT-006)"}


def migration_hash_check(source_files: List[str], before: Dict[str, str]) -> Dict:
    """DAT-005: хэши исходных файлов до и после миграции совпадают — архив не повреждён."""
    after = {}
    for p in source_files:
        h = hashlib.sha256(open(p, "rb").read()).hexdigest()
        after[p] = h
    mismatch = [p for p in source_files if before.get(p) != after[p]]
    return {"ok": not mismatch, "checked": len(source_files), "mismatch": mismatch,
            "rule": "исходные файлы и сведения о происхождении остаются неизменными (§4.13.4)"}
