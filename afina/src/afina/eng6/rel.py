"""REL-01 надёжность: доступность, SPOF, деревья отказов, Марковские цепи (§4.5.3, §4.5.4)."""
from typing import Dict, List, Optional, Set
import math

VERSION = "1.0.0"


def availability(mtbf_h: float, mttr_h: float) -> float:
    """Коэффициент готовности K = MTBF/(MTBF+MTTR)."""
    if mtbf_h <= 0 or mttr_h < 0:
        raise ValueError("REL: MTBF>0, MTTR>=0")
    return mtbf_h / (mtbf_h + mttr_h)


def series_avail(avails: List[float]) -> float:
    return math.prod(avails)


def parallel_avail(avails: List[float]) -> float:
    return 1 - math.prod(1 - a for a in avails)


# ---------- SPOF: поиск точек единичного отказа в графе ----------
def find_spof(nodes: List[str], edges: List[tuple], services: Dict[str, List[str]]) -> Dict:
    """services: имя услуги → список узлов-источников; связность по рёбрам (физическая топология).

    SPOF-узел: удаление которого разрывает связность хотя бы одной пары
    (источник услуги — потребитель) или изолирует источник.
    Условный «потребитель» — узел 'CORE' при его наличии.
    """
    adj = {n: set() for n in nodes}
    for a, b in edges:
        adj[a].add(b); adj[b].add(a)

    def comps(exclude: Optional[str]) -> Dict[str, int]:
        seen: Dict[str, int] = {}; cid = 0
        for n in nodes:
            if n == exclude or n in seen: continue
            cid += 1; stack = [n]
            while stack:
                v = stack.pop()
                if v in seen or v == exclude: continue
                seen[v] = cid
                stack.extend(adj[v] - set(seen))
        return seen

    base = comps(None)
    spof = []
    for n in nodes:
        c = comps(n)
        broken = False
        for svc, srcs in services.items():
            ids = {c.get(s) for s in srcs if s != n}
            if len(srcs) == 1 and srcs[0] == n:
                broken = True; break
            if 'CORE' in c and len(ids - {None}) > 1:
                pass
            if None in ids or len({i for i in ids if i is not None}) != len([s for s in srcs if s != n]):
                broken = True; break
        if not broken and 'CORE' in base:
            core_id = base.get('CORE')
            for m in nodes:
                if m == n or m == 'CORE': continue
                if base.get(m) == core_id and c.get(m) != c.get('CORE'):
                    broken = True; break
        if broken:
            spof.append(n)
    return {"spof_nodes": spof, "count": len(spof),
            "note": "географическое соседство не создаёт технической связи: анализ по физической топологии"}


# ---------- Марковская цепь (стационарное распределение) ----------
def markov_steady(transition: List[List[float]], tol=1e-12, iters=100000) -> List[float]:
    """P·π = π методом итераций; контроль сходимости."""
    n = len(transition)
    pi = [1.0 / n] * n
    for it in range(iters):
        new = [sum(pi[i] * transition[i][j] for i in range(n)) for j in range(n)]
        diff = max(abs(new[j] - pi[j]) for j in range(n))
        pi = new
        if diff < tol:
            return {"pi": pi, "iterations": it + 1, "converged": True}
    return {"pi": pi, "iterations": iters, "converged": False,
            "warning": "сходимость не достигнута — результат маркирован как предварительный"}


# ---------- FMEA-каркас ----------
def fmea(failures: List[Dict]) -> List[Dict]:
    """failures: [{mode, effect, p(1-5), s(1-5), d(1-5)}] → RPN и ранжирование."""
    out = []
    for f in failures:
        rpn = f.get("p", 1) * f.get("s", 1) * f.get("d", 1)
        out.append({**f, "rpn": rpn})
    out.sort(key=lambda x: -x["rpn"])
    return out
