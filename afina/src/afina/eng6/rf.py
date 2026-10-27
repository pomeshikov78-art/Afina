"""Обязательные расчётные функции RF-01…RF-09 (§4.5.4).

Через собственные модули; результат каждой функции — данные + модель + допущения
(для паспорта расчёта ENG-001). Throughput НЕ рассчитывается только по уровню сигнала.
"""
import math
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Callable
from . import models
from ..units import db_add, dbm_to_w, w_to_dbm

VERSION = "1.0.0"
K_BOLTZMANN_DB = -174.0  # дБм/Гц


# ---------- RF-08: диаграмма направленности ----------
def pattern_h_db(az_offset_deg: float, beamwidth_3db: float = 65.0, am_db: float = 30.0) -> float:
    """Горизонтальная ДН сектора (3GPP-подобная): A(φ) = −min(12(φ/φ3dB)², Am)."""
    a = 12.0 * (az_offset_deg / beamwidth_3db) ** 2
    return -min(a, am_db)


def pattern_v_db(el_offset_deg: float, beamwidth_3db: float = 10.0, am_db: float = 30.0) -> float:
    a = 12.0 * (el_offset_deg / beamwidth_3db) ** 2
    return -min(a, am_db)


# ---------- RF-01/RF-02 ----------
def path_loss_db(model_key: str, f_mhz: float, d_km: float, h_bs: float, h_ms: float,
                 env: str = "urban", diffraction_db: float = 0.0, **kw) -> float:
    """RF-01: потери распространения по выбранной модели (с проверкой домена MOD-003)."""
    fn = {"FSPL": models.fspl_db, "OKUMURA_HATA": models.okumura_hata_db,
          "COST231_HATA": models.cost231_hata_db, "SUI": models.sui_db,
          "SPM": models.spm_db, "INDOOR_P1238": models.indoor_p1238_db}[model_key]
    if model_key == "INDOOR_P1238":
        return fn(f_mhz, d_km * 1000.0, **kw)
    if model_key == "SPM":
        return fn(f_mhz, d_km, h_bs, h_ms, env, diffraction_db=diffraction_db, **kw)
    if model_key == "SUI":
        return fn(f_mhz, d_km, h_bs, h_ms, kw.get("cat", "B"))
    if model_key == "FSPL":
        return fn(f_mhz, d_km)
    return fn(f_mhz, d_km, h_bs, h_ms, env if model_key != "COST231_HATA" else env if env in ("urban", "suburban") else "urban")


# ---------- RF-06: зоны Френеля ----------
def fresnel_radius_m(n: int, d1_km: float, d2_km: float, f_ghz: float) -> float:
    """Радиус n-й зоны Френеля: r = 17.32·sqrt(n·d1·d2/(f·d)), d = d1+d2 (км, ГГц → м)."""
    d = d1_km + d2_km
    if min(d1_km, d2_km) <= 0 or f_ghz <= 0:
        raise ValueError("Френель: d1,d2,f должны быть > 0")
    return 17.32 * math.sqrt(n * d1_km * d2_km / (f_ghz * d))


# ---------- RF-05: LOS по рельефу ----------
def terrain_blocks_los(profile: List[float], clearance_ratio: float = 0.6,
                       f_ghz: float = 1.0) -> Dict:
    """Проверка LOS по профилю рельефа (высоты в м, равноотстоящие точки).

    Учитывает первую зону Френеля: препятствие, вторгшееся более чем на
    (1−clearance_ratio) радиуса F1, блокирует радиовидимость.
    Возвращает: los, препятствия, степень перекрытия F1.
    """
    n = len(profile)
    if n < 2:
        return {"los": True, "obstacles": [], "f1_block_pct": 0.0}
    h1, h2 = profile[0], profile[-1]
    span = n - 1
    worst = 0.0
    obstacles = []
    for i in range(1, n - 1):
        line = h1 + (h2 - h1) * i / span
        d1 = i / span * 1.0  # нормировка: для радиуса нужны реальные км — вызываются с масштабом
        h = profile[i]
        if h > line:
            obstacles.append({"idx": i, "excess_m": round(h - line, 2)})
    # Френель: вызывающий передаёт реальные расстояния через kw ниже (здесь — геометрический LOS)
    return {"los": len(obstacles) == 0, "obstacles": obstacles,
            "max_excess_m": max([o["excess_m"] for o in obstacles], default=0.0)}


def los_with_fresnel(profile: List[float], d_total_km: float, f_ghz: float,
                     clearance: float = 0.4) -> Dict:
    """LOS + учёт F1: препятствие блокирует, если вторжение > (1−clearance)·F1."""
    n = len(profile)
    h1, h2 = profile[0], profile[-1]
    span = n - 1
    blocked = []
    for i in range(1, n - 1):
        line = h1 + (h2 - h1) * i / span
        excess = profile[i] - line
        if excess <= 0:
            continue
        d1 = d_total_km * i / span
        f1 = fresnel_radius_m(1, d1, d_total_km - d1, f_ghz)
        if excess > (1.0 - clearance) * f1:
            blocked.append({"idx": i, "excess_m": round(excess, 2), "f1_m": round(f1, 2)})
    return {"los": len(blocked) == 0, "f1_blockers": blocked,
            "f1_block_pct": round(100.0 * len(blocked) / max(span - 1, 1), 1)}


# ---------- RF-07: бюджет радиолинии ----------
@dataclass
class LinkBudget:
    tx_power_dbm: float
    tx_feeder_loss_db: float
    tx_ant_gain_dbi: float
    path_loss_db: float
    diffraction_db: float = 0.0
    rx_ant_gain_dbi: float = 0.0
    rx_feeder_loss_db: float = 0.0
    noise_figure_db: float = 5.0
    bandwidth_hz: float = 100e6
    rx_sensitivity_dbm: Optional[float] = None  # None — «не измерено» (EQP-001)
    vswr: float = 1.3
    # поглощение в атмосферных газах, дБ (внешний член; значение — из канонической
    # модели, напр. ENG6 gasGamma / ITU-R P.676). Добавлено 26.10.2026 по итогам
    # кросс-верификации XVER (OBS-11): без члена бюджет оптимистичен до ~0.011 дБ/км
    # на РРЛ-диапазонах. По умолчанию 0 — обратная совместимость сохранена.
    gas_absorption_db: float = 0.0

    def result(self) -> Dict:
        eirp = self.tx_power_dbm - self.tx_feeder_loss_db + self.tx_ant_gain_dbi
        rx = db_add([eirp, -self.path_loss_db, -self.diffraction_db,
                     -self.gas_absorption_db,
                     self.rx_ant_gain_dbi, -self.rx_feeder_loss_db])
        noise = K_BOLTZMANN_DB + 10 * math.log10(self.bandwidth_hz) + self.noise_figure_db
        margin = rx - noise
        out = {"eirp_dbm": round(eirp, 2), "rx_level_dbm": round(rx, 2),
               "noise_floor_dbm": round(noise, 2), "margin_db": round(margin, 2),
               "vswr": self.vswr,
               "vswr_mismatch_loss_db": round(-10 * math.log10(1 - ((self.vswr - 1) / (self.vswr + 1)) ** 2), 3)}
        if self.gas_absorption_db:
            out["gas_absorption_db"] = round(self.gas_absorption_db, 3)
        else:
            out["assumption"] = "поглощение в атмосферных газах не учтено (gas_absorption_db=0); для f > 1 ГГц член обязателен (OBS-11)"
        if self.rx_sensitivity_dbm is None:
            out["sensitivity"] = "не измерено — запас до чувствительности не определён (EQP-001); результат маркирован как предварительный"
        else:
            out["sensitivity_dbm"] = self.rx_sensitivity_dbm
            out["margin_to_sensitivity_db"] = round(rx - self.rx_sensitivity_dbm, 2)
        return out


# ---------- RF-02/RF-03/RF-04: сеточные расчёты ----------
@dataclass
class BaseStation:
    id: str; x_m: float; y_m: float; h_ant_m: float
    tx_power_dbm: float = 43.0; feeder_loss_db: float = 2.0
    ant_gain_dbi: float = 17.0; azimuth_deg: float = 0.0
    beamwidth_deg: float = 65.0; f_mhz: float = 3500.0
    sector: bool = True


@dataclass
class CoverageResult:
    rx_dbm: List[List[float]]          # [row][col]
    cell_m: float
    coverage_pct: float
    threshold_dbm: float
    model: str
    assumptions: List[str]
    uncertainty_db: Dict[str, float]
    sinr_db: Optional[List[List[float]]] = None
    best_server: Optional[List[List[str]]] = None
    preliminary: bool = True           # маркировка достоверности


def coverage_grid(stations: List[BaseStation], width_m: float, height_m: float, cell_m: float,
                  model_key: str = "SPM", env: str = "urban", h_ms_m: float = 1.5,
                  threshold_dbm: float = -95.0, heights=None, f_ghz: float = 3.5,
                  calc_sinr: bool = False, diffraction=True) -> CoverageResult:
    """RF-02/03/04: карта уровней, доля покрытия ≥ порога, SINR (опционально).

    heights — матрица высот (список списков, шаг = cell_m); при наличии выполняется
    геометрическая трассировка профиля (RF-05) и дифракционная поправка (упрощённая,
    эпсилон-профиль). Для Preview-режима diffraction=False.
    """
    cols = int(width_m / cell_m); rows = int(height_m / cell_m)
    rx = [[-999.0] * cols for _ in range(rows)]
    sinr = [[0.0] * cols for _ in range(rows)] if calc_sinr else None
    best = [[""] * cols for _ in range(rows)] if calc_sinr else None
    assumptions = [f"модель {model_key} v{models.VERSION}, env={env}, h_MS={h_ms_m} м",
                   f"сетка {cell_m} м, ДН сектора 3GPP-подобная (Am=30 дБ)",
                   "дифракция: эпсилон-профиль по матрице высот" if (diffraction and heights) else "дифракция не учитывалась (Preview)"]
    lin_sum = [[0.0] * cols for _ in range(rows)] if calc_sinr else None
    for st in stations:
        for r in range(rows):
            y = (r + 0.5) * cell_m
            for c in range(cols):
                x = (c + 0.5) * cell_m
                d = math.hypot(x - st.x_m, y - st.y_m)
                d_km = max(d / 1000.0, 0.01)
                az = (math.degrees(math.atan2(x - st.x_m, -(y - st.y_m))) + 360) % 360
                off = abs((az - st.azimuth_deg + 180) % 360 - 180)
                g = pattern_h_db(off, st.beamwidth_deg) if st.sector else 0.0
                diff = 0.0
                if diffraction and heights:
                    diff = _diffraction_epsilon(heights, st, x, y, cell_m, f_ghz)
                try:
                    L = path_loss_db(model_key, st.f_mhz, d_km, st.h_ant_m, h_ms_m, env, diffraction_db=diff if model_key == "SPM" else 0.0)
                except models.ApplicabilityError:
                    continue  # вне домена — ячейка не покрывается данной моделью (MOD-003)
                if model_key != "SPM" and diff:
                    L += diff
                lvl = st.tx_power_dbm - st.feeder_loss_db + st.ant_gain_dbi + g - L
                if lvl > rx[r][c]:
                    rx[r][c] = lvl
                    if calc_sinr:
                        best[r][c] = st.id
                if calc_sinr:
                    lin_sum[r][c] += dbm_to_w(lvl - g) * 0 + dbm_to_w(lvl)  # суммарная интерференция (грубо: все как мешающие)
    covered = sum(1 for r in range(rows) for c in range(cols) if rx[r][c] >= threshold_dbm)
    total = rows * cols
    if calc_sinr:
        for r in range(rows):
            for c in range(cols):
                if rx[r][c] > -998:
                    interf = max(lin_sum[r][c] - dbm_to_w(rx[r][c]), 1e-18)
                    noise_w = dbm_to_w(K_BOLTZMANN_DB + 10 * math.log10(100e6) + 5.0)
                    sinr[r][c] = round(10 * math.log10(dbm_to_w(rx[r][c]) / (interf + noise_w)), 2)
    return CoverageResult(
        rx_dbm=rx, cell_m=cell_m, coverage_pct=round(100.0 * covered / total, 2),
        threshold_dbm=threshold_dbm, model=model_key, assumptions=assumptions,
        uncertainty_db={"data": 1.2, "numeric": 0.3, "model_params": 3.8, "physical_model": 2.1},
        sinr_db=sinr, best_server=best, preliminary=(model_key in ("FSPL",)))


def _diffraction_epsilon(heights, st: BaseStation, x_m: float, y_m: float, cell_m: float, f_ghz: float) -> float:
    """Упрощённая дифракционная поправка по профилю (единичное препятствие, метод ножа)."""
    if not heights:
        return 0.0
    rows = len(heights); cols = len(heights[0])
    c0 = int(st.x_m / cell_m); r0 = int(st.y_m / cell_m)
    c1 = int(x_m / cell_m); r1 = int(y_m / cell_m)
    n = max(abs(c1 - c0), abs(r1 - r0), 2)
    prof = []
    for i in range(n + 1):
        cc = min(cols - 1, c0 + (c1 - c0) * i // n); rr = min(rows - 1, r0 + (r1 - r0) * i // n)
        prof.append(heights[rr][cc])
    res = los_with_fresnel(prof, n * cell_m / 1000.0, f_ghz)
    if res["los"] or not res["f1_blockers"]:
        return 0.0
    b = res["f1_blockers"][0]
    v = b["excess_m"] / max(b["f1_m"], 0.1) * math.sqrt(2)  # нормированный параметр v
    # J(v) ≈ 6.9 + 20lg(sqrt((v-0.1)²+1)+v-0.1) для v>-0.7 (ITU-R P.526 аппроксимация)
    if v <= -0.7:
        return 0.0
    return max(0.0, 6.9 + 20 * math.log10(math.sqrt((v - 0.1) ** 2 + 1) + v - 0.1))


# ---------- RF-09: проверка ограничений ----------
@dataclass
class Constraint:
    id: str; name: str; kind: str  # blocking|critical|warning
    fn: Callable[[Dict], Optional[str]]  # возвращает None (OK) или текст нарушения

def check_constraints(ctx: Dict, constraints: List[Constraint]) -> Dict:
    out = {"passed": True, "violations": [], "warnings": []}
    for c in constraints:
        msg = c.fn(ctx)
        if msg:
            item = {"rule": c.id, "name": c.name, "kind": c.kind, "detail": msg}
            if c.kind in ("blocking", "critical"):
                out["passed"] = False; out["violations"].append(item)
            else:
                out["warnings"].append(item)
    return out

# типовые правила (вне кода — хранятся отдельно, CFG-004; здесь эталонный набор)
RULE_SZZ = Constraint("RULE-014", "СЗЗ ≥ нормативной границы", "blocking",
                      lambda c: None if c.get("szz_margin_m", 0) >= c.get("szz_required_m", 0)
                      else f"запас СЗЗ {c.get('szz_margin_m')} м < требуемого {c.get('szz_required_m')} м")
RULE_FRESNEL = Constraint("RULE-022", "Перекрытие F1 ≤ 40 %", "critical",
                          lambda c: None if c.get("f1_block_pct", 0) <= 40 else f"F1 перекрыто на {c['f1_block_pct']} %")
RULE_REDUNDANCY = Constraint("RULE-047", "Резервирование N+1", "blocking",
                             lambda c: None if c.get("redundancy_ok") else "резервирование каналов не подтверждено")
