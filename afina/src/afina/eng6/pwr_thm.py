"""PWR-01 электропитание и THM-01 тепловой режим (§4.5.4).

PWR: баланс, пики, резерв, падение напряжения, автономность.
THM: сосредоточенная RC-модель шкафа/помещения; на выходе — температура и
допустимость работы (вход для Co-Simulation: мощность → тепло → доступность).
"""
from dataclasses import dataclass, field
from typing import List, Optional
import math

VERSION = "1.0.0"


# ---------- PWR-01 ----------
@dataclass
class Load:
    id: str; w_nom: float; w_peak: float; critical: bool = True; voltage_dc: bool = False


@dataclass
class PowerSystem:
    loads: List[Load]
    mains_w: float = 10000.0
    ups_w: float = 10000.0
    ups_battery_wh: float = 20000.0
    ups_efficiency: float = 0.94
    gen_w: float = 30000.0
    gen_fuel_hours: float = 8.0
    gen_start_reliable_temp_c: Optional[float] = -10.0  # ниже — пуск не гарантирован
    cable_r_ohm: float = 0.05
    cable_v_nom: float = 220.0

    def balance(self, ambient_c: float = 20.0) -> dict:
        w_nom = sum(l.w_nom for l in self.loads)
        w_peak = sum(l.w_peak for l in self.loads)
        i_nom = w_nom / self.cable_v_nom
        v_drop = 2 * self.cable_r_ohm * i_nom
        batt_hours = self.ups_battery_wh * self.ups_efficiency / max(w_nom, 1e-9) / 3600 * 3600 / 3600
        batt_hours = self.ups_battery_wh * self.ups_efficiency / max(w_nom, 1e-9)
        gen_ok = (self.gen_start_reliable_temp_c is None or ambient_c >= self.gen_start_reliable_temp_c)
        autonomy_h = batt_hours + (self.gen_fuel_hours if gen_ok else 0.0)
        return {
            "w_nom": round(w_nom, 1), "w_peak": round(w_peak, 1),
            "mains_headroom_pct": round(100 * (1 - w_peak / self.mains_w), 1),
            "ups_load_pct": round(100 * w_nom / self.ups_w, 1),
            "v_drop_v": round(v_drop, 2),
            "v_drop_pct": round(100 * v_drop / self.cable_v_nom, 2),
            "battery_hours": round(batt_hours, 2),
            "generator_start_guaranteed": gen_ok,
            "autonomy_hours": round(autonomy_h, 2),
            "autonomy_note": None if gen_ok else f"при {ambient_c} °C пуск АД не гарантирован — автономность = только АКБ",
        }


# ---------- THM-01 ----------
@dataclass
class ThermalNode:
    id: str
    power_w: float                 # тепловыделение
    c_j_per_k: float = 2.2e5       # теплоёмкость узла (шкаф с оборудованием)
    r_th_k_per_w: float = 0.02     # термосопротивление узел→среда
    t_limit_c: float = 35.0


def thermal_steady(node: ThermalNode, t_ambient_c: float, cooling_w: float = 0.0) -> dict:
    """Стационарная температура: T = Ta + (P − Q_cool)·Rth."""
    net = max(node.power_w - cooling_w, 0.0)
    t = t_ambient_c + net * node.r_th_k_per_w
    return {"t_c": round(t, 2), "limit_c": node.t_limit_c,
            "operational": t <= node.t_limit_c,
            "required_cooling_w": round(max(net - (node.t_limit_c - t_ambient_c) / node.r_th_k_per_w, 0), 1),
            "margin_c": round(node.t_limit_c - t, 2)}


def thermal_transient(node: ThermalNode, t_ambient_c: float, t0_c: float,
                      t_end_s: float, dt_s: float = 60.0, cooling_w: float = 0.0) -> dict:
    """Переходный процесс (Эйлер): сходимость контролируется (MAT-03 — двойное измельчение)."""
    t = t0_c
    tau = node.c_j_per_k * node.r_th_k_per_w
    steps = int(t_end_s / dt_s)
    series = []
    for i in range(steps + 1):
        series.append(round(t, 3))
        t = t + dt_s * ((node.power_w - cooling_w) / node.c_j_per_k - (t - t_ambient_c) / tau)
    return {"tau_s": round(tau, 1), "t_final_c": round(t, 2),
            "time_to_limit_s": next((i * dt_s for i, v in enumerate(series) if v > node.t_limit_c), None),
            "series_head": series[:5], "operational_at_end": t <= node.t_limit_c}
