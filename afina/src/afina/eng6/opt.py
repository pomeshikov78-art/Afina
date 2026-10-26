"""OPT-01 многокритериальная оптимизация с жёсткими ограничениями (§4.5.4, ENG-006).

Вариант, нарушающий обязательное ограничение, НЕ может быть «лучшим»:
сначала фильтрация до допустимого множества, затем Парето-ранжирование.
LCC-01: TCO с чувствительностью.
"""
from typing import List, Dict, Callable, Optional
from dataclasses import dataclass

VERSION = "1.0.0"


@dataclass
class Criterion:
    key: str
    name: str
    direction: str          # 'min' | 'max'
    weight: float = 1.0
    hard_min: Optional[float] = None   # жёсткое ограничение
    hard_max: Optional[float] = None


def feasible(variants: List[Dict], criteria: List[Criterion],
             constraints: List[Callable[[Dict], Optional[str]]] = None) -> Dict:
    """Разделение: допустимые / отклонённые (с причиной — ENG-006, 3DM-003)."""
    ok, rejected = [], []
    for v in variants:
        reason = None
        for c in criteria:
            val = v.get(c.key)
            if val is None:
                reason = f"{c.key}: значение «не определено» — вариант не оценивается"; break
            if c.hard_min is not None and val < c.hard_min:
                reason = f"{c.name}: {val} < жёсткий минимум {c.hard_min}"; break
            if c.hard_max is not None and val > c.hard_max:
                reason = f"{c.name}: {val} > жёсткий максимум {c.hard_max}"; break
        if reason is None and constraints:
            for fn in constraints:
                reason = fn(v)
                if reason: break
        (rejected.append({"variant": v, "reason": reason}) if reason else ok.append(v))
    return {"feasible": ok, "rejected": rejected}


def pareto_front(variants: List[Dict], criteria: List[Criterion]) -> List[str]:
    """Недоминируемое множество по всем критериям."""
    front = []
    for a in variants:
        dominated = False
        for b in variants:
            if a is b: continue
            better_eq = all(
                (b[c.key] <= a[c.key]) if c.direction == "min" else (b[c.key] >= a[c.key])
                for c in criteria)
            strictly = any(
                (b[c.key] < a[c.key]) if c.direction == "min" else (b[c.key] > a[c.key])
                for c in criteria)
            if better_eq and strictly:
                dominated = True; break
        if not dominated:
            front.append(a.get("id", str(a)))
    return front


def rank_weighted(variants: List[Dict], criteria: List[Criterion]) -> List[Dict]:
    """Взвешенное ранжирование допустимых вариантов (нормировка min-max)."""
    if not variants: return []
    norm = {}
    for c in criteria:
        vals = [v[c.key] for v in variants]
        lo, hi = min(vals), max(vals)
        # фиксация c/lo/hi значениями по умолчанию — защита от late-binding замыканий
        norm[c.key] = (lambda x, c=c, lo=lo, hi=hi: 0.5 if hi == lo else ((hi - x) / (hi - lo) if c.direction == "min" else (x - lo) / (hi - lo)))
    wsum = sum(c.weight for c in criteria) or 1.0
    out = []
    for v in variants:
        score = sum(c.weight * norm[c.key](v[c.key]) for c in criteria) / wsum
        out.append({**v, "score": round(score, 4)})
    out.sort(key=lambda x: -x["score"])
    return out


def lcc(capex: float, years: int, opex_per_year: float, discount: float = 0.08,
        disposal: float = 0.0) -> Dict:
    """LCC-01: TCO + чувствительность к opex ±20 %."""
    pv_opex = sum(opex_per_year / (1 + discount) ** t for t in range(1, years + 1))
    tco = capex + pv_opex - disposal / (1 + discount) ** years
    tco_hi = capex + sum(opex_per_year * 1.2 / (1 + discount) ** t for t in range(1, years + 1)) - disposal / (1 + discount) ** years
    tco_lo = capex + sum(opex_per_year * 0.8 / (1 + discount) ** t for t in range(1, years + 1)) - disposal / (1 + discount) ** years
    return {"tco": round(tco, 1), "tco_sensitivity": {"minus20_opex": round(tco_lo, 1), "plus20_opex": round(tco_hi, 1)},
            "years": years, "discount": discount}


# ---------- декларативные ограничения (G4.2): данные, а не код ----------
def declarative_constraints(specs: List[Dict]) -> List[Callable[[Dict], Optional[str]]]:
    """Преобразование декларативных спецификаций ограничений в проверяющие функции.

    Поддержаны: subset_of, forbidden_value, all_required, numeric_bound.
    Причина отказа всегда человекочитаема (ENG-006, 3DM-003)."""
    fns = []
    for sp in specs or []:
        t = sp.get("type")
        if t == "subset_of":
            f = sp["field"]; vals = set(sp["values"])
            fns.append(lambda v, f=f, vals=vals: None if v.get(f) in vals
                       else f"{f}=«{v.get(f)}» вне допустимого множества {sorted(vals)}")
        elif t == "forbidden_value":
            f = sp["field"]; bad = sp["value"]
            fns.append(lambda v, f=f, bad=bad: f"{f}=«{bad}» запрещено" if v.get(f) == bad else None)
        elif t == "all_required":
            fs = list(sp["fields"])
            fns.append(lambda v, fs=fs: None if all(v.get(x) is not None for x in fs)
                       else f"не заполнены обязательные поля: {[x for x in fs if v.get(x) is None]}")
        elif t == "numeric_bound":
            f = sp["field"]; lo = sp.get("min"); hi = sp.get("max")
            def _nb(v, f=f, lo=lo, hi=hi):
                x = v.get(f)
                if x is None: return f"{f}: значение не определено"
                if lo is not None and x < lo: return f"{f}={x} < минимума {lo}"
                if hi is not None and x > hi: return f"{f}={x} > максимума {hi}"
                return None
            fns.append(_nb)
        else:
            raise ValueError(f"тип ограничения «{t}» не поддержан — запуск отклоняется (тихая подмена запрещена)")
    return fns


def optimize(variants: List[Dict], criteria: List[Dict], constraints: List[Dict] = None) -> Dict:
    """OPT-01 полный цикл (§4.5.4): допустимое множество → Парето → взвешенное ранжирование.

    Вариант, нарушающий обязательное ограничение, НЕ может быть «лучшим» (ENG-006):
    отклонённые возвращаются отдельно, с причиной."""
    crits = [Criterion(**{k: v for k, v in c.items() if k in Criterion.__dataclass_fields__}) for c in criteria]
    fns = declarative_constraints(constraints)
    sep = feasible(variants, crits, fns)
    ok = sep["feasible"]
    ranked = rank_weighted(ok, crits)
    front = pareto_front(ok, crits) if ok else []
    best = ranked[0] if ranked else None
    if best is not None and best.get("id") in front:
        note = "лучший по взвешенной оценке входит в Парето-фронт"
    elif best is not None:
        note = "лучший по взвешенной оценке НЕ входит в Парето-фронт — решение зависит от весов; показано оба результата"
    else:
        note = "допустимых вариантов нет: ни один не может быть признан лучшим (ENG-006)"
    return {"feasible_count": len(ok), "rejected_count": len(sep["rejected"]),
            "rejected": sep["rejected"], "pareto_front": front, "ranked": ranked,
            "best": best, "note": note,
            "rule": "сначала фильтрация по жёстким ограничениям, затем ранжирование; нарушение ограничения не компенсируется высоким баллом"}
