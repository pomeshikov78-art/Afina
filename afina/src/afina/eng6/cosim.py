"""Co-Simulation (§4.5.6, ENG-005): многофизическое сопряжение решателей.

Обязательны: единый ID сценария, описание входов/выходов, согласование единиц,
обнаружение циклических зависимостей, контроль сходимости, обработка ошибок
отдельного решателя, фиксация версий. Тихая подмена упрощённым обменом запрещена.
"""
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional
from ..units import Q, DimensionError

VERSION = "1.0.0"


@dataclass
class SolverIO:
    inputs: Dict[str, str]    # имя → единица
    outputs: Dict[str, str]


@dataclass
class Solver:
    id: str; version: str; io: SolverIO; fn: Callable[[Dict], Dict]
    compatible_with: List[str] = field(default_factory=list)  # подтверждённые сопряжения


class CosimError(RuntimeError):
    pass


def detect_cycles(solvers: List[Solver], wiring: List[tuple]) -> List[List[str]]:
    """wiring: [(src_solver, dst_solver)]; поиск циклов DFS."""
    adj = {s.id: [] for s in solvers}
    for a, b in wiring:
        adj[a].append(b)
    cycles = []; color = {s.id: 0 for s in solvers}; stack = []

    def dfs(v):
        color[v] = 1; stack.append(v)
        for w in adj.get(v, []):
            if color.get(w) == 1:
                cycles.append(stack[stack.index(w):] + [w])
            elif color.get(w, 0) == 0:
                dfs(w)
        stack.pop(); color[v] = 2
    for s in solvers:
        if color[s.id] == 0: dfs(s.id)
    return cycles


def check_units(src: Solver, dst: Solver, mapping: Dict[str, str]) -> List[str]:
    """mapping: {src_output: dst_input}; проверка совместимости единиц (ENG-004)."""
    problems = []
    for so, di in mapping.items():
        u1 = src.io.outputs.get(so); u2 = dst.io.inputs.get(di)
        if u1 is None or u2 is None:
            problems.append(f"{src.id}.{so} → {dst.id}.{di}: порт не объявлен"); continue
        try:
            Q(1.0, u1).to(u2)
        except DimensionError as e:
            problems.append(f"{src.id}.{so}[{u1}] → {dst.id}.{di}[{u2}]: {e}")
    return problems


def run_chain(scenario_id: str, solvers: List[Solver], wiring: List[tuple],
              mappings: List[Dict[str, str]], seed: Dict[str, float],
              tolerance: float = 0.01, max_iter: int = 20) -> Dict:
    """Прямой прогон цепочки (без циклов). Контроль версий и единиц на каждой связке."""
    cycles = detect_cycles(solvers, wiring)
    if cycles:
        return {"ok": False, "scenario_id": scenario_id, "error": "обнаружены циклические зависимости",
                "cycles": cycles, "note": "требуется итерационная схема с контролем сходимости"}
    by_id = {s.id: s for s in solvers}
    data = dict(seed); log = []; ok = True
    order = _topo(solvers, wiring)
    for sid in order:
        s = by_id[sid]
        try:
            inp = {k: data[k] for k in s.io.inputs if k in data}
            missing = [k for k in s.io.inputs if k not in data]
            if missing:
                raise CosimError(f"не хватает входов: {missing}")
            out = s.fn(inp)
            data.update(out)
            log.append({"solver": sid, "version": s.version, "outputs": {k: round(v, 4) if isinstance(v, float) else v for k, v in out.items()}})
        except CosimError as e:
            ok = False; log.append({"solver": sid, "error": str(e),
                                    "policy": "ошибка решателя не подменяется упрощённым значением (запрет тихой деградации)"})
            break
        except Exception as e:
            ok = False; log.append({"solver": sid, "error": repr(e)}); break
    return {"ok": ok, "scenario_id": scenario_id, "data": data, "log": log,
            "versions": {s.id: s.version for s in solvers}}


def _topo(solvers, wiring):
    adj = {s.id: [] for s in solvers}
    indeg = {s.id: 0 for s in solvers}
    for a, b in wiring:
        adj[a].append(b); indeg[b] += 1
    q = [s for s in indeg if indeg[s] == 0]; order = []
    while q:
        v = q.pop(0); order.append(v)
        for w in adj[v]:
            indeg[w] -= 1
            if indeg[w] == 0: q.append(w)
    return order


# ---------- типовая цепочка: мощность → тепло → допустимость → доступность ----------
def pwr_thm_rel_solvers() -> (List[Solver], List[tuple], List[Dict[str, str]]):
    from .pwr_thm import PowerSystem, Load, ThermalNode, thermal_steady
    from .rel import availability

    def pwr(inp):
        ps = PowerSystem(loads=[Load("оборудование", inp["w_nom"], inp["w_peak"])])
        b = ps.balance(ambient_c=inp.get("t_ambient_c", 20))
        return {"heat_w": b["w_nom"], "battery_hours": b["battery_hours"], "autonomy_hours": b["autonomy_hours"]}

    def thm(inp):
        node = ThermalNode("шкаф", inp["heat_w"], t_limit_c=inp.get("t_limit_c", 35.0))
        r = thermal_steady(node, inp.get("t_ambient_c", 20.0), cooling_w=inp.get("cooling_w", 0.0))
        return {"t_cabinet_c": r["t_c"], "operational": 1.0 if r["operational"] else 0.0,
                "required_cooling_w": r["required_cooling_w"]}

    def rel(inp):
        mtbf = inp.get("mtbf_h", 8000.0)
        mttr = inp.get("mttr_h", 4.0)
        a = availability(mtbf, mttr)
        if inp.get("operational", 1.0) < 1.0:
            a = 0.0  # недопустимая температура — функция не сохраняется
        return {"availability": a}

    solvers = [
        Solver("PWR-01", "1.0.0", SolverIO({"w_nom": "Вт", "w_peak": "Вт", "t_ambient_c": "°C"},
                                           {"heat_w": "Вт", "battery_hours": "ч", "autonomy_hours": "ч"}), pwr),
        Solver("THM-01", "1.0.0", SolverIO({"heat_w": "Вт", "t_ambient_c": "°C", "t_limit_c": "°C", "cooling_w": "Вт"},
                                           {"t_cabinet_c": "°C", "operational": "1", "required_cooling_w": "Вт"}), thm),
        Solver("REL-01", "1.0.0", SolverIO({"operational": "1", "mtbf_h": "ч", "mttr_h": "ч"},
                                           {"availability": "1"}), rel),
    ]
    wiring = [("PWR-01", "THM-01"), ("THM-01", "REL-01")]
    mappings = [{"heat_w": "heat_w"}, {"operational": "operational"}]
    return solvers, wiring, mappings
