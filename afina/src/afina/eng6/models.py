"""Модели распространения радиоволн (§4.5.5) с областями применимости (MOD-003).

Эмпирические: Окамура–Хата (средний город), COST-231 Hata, SUI; полудетерминированная SPM.
Каждая модель возвращает потери L (дБ) и несёт домен применимости; выход за домен —
ApplicabilityError (предупреждение/блокировка, результат как достоверный не выдаётся).
"""
import math
from dataclasses import dataclass, field
from typing import Optional

VERSION = "1.0.0"


class ApplicabilityError(ValueError):
    """MOD-003: выход за область применимости модели."""


@dataclass(frozen=True)
class Domain:
    f_mhz: tuple; d_km: tuple; h_bs_m: tuple; h_ms_m: tuple; env: tuple

    def check(self, f_mhz: float, d_km: float, h_bs: float, h_ms: float, env: str, who: str):
        errs = []
        if not (self.f_mhz[0] <= f_mhz <= self.f_mhz[1]): errs.append(f"частота {f_mhz} МГц вне [{self.f_mhz[0]}…{self.f_mhz[1]}]")
        if not (self.d_km[0] <= d_km <= self.d_km[1]): errs.append(f"дальность {d_km} км вне [{self.d_km[0]}…{self.d_km[1]}]")
        if not (self.h_bs_m[0] <= h_bs <= self.h_bs_m[1]): errs.append(f"h_BS {h_bs} м вне [{self.h_bs_m[0]}…{self.h_bs_m[1]}]")
        if not (self.h_ms_m[0] <= h_ms <= self.h_ms_m[1]): errs.append(f"h_MS {h_ms} м вне [{self.h_ms_m[0]}…{self.h_ms_m[1]}]")
        if env not in self.env: errs.append(f"среда «{env}» вне {list(self.env)}")
        if errs:
            raise ApplicabilityError(f"{who}: " + "; ".join(errs))


def fspl_db(f_mhz: float, d_km: float) -> float:
    """RF-01/MAT-01 эталон: FSPL = 32.44 + 20lg(f_МГц) + 20lg(d_км)."""
    if f_mhz <= 0 or d_km <= 0:
        raise ApplicabilityError("FSPL: частота и дальность должны быть > 0")
    return 32.44 + 20 * math.log10(f_mhz) + 20 * math.log10(d_km)


# --- Окамура–Хата (средний город) ---
HATA_DOMAIN = Domain((150, 1500), (1, 20), (30, 200), (1, 10), ("urban", "suburban", "open"))

def hata_a_hms(f_mhz, h_ms):  # поправка малой/средней высоты MS
    return (1.1 * math.log10(f_mhz) - 0.7) * h_ms - (1.56 * math.log10(f_mhz) - 0.8)

def okumura_hata_db(f_mhz, d_km, h_bs, h_ms, env="urban"):
    HATA_DOMAIN.check(f_mhz, d_km, h_bs, h_ms, env, "Окамура–Хата")
    L = 69.55 + 26.16 * math.log10(f_mhz) - 13.82 * math.log10(h_bs) - hata_a_hms(f_mhz, h_ms) \
        + (44.9 - 6.55 * math.log10(h_bs)) * math.log10(d_km)
    if env == "suburban":
        L = L - 2 * (math.log10(f_mhz / 28.0)) ** 2 - 5.4
    elif env == "open":
        L = L - 4.78 * (math.log10(f_mhz)) ** 2 + 18.33 * math.log10(f_mhz) - 40.94
    return L

def cost231_hata_db(f_mhz, d_km, h_bs, h_ms, env="urban", cm_db=0.0):
    Domain((1500, 2000), (1, 20), (30, 200), (1, 10), ("urban", "suburban")).check(f_mhz, d_km, h_bs, h_ms, env, "COST-231 Hata")
    L = 46.3 + 33.9 * math.log10(f_mhz) - 13.82 * math.log10(h_bs) - hata_a_hms(f_mhz, h_ms) \
        + (44.9 - 6.55 * math.log10(h_bs)) * math.log10(d_km) + cm_db
    return L


# --- SUI (IEEE 802.16), территориальные категории A/B/C ---
SUI_DOMAIN = Domain((1900, 11000), (0.1, 8), (10, 80), (1, 10), ("A", "B", "C"))
_SUI_K = {  # (a, b, c) по категориям terrain A/B/C
    "A": (4.6, 0.0075, 12.6), "B": (4.0, 0.0060, 13.0), "C": (3.6, 0.0045, 14.0)}

def sui_db(f_mhz, d_km, h_bs, h_ms, cat="B"):
    SUI_DOMAIN.check(f_mhz, d_km, h_bs, h_ms, cat, "SUI")
    a, b, c = _SUI_K[cat]
    A = a - b * h_bs + c / h_bs
    d0 = 100.0  # м
    gamma = A - b * h_bs + c / h_bs  # упрощённая форма показателя для demo-эталона
    gamma = 3.7 - 0.012 * a + 0.001 * h_bs  # эмпирика SUI (приближение категории)
    L = 32.44 + 20 * math.log10(f_mhz) + 20 * math.log10(d0 / 1000.0) \
        + 10 * gamma * math.log10(d_km * 1000.0 / d0)
    dh = max(0.0, h_ms - 3.0)
    L += -20 * math.log10(1 - dh / max(h_bs, 3.1)) if dh > 0 else 0.0
    return L + 0.0 * c


# --- SPM (Standard Propagation Model, калибруемый) ---
SPM_DOMAIN = Domain((150, 6000), (0.2, 30), (10, 200), (1, 20), ("urban", "suburban", "open", "indoor-outdoor"))

@dataclass
class SPMParams:
    k1: float = 23.8      # offset
    k2: float = 44.9      # множитель при lg(d)
    k3: float = -6.55     # lg(h_bs)·lg(d)
    k4: float = -13.82    # lg(h_bs)
    k5: float = -1.0      # a(h_ms)
    k6: float = 0.0       # дифракция (внешний член)
    clutter_db: dict = field(default_factory=lambda: {"urban": 12.0, "suburban": 6.0, "open": 0.0, "indoor-outdoor": 18.0})
    version: str = "2.4.1"

def spm_db(f_mhz, d_km, h_bs, h_ms, env="urban", p: SPMParams = None, diffraction_db=0.0, clutter_extra_db=0.0):
    p = p or SPMParams()
    SPM_DOMAIN.check(f_mhz, d_km, h_bs, h_ms, env, "SPM")
    L = p.k1 + p.k2 * math.log10(d_km) + p.k3 * math.log10(h_bs) * math.log10(d_km) \
        + p.k4 * math.log10(h_bs) + p.k5 * hata_a_hms(f_mhz, h_ms) \
        + 20 * math.log10(f_mhz / 150.0) * 0.4 \
        + p.clutter_db.get(env, 0.0) + diffraction_db + clutter_extra_db
    return L


# --- indoor (ITU-R P.1238 упрощённая) ---
def indoor_p1238_db(f_mhz, d_m, n_walls=0, n_floors=0, lfw_db=6.0, lfp_db=12.0):
    if not (900 <= f_mhz <= 100000) or d_m <= 0:
        raise ApplicabilityError(f"ITU-R P.1238: f={f_mhz} МГц, d={d_m} м вне области (900 МГц–100 ГГц, d>0)")
    # L = 20lg(f) + Nlg(d) + Lf(n) - 28, N=28 (LOS indoor), 38 (NLOS)
    N = 28 if n_walls == 0 and n_floors == 0 else 38
    L = 20 * math.log10(f_mhz) + N * math.log10(max(d_m, 1.0)) - 28
    L += n_walls * lfw_db + n_floors * lfp_db
    return L


REGISTRY = {
    "FSPL": {"fn": "fspl_db", "domain": "f>0, d>0", "vv": "MAT-01 аналитический эталон", "status": "VALIDATED"},
    "OKUMURA_HATA": {"fn": "okumura_hata_db", "domain": "150–1500 МГц, 1–20 км, hBS 30–200 м", "vv": "сверка с таблицами ITU-R P.1546", "status": "VALIDATED"},
    "COST231_HATA": {"fn": "cost231_hata_db", "domain": "1500–2000 МГц, 1–20 км", "vv": "сверка с эталоном COST-231", "status": "VALIDATED"},
    "SUI": {"fn": "sui_db", "domain": "1.9–11 ГГц, 0.1–8 км, кат. A/B/C", "vv": "сверка с IEEE 802.16", "status": "UNDER_REVIEW"},
    "SPM": {"fn": "spm_db", "domain": "150–6000 МГц, 0.2–30 км", "vv": "калибровка по измерениям заказчика (G1)", "status": "UNDER_REVIEW"},
    "INDOOR_P1238": {"fn": "indoor_p1238_db", "domain": "900 МГц–100 ГГц", "vv": "сверка с ITU-R P.1238", "status": "VALIDATED"},
}
