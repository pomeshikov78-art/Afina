"""Управление конфигурацией (§4.8): изменения, анализ влияния (CFG-002),
инженерные правила вне кода (CFG-004), статусы изменения (CFG-003).

Граф зависимостей: объекты → расчёты → документы → испытания → требования.
Ключевое правило: изменение параметра НЕ подменяет исходные данные выполненного
расчёта — старый результат сохраняется со своей конфигурацией, новый получает свой ID.
"""
import json, time
from typing import Dict, List, Optional

VERSION = "1.0.0"

CHANGE_STATES = ["Предложено", "Внесено", "Проверено", "Утверждено"]


class DependencyGraph:
    """Типизированный граф зависимостей (вершины: object/calc/doc/test/requirement)."""

    def __init__(self):
        self.adj: Dict[str, List[str]] = {}
        self.kinds: Dict[str, str] = {}

    def add(self, a: str, b: str, ka="object", kb="object"):
        self.adj.setdefault(a, []).append(b)
        self.adj.setdefault(b, [])
        self.kinds.setdefault(a, ka); self.kinds.setdefault(b, kb)

    def impact(self, start: str) -> Dict:
        """BFS от изменяемого узла: все затронутые вершины с типами и путями."""
        seen = {start: None}
        queue = [start]
        while queue:
            v = queue.pop(0)
            for w in self.adj.get(v, []):
                if w not in seen:
                    seen[w] = v
                    queue.append(w)
        affected = []
        for node, parent in seen.items():
            if node == start:
                continue
            path = [node]
            p = parent
            while p is not None:
                path.append(p)
                p = seen.get(p)
            affected.append({"id": node, "kind": self.kinds.get(node, "?"),
                             "path": " ← ".join(reversed(path))})
        by_kind = {}
        for a in affected:
            by_kind.setdefault(a["kind"], []).append(a["id"])
        return {"start": start, "affected": affected, "by_kind": by_kind,
                "counts": {k: len(v) for k, v in by_kind.items()}}


def build_graph_from_store(store, project: str = None) -> DependencyGraph:
    """Сборка графа из хранилища: объекты → расчёты (по упоминанию ID) → замечания/требования."""
    g = DependencyGraph()
    objs = store.q("SELECT * FROM objects")
    for o in objs:
        g.add(o["id"], o["id"], "object", "object")
        g.kinds[o["id"]] = "object"
    for c in store.q("SELECT * FROM calcs"):
        payload = c.get("payload") or ""
        for o in objs:
            if o["id"] in payload:
                g.add(o["id"], c["run_id"], "object", "calc")
    for z in store.q("SELECT * FROM zam"):
        req = z.get("req") or ""
        g.add(z["id"], req, "zam", "requirement")
        for o in objs:
            if o["id"] in (z.get("title") or ""):
                g.add(o["id"], z["id"], "object", "zam")
    return g


# ---------- инженерные правила ВНЕ кода (CFG-004) ----------
DEFAULT_RULES = [
    {"id": "RULE-014", "name": "СЗЗ ≥ нормативной границы", "class": "блокирующее", "version": 3,
     "basis": "санитарные правила по ЭМИ", "author": "инженер-расчётчик", "since": "2026-03-01",
     "check": {"type": ">=", "field": "szz_margin_m", "ref_field": "szz_required_m"}},
    {"id": "RULE-022", "name": "Перекрытие зоны Френеля F1 ≤ 40 %", "class": "критическое", "version": 2,
     "basis": "ITU-R P.530", "author": "инженер-расчётчик", "since": "2026-02-11",
     "check": {"type": "<=", "field": "f1_block_pct", "value": 40}},
    {"id": "RULE-031", "name": "Падение напряжения PoE ≤ 5 %", "class": "предупреждение", "version": 1,
     "basis": "внутренний стандарт", "author": "сетевой архитектор", "since": "2026-04-02",
     "check": {"type": "<=", "field": "poe_drop_pct", "value": 5}},
    {"id": "RULE-047", "name": "Резервирование каналов N+1", "class": "блокирующее", "version": 4,
     "basis": "требование заказчика", "author": "руководитель программы", "since": "2026-01-20",
     "check": {"type": "flag_true", "field": "redundancy_ok"}},
]


def eval_rules(ctx: Dict, rules: List[Dict] = None) -> Dict:
    """Исполнение декларативных правил (данные, не код). Изменение правила не
    переписывает задним числом результаты утверждённых проектов."""
    rules = rules or DEFAULT_RULES
    violations, warnings, passed = [], [], []
    for r in rules:
        c = r["check"]
        v = ctx.get(c["field"])
        bad = None
        if v is None:
            bad = f"поле «{c['field']}» отсутствует — правило не может быть проверено (результат НЕ выдаётся как достоверный)"
        elif c["type"] == ">=":
            ref = ctx.get(c.get("ref_field", ""), c.get("value"))
            bad = None if (ref is not None and v >= ref) else f"{v} < {ref}"
        elif c["type"] == "<=":
            bad = None if v <= c["value"] else f"{v} > {c['value']}"
        elif c["type"] == "flag_true":
            bad = None if v else "флаг не установлен"
        item = {"rule": r["id"], "name": r["name"], "class": r["class"], "version": r["version"], "detail": bad}
        if bad is None:
            passed.append(item)
        elif r["class"] in ("блокирующее", "критическое"):
            violations.append(item)
        else:
            warnings.append(item)
    return {"passed": passed, "violations": violations, "warnings": warnings,
            "overall": "FAIL" if violations else ("WARN" if warnings else "PASS"),
            "rules_version": {r["id"]: r["version"] for r in rules}}
