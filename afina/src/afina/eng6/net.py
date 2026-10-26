"""NET-01 оптический бюджет, NET-02 сетевая модель (§4.5.4).

Throughput рассчитывается с учётом технологии, полосы, конфигурации радиоинтерфейса,
модели помех и нагрузки — НЕ только по уровню сигнала.
"""
import math
from dataclasses import dataclass
from typing import List, Optional

VERSION = "1.0.0"


# ---------- NET-01 ----------
@dataclass
class OpticalLink:
    fiber_km: float
    attenuation_db_per_km: float = 0.35        # 1550 нм SMF
    connectors: int = 4
    connector_loss_db: float = 0.5
    splices: int = 2
    splice_loss_db: float = 0.1
    splitters: List[float] = None              # списки потерь разветвителей, дБ
    tx_power_dbm: float = 0.0
    rx_sensitivity_dbm: float = -24.0

    def budget(self) -> dict:
        spl = sum(self.splitters or [])
        total_loss = (self.fiber_km * self.attenuation_db_per_km
                      + self.connectors * self.connector_loss_db
                      + self.splices * self.splice_loss_db + spl)
        rx = self.tx_power_dbm - total_loss
        margin = rx - self.rx_sensitivity_dbm
        return {"total_loss_db": round(total_loss, 2), "rx_level_dbm": round(rx, 2),
                "margin_db": round(margin, 2), "verdict": "PASS" if margin >= 3 else ("MARGINAL" if margin >= 0 else "FAIL"),
                "components": {"fiber": round(self.fiber_km * self.attenuation_db_per_km, 2),
                               "connectors": round(self.connectors * self.connector_loss_db, 2),
                               "splices": round(self.splices * self.splice_loss_db, 2),
                               "splitters": round(spl, 2)}}


# ---------- NET-02 ----------
def throughput_mbps(snr_db: Optional[float], bw_mhz: float, tech: str = "LTE",
                    mimo_layers: int = 2, modulation_cap: int = 6, load: float = 0.5) -> dict:
    """Оценка пропускной способности по Шеннону с технологическими ограничениями.

    snr_db=None — SINR «не определён»: результат НЕ выдаётся (запрет расчёта только по уровню).
    """
    if snr_db is None:
        return {"throughput_mbps": None,
                "note": "SINR не определён — throughput не рассчитывается. Требуются: конфигурация радиоинтерфейса, модель помех, нагрузка (§4.5.4)."}
    eff = {"LTE": 0.62, "NR": 0.72, "WiFi6": 0.55}.get(tech, 0.6)
    shannon = bw_mhz * math.log2(1 + 10 ** (snr_db / 10))          # Мбит/с теоретически
    bits_sym = min(modulation_cap, max(1, int(snr_db // 5) + 1))   # грубая MCS-ограниченность
    mcs_cap = bw_mhz * 12 * 14 * bits_sym * 1e-3 * 1000 / 1000     # упрощённый ресурсный потолок
    cap = min(shannon, mcs_cap) * eff * mimo_layers * (1 - load)
    return {"throughput_mbps": round(cap, 1), "shannon_mbps": round(shannon, 1),
            "efficiency": eff, "mimo_layers": mimo_layers, "load": load,
            "note": "оценка L0–L1; для проектных значений — полная конфигурация радиоинтерфейса"}


def mms1_delay_ms(arrival_rate: float, service_rate: float) -> dict:
    """M/M/1: задержка и джиттер узла (очереди)."""
    if service_rate <= arrival_rate:
        return {"stable": False, "note": "нагрузка ≥ пропускной способности — очередь неустойчива"}
    rho = arrival_rate / service_rate
    w = 1.0 / (service_rate - arrival_rate)
    return {"stable": True, "rho": round(rho, 3), "delay_ms": round(w * 1000, 2),
            "jitter_ms": round(w * 1000, 2), "losses_pct": 0.0}


def network_path(links: List[OpticalLink], nodes_load: List[tuple]) -> dict:
    """Сквозная модель пути: бюджеты всех участков + суммарная задержка очередей."""
    budgets = [l.budget() for l in links]
    queues = [mms1_delay_ms(a, s) for a, s in nodes_load]
    fail = [i for i, b in enumerate(budgets) if b["verdict"] == "FAIL"]
    delay = sum(q.get("delay_ms", 0) for q in queues)
    return {"link_budgets": budgets, "queue_delays": queues,
            "total_delay_ms": round(delay, 2), "failed_links": fail,
            "path_ok": len(fail) == 0 and all(q.get("stable", True) for q in queues)}
