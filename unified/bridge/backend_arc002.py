"""unified/bridge/backend_arc002.py — абстракция вычислительного бэкенда (ARC-002, P0).

Требование Прил. А: «ARC-002 · Абстракция вычислительного бэкенда. Один расчёт
воспроизведён на двух типах вычислительного пути в пределах допуска».

Реализация совмещённой платформы:
  * PythonRefBackend  — эталонная реализация ENG6+ (линия A, CPU-reference);
  * Eng6ProbeBackend  — вендор ENG6 v5.1.0 (линия B) через зонд eng6_probe.mjs
                        (прямые вызовы PROP.*; сервер не требуется);
  * Eng6PlanBackend   — полный расчётный сервис ENG6-PLAN через REST
                        (plan/sync, plan/async; конформанс-гейт К-04).

Каждый бэкенд выдаёт паспорт-отпечаток (id, версия, sha256 ядра — для вендора),
который записывается в паспорт расчётного запуска (§4.5.10, ENG-001).

verify_arc002() выполняет сверку в ДВЕ стадии:
  RAW      — значения «как есть», без поправок: измеряет фактические расхождения;
  ALIGNED  — с документированными семантическими согласованиями (см. ALIGNMENTS):
             ожидаемый вердикт PASS; расхождение вне допуска → DEGRADED (не замалчивается).

Найденные при первой сверке семантические различия (зафиксированы протоколом):
  XVER-COST231-CM : ENG6.PROP.cost231 при переданном env всегда добавляет CM=+3 дБ;
                    Python cost231_hata_db по умолчанию cm_db=0 → согласование: cm_db=3.0.
  XVER-HATA-SUBURBAN: ENG6.PROP.hata трактует env='suburban' как 'urban' (без поправки);
                    Python применяет стандартную поправку −2·lg²(f/28)−5.4 дБ.
                    Несовместимо без изменения вендора (вендор неизменяем, §9.3.1 Доп.№2) →
                    точки suburban исключены из ALIGNED с документированием; при
                    планировании через ENG6-JS для suburb применять env='urban'
                    (консервативно) либо поправку на стороне платформы.
  XVER-FRESNEL    : ENG6.PROP.fresnelR(d1,d2,f) возвращает значение в √1000≈31.6228 раз
                    больше эталона RF-06 (смешение м/км в формуле вендора);
                    согласование: деление на √1000 — после него совпадение точное.

stdlib-only. Детерминированно: сетка и поправки фиксируются в протоколе.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
UNIFIED_DIR = os.path.dirname(BRIDGE_DIR)
REPO_DIR = os.path.dirname(UNIFIED_DIR)
AFINA_SRC = os.path.join(REPO_DIR, "afina", "src")
PROBE_MJS = os.path.join(BRIDGE_DIR, "eng6_probe.mjs")

if AFINA_SRC not in sys.path:
    sys.path.insert(0, AFINA_SRC)

from afina.eng6 import models as py_models  # noqa: E402  (линия A)
from afina.eng6 import net as py_net        # noqa: E402
from afina.eng6 import rf as py_rf          # noqa: E402
from afina.eng6.passport import Passport, stable_hash, verify_reproducibility  # noqa: E402

VERSION = "1.1.0"

# Пары «модель платформы ⇄ примитив ENG6» с допуском и единицами.
# Допуск — измеряемая величина протокола XVER, а не подгонка: 0.05 для
# аналитически совпадающих формул, 0.5 для эмпирик с разной записью поправок.
PAIRED_MODELS: Dict[str, Dict[str, Any]] = {
    "FSPL":         {"probe": "fspl",         "tol": 0.05, "unit": "дБ"},
    "OKUMURA_HATA": {"probe": "hata",         "tol": 0.05, "unit": "дБ"},
    "COST231_HATA": {"probe": "cost231_hata", "tol": 0.05, "unit": "дБ"},
    "FRESNEL_R1":   {"probe": "fresnel",      "tol": 0.001, "unit": "м"},
    # RF-07 (шумовая площадка kTB) ⇄ ENG6.noiseDbm: единицы разные (Гц ⇄ кГц) —
    # конвертация ЯВНАЯ на стороне платформы (ENG-004: молчаливая конвертация запрещена).
    "NOISE_FLOOR_DB": {"probe": "noise_dbm",  "tol": 0.01, "unit": "дБм"},
    # Дифракция J(v) (ITU-R P.526 аппроксимация): Python _diffraction_epsilon (rf.py)
    # ⇄ ENG6.PROP.knifeEdgeJ. Формулы идентичны: 6.9+20lg(√((v−0.1)²+1)+v−0.1);
    # пороги отсечки РАЗНЫЕ: Python v≤−0.7→0, вендор v≤−0.78→0 (XVER-KNIFE-THRESH).
    # Сетка избегает расходящейся полосы (−0.78; −0.7] — полоса документирована.
    "DIFFRACTION_J": {"probe": "knife_j", "tol": 0.01, "unit": "дБ",
                      "note": "XVER-KNIFE-THRESH: пороги отсечки −0.7 (Python) и −0.78 (вендор); "
                              "полоса (−0.78;−0.7] вне сетки, расхождение ≤0.35 дБ измерено отдельно"},
    # NET-01 (оптический бюджет ВОЛС) ⇄ ENG6.FIBER.loss: формулы совпадают после
    # параметризации (XVER-FIBER-PARAMS) — ENG6 фиксирует шаг сросток 4 км и
    # 2 коннектора; Python NET-01 принимает явные количества. Допуск 0.011 дБ —
    # бюджет Python округляет до 2 знаков.
    "FIBER_LOSS_DB": {"probe": "fiber_loss", "tol": 0.011, "unit": "дБ",
                      "note": "XVER-FIBER-PARAMS: согласование параметризации (connectors=2, "
                              "splices=⌊км/4⌋), а не физики; RAW показывает разницу дефолтных комплектов"},
    # Композитный уровень: настоящий ENG6.calcLink (РРЛ, flat-рельеф, нулевые
    # дождь/туман, open-clutter loss=0, misc=0, feeder=0). При f ≤ 900 МГц
    # газопоглощение ENG6 равно 0 → остаётся только prop=FSPL, и связка
    # «预算 Python RF-07 ⇄ calcLink» обязана совпасть до округления Python (2 знака).
    # При f ≥ 1400 МГц проявляется честный зазор: RF-07 не моделирует поглощение
    # в газах (OBS-11) — такие точки в сетку НЕ входят (не подгонка, а граница
    # применимости пары; зазор измерен и задокументирован отдельно).
    "CALC_LINK_PROP_DB": {"probe": "calc_link_prop", "tol": 0.01, "unit": "дБ",
                          "note": "breakdown.prop calcLink (РРЛ/flat) ⇄ RF-01 FSPL"},
    "CALC_LINK_RX_DBM": {"probe": "calc_link_rx", "tol": 0.011, "unit": "дБм",
                         "note": "rxPowerDbm calcLink ⇄ RF-07 LinkBudget.rx_level_dbm; "
                                 "допуск = округление бюджета Python до 2 знаков"},
    "CALC_LINK_NOISE_DBM": {"probe": "calc_link_noise", "tol": 0.011, "unit": "дБм",
                            "note": "noiseDbm calcLink ⇄ kTB Python (−174+10lg B_Гц+NF)"},
}

# Документированные семантические согласования (стадия ALIGNED).
ALIGNMENTS: Dict[str, Dict[str, Any]] = {
    "COST231_HATA": {
        "id": "XVER-COST231-CM",
        "a_kwargs": {"cm_db": 3.0},
        "note": "ENG6.PROP.cost231 при переданном env применяет CM=+3 дБ; "
                "Python-стороне явно передаётся cm_db=3.0 (измерено: Δ=+3.0 дБ на всех точках RAW)",
    },
    "FRESNEL_R1": {
        "id": "XVER-FRESNEL",
        "b_scale_div": math.sqrt(1000.0),
        "note": "ENG6.PROP.fresnelR возвращает значение в √1000 раз больше эталона RF-06 "
                "(смешение м/км в формуле вендора); после деления на √1000 совпадение точное",
    },
    "FIBER_LOSS_DB": {
        "id": "XVER-FIBER-PARAMS",
        "a_kwargs": {"connectors": 2, "splice_pitch_km": 4},
        "note": "ENG6.FIBER.loss фиксирует 2 коннектора и сростки каждые 4 км; Python NET-01 "
                "принимает явные количества. Согласование параметризации: connectors=2, "
                "splices=⌊км/4⌋ → формулы идентичны (км·att + сростки·splice + коннекторы·conn)",
    },
    "CALC_LINK_RX_DBM": {
        "id": "XVER-GAS-SUBST",
        "gas_substitute": True,
        "note": "RF-07 (линия A) с 26.10.2026 имеет поле gas_absorption_db, но собственной "
                "модели поглощения в газах (ITU-R P.676) не содержит — в стадии ALIGNED член "
                "gas = gasGamma(f,15°C,60%)·d подставляется из КАНОНИЧЕСКОГО источника "
                "(вендор ENG6) и фиксируется в протоколе (alignment_gas_db каждой строки). "
                "Проверяется КОМПОЗИЦИЯ бюджета, а не модель газа (OBS-11 — зазор остаётся "
                "открытым до реализации P.676 в линии A). Для f ≤ 900 МГц gas=0 — подстановка не нужна",
    },
}

# Точки, исключённые из ALIGNED с обоснованием (не замалчиваются — попадают в протокол).
KNOWN_INCOMPATIBLE = {
    ("OKUMURA_HATA", "suburban"): {
        "id": "XVER-HATA-SUBURBAN",
        "reason": "ENG6.hata трактует suburban как urban; Python применяет стандартную поправку "
                  "−2·lg²(f/28)−5.4 дБ. Вендор неизменяем (§9.3.1 Доп. №2). Для suburb при "
                  "планировании через ENG6-JS применять env='urban' (консервативно) или поправку платформы.",
    },
}


class BackendError(RuntimeError):
    pass


@dataclass
class BackendFingerprint:
    backend_id: str
    version: str
    sha256: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = {"backend": self.backend_id, "version": self.version}
        if self.sha256:
            d["sha256"] = self.sha256
        d.update(self.extra)
        return d


class ComputeBackend:
    """Интерфейс вычислительного пути (ARC-002)."""

    id = "abstract"
    version = "0.0.0"

    def fingerprint(self) -> BackendFingerprint:
        raise NotImplementedError

    def canonical_hash(self, value: Any) -> str:
        """Точный canonicalHash ядра (core/validate.mjs) — для сквозной связки аудитов.

        Повторять JS-сериализацию чисел в Python ненадёжно (toPrecision(12) +
        кратчайшая форма), поэтому хэш считается ТОЙ ЖЕ функцией контура.
        """
        resp = self._call({"mode": "canonical_hash", "value": value})
        return resp["sha256"]

    def evaluate(self, point: Dict[str, Any]) -> float:
        """Расчёт одной точки сетки XVER (модель + параметры + опц. согласования)."""
        raise NotImplementedError

    # совместимость с RF-интерфейсом платформы
    def path_loss_db(self, model_key: str, f_mhz: float, d_km: float,
                     h_bs_m: float, h_ms_m: float, env: str = "open") -> float:
        return self.evaluate({"model": model_key, "f_mhz": f_mhz, "d_km": d_km,
                              "h_bs_m": h_bs_m, "h_ms_m": h_ms_m, "env": env})


class PythonRefBackend(ComputeBackend):
    """Эталонная реализация ENG6+ (линия A): RF-01/RF-06 через afina.eng6.rf/models."""

    id = "ENG6+-Python-reference"

    def __init__(self) -> None:
        self.version = py_rf.VERSION

    def fingerprint(self) -> BackendFingerprint:
        src_hash = hashlib.sha256()
        for mod in (py_models, py_rf):
            with open(mod.__file__, "rb") as f:
                src_hash.update(f.read())
        return BackendFingerprint(self.id, self.version, sha256=src_hash.hexdigest(),
                                  extra={"path": "afina/eng6 (RF-01…09)",
                                         "runtime": f"python {sys.version.split()[0]}"})

    def evaluate(self, point: Dict[str, Any]) -> float:
        m = point["model"]
        if m == "FRESNEL_R1":
            return py_rf.fresnel_radius_m(1, float(point["d1_km"]), float(point["d2_km"]), float(point["f_ghz"]))
        if m == "NOISE_FLOOR_DB":
            # RF-07: шум = K_BOLTZMANN_DB + 10·lg(B_Гц) + NF; B_кГц → B_Гц явно (ENG-004)
            bw_hz = float(point["bw_khz"]) * 1000.0
            return py_rf.K_BOLTZMANN_DB + 10.0 * math.log10(bw_hz) + float(point["nf_db"])
        if m == "DIFFRACTION_J":
            # J(v) по формуле rf.py::_diffraction_epsilon (ITU-R P.526 аппроксимация):
            #   v ≤ −0.7 → 0; иначе 6.9 + 20·lg(√((v−0.1)²+1) + v − 0.1)
            # (код линии A не изменяется — формула воспроизведена по её документации)
            v = float(point["v"])
            if v <= -0.7:
                return 0.0
            return 6.9 + 20.0 * math.log10(math.sqrt((v - 0.1) ** 2 + 1.0) + v - 0.1)
        if m == "FIBER_LOSS_DB":
            # NET-01: суммарные потери ВОЛС. Без согласования — дефолты Python
            # (4 коннектора × 0.5 дБ, 2 сростки × 0.1 дБ); с согласованием
            # XVER-FIBER-PARAMS — параметризация ENG6 (2 коннектора, сростки ⌊км/4⌋).
            km = float(point["len_km"])
            kw = dict(point.get("a_kwargs") or {})
            pitch = kw.get("splice_pitch_km")
            splices = int(km // float(pitch)) if pitch else int(point.get("splices", 2))
            connectors = int(kw.get("connectors", point.get("connectors", 4)))
            budget = py_net.OpticalLink(
                fiber_km=km,
                attenuation_db_per_km=float(point["att_db_km"]),
                connectors=connectors,
                connector_loss_db=float(point["conn_db"]),
                splices=splices,
                splice_loss_db=float(point["splice_db"]),
                splitters=[]).budget()
            return budget["total_loss_db"]
        if m == "CALC_LINK_PROP_DB":
            # RF-01 FSPL: эталон распространения для РРЛ-ветви calcLink
            return py_rf.path_loss_db("FSPL", float(point["f_mhz"]), float(point["d_km"]), 30.0, 30.0)
        if m in ("CALC_LINK_RX_DBM", "CALC_LINK_NOISE_DBM"):
            # RF-07: бюджет линии с теми же параметрами, что у сценария зонда
            # (feeder=0, diffraction=0 — flat; усиления одинаковы на обоих концах;
            #  gas_absorption_db — из a_kwargs при согласовании XVER-GAS-SUBST)
            kw = dict(point.get("a_kwargs") or {})
            pl = py_rf.path_loss_db("FSPL", float(point["f_mhz"]), float(point["d_km"]), 30.0, 30.0)
            lb = py_rf.LinkBudget(
                tx_power_dbm=float(point["tx_dbm"]), tx_feeder_loss_db=0.0,
                tx_ant_gain_dbi=float(point["gain_dbi"]), path_loss_db=pl,
                diffraction_db=0.0, rx_ant_gain_dbi=float(point["gain_dbi"]),
                rx_feeder_loss_db=0.0, noise_figure_db=float(point.get("nf_db", 6.0)),
                bandwidth_hz=float(point.get("bw_khz", 25.0)) * 1000.0,
                gas_absorption_db=float(kw.get("gas_absorption_db", 0.0)))
            res = lb.result()
            return res["rx_level_dbm"] if m == "CALC_LINK_RX_DBM" else res["noise_floor_dbm"]
        kw = dict(point.get("a_kwargs") or {})
        if m == "COST231_HATA":
            # rf.path_loss_db не пробрасывает cm_db → вызываем модель напрямую (код линии A не изменяется)
            return py_models.cost231_hata_db(float(point["f_mhz"]), float(point["d_km"]),
                                             float(point["h_bs_m"]), float(point["h_ms_m"]),
                                             point.get("env", "urban"), **kw)
        return py_rf.path_loss_db(m, float(point["f_mhz"]), float(point["d_km"]),
                                  float(point["h_bs_m"]), float(point["h_ms_m"]),
                                  env=point.get("env", "open"))


class Eng6ProbeBackend(ComputeBackend):
    """Вендор ENG6 v5.1.0 (линия B) через зонд: пакетный вызов PROP-примитивов.

    Зонд запускается один раз на пакет точек (subprocess, stdin/stdout JSON).
    Сервер ENG6-PLAN не требуется — путь работает в режиме Б без сети.
    """

    id = "ENG6-JS-vendor-probe"

    def __init__(self, node_bin: str = "node", probe_path: str = PROBE_MJS):
        self.node_bin = node_bin
        self.probe_path = probe_path
        self._core: Optional[Dict[str, Any]] = None

    def _call(self, request: Dict[str, Any], timeout_s: float = 60.0) -> Dict[str, Any]:
        proc = subprocess.run(
            [self.node_bin, self.probe_path],
            input=json.dumps(request, ensure_ascii=False).encode("utf-8"),
            capture_output=True, timeout=timeout_s)
        if not proc.stdout:
            raise BackendError(f"зонд ENG6 не ответил (rc={proc.returncode}): {proc.stderr.decode('utf-8', 'replace')[:400]}")
        resp = json.loads(proc.stdout.decode("utf-8"))
        if not resp.get("ok"):
            raise BackendError(f"зонд ENG6: {resp.get('error')}")
        return resp

    def probe_meta(self) -> Dict[str, Any]:
        if self._core is None:
            self._core = self._call({"mode": "meta"})["core"]
        return self._core

    def fingerprint(self) -> BackendFingerprint:
        core = self.probe_meta()
        return BackendFingerprint(self.id, str(core.get("version") or "5.1.0"), sha256=core.get("sha256"),
                                  extra={"vendor_bytes": core.get("bytes"),
                                         "prop_primitives": len(core.get("propKeys") or []),
                                         "runtime": "node (изолированный V8-контекст)"})

    def eval_points(self, points: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        payload = []
        for p in points:
            pair = PAIRED_MODELS[p["model"]]
            q: Dict[str, Any] = {"model": pair["probe"]}
            for k in ("f_mhz", "d_km", "h_bs_m", "h_ms_m", "env", "n", "d1_km", "d2_km", "f_ghz",
                      "bw_khz", "nf_db", "v", "len_km", "att_db_km", "splice_db", "conn_db",
                      "tx_dbm", "gain_dbi", "temp_c", "hum"):
                if p.get(k) is not None:
                    q[k] = p[k]
            payload.append(q)
        return self._eval_raw(payload)

    def _eval_raw(self, payload: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        resp = self._call({"mode": "batch", "points": list(payload)})
        self._core = resp.get("core") or self._core
        return resp["results"]

    def gas_gamma_batch(self, freqs_mhz: Sequence[float], temp_c: float = 15.0,
                        hum: float = 60.0) -> List[float]:
        """Удельное поглощение в газах, дБ/км (ENG6.PROP.gasGamma) — пакетно.

        Используется согласованием XVER-GAS-SUBST: RF-07 линии A с 26.10.2026
        имеет поле gas_absorption_db, но собственной модели P.676 нет — значение
        члена берётся из канонического источника (вендор ENG6) и фиксируется
        в протоколе как подстановка (OBS-11).
        """
        res = self._eval_raw([{"model": "gas_gamma", "f_mhz": f, "temp_c": temp_c, "hum": hum}
                              for f in freqs_mhz])
        out = []
        for r in res:
            if not r.get("ok"):
                raise BackendError(f"gasGamma: {r.get('error')}")
            out.append(float(r["value"]))
        return out

    def evaluate(self, point: Dict[str, Any]) -> float:
        res = self.eval_points([point])[0]
        if not res.get("ok"):
            raise BackendError(f"ENG6.{PAIRED_MODELS[point['model']]['probe']}: {res.get('error')}")
        return float(res["value"])


class Eng6PlanBackend:
    """Полный вычислительный контур ENG6-PLAN (REST).

    Второй вычислительный путь УРОВНЯ ПЛАНА: паспорт запуска фиксирует ядро
    (sha256 + версия), конформанс-гейт К-04 выполняется до расчёта.
    """

    id = "ENG6-PLAN-service"

    def __init__(self, client):
        self.client = client

    def fingerprint(self) -> BackendFingerprint:
        meta = self.client.meta()
        core = meta.get("core") or meta.get("eng6") or {}
        svc = meta.get("service") if isinstance(meta.get("service"), dict) else {}
        return BackendFingerprint(self.id,
                                  str(core.get("version") or meta.get("version") or "?"),
                                  sha256=core.get("sha256"),
                                  extra={"service_version": svc.get("version") or meta.get("version"),
                                         "endpoints": len(meta.get("endpoints") or [])})

    def plan(self, request: Dict[str, Any], mode: str = "auto") -> Dict[str, Any]:
        self.client.conformance_gate()  # К-04: недостоверное ядро → отказ, не подмена
        if mode == "sync":
            return self.client.plan_sync(request)
        if mode == "async":
            accepted = self.client.plan_async(request)
            done = self.client.wait_task(accepted["taskId"])
            if (done.get("state") or done.get("status")) != "SUCCEEDED":
                raise BackendError(f"задача не выполнена: {json.dumps(done.get('error'), ensure_ascii=False)[:300]}")
            return done.get("result") or {}
        return self.client.plan_auto(request)


# ---------- ARC-002: сверка двух вычислительных путей ----------

def default_grid() -> List[Dict[str, Any]]:
    """Детерминированная сетка XVER внутри доменов моделей (MOD-003)."""
    pts: List[Dict[str, Any]] = []
    # Hata: 150–1500 МГц, 1–20 км, hBS 30–200, hMS 1–10; env urban и open (suburban — см. KNOWN_INCOMPATIBLE)
    for f in (150.0, 450.0, 900.0, 1500.0):
        for d in (1.0, 5.0, 12.0, 20.0):
            for h_bs, h_ms in ((30.0, 2.0), (50.0, 6.0)):
                for env in ("urban", "open"):
                    pts.append({"model": "OKUMURA_HATA", "f_mhz": f, "d_km": d,
                                "h_bs_m": h_bs, "h_ms_m": h_ms, "env": env})
    # Hata suburban — RAW-свидетели документированного расхождения (в ALIGNED исключаются)
    for f in (450.0, 900.0):
        pts.append({"model": "OKUMURA_HATA", "f_mhz": f, "d_km": 5.0,
                    "h_bs_m": 30.0, "h_ms_m": 2.0, "env": "suburban"})
    # COST-231: 1500–2000 МГц
    for f in (1500.0, 1800.0, 2000.0):
        for d in (1.0, 5.0, 15.0):
            pts.append({"model": "COST231_HATA", "f_mhz": f, "d_km": d,
                        "h_bs_m": 30.0, "h_ms_m": 2.0, "env": "urban"})
    # FSPL: f>0, d>0
    for f in (30.0, 450.0, 900.0, 2400.0, 5800.0):
        for d in (0.5, 5.0, 50.0):
            pts.append({"model": "FSPL", "f_mhz": f, "d_km": d,
                        "h_bs_m": 30.0, "h_ms_m": 2.0, "env": "open"})
    # RF-06 ⇄ PROP.fresnelR (первая зона Френеля)
    for f_ghz in (1.0, 2.5, 5.8, 10.0):
        for d1, d2 in ((1.0, 1.0), (2.5, 2.5), (5.0, 10.0)):
            pts.append({"model": "FRESNEL_R1", "d1_km": d1, "d2_km": d2, "f_ghz": f_ghz})
    # RF-07 (шумовая площадка kTB) ⇄ ENG6.noiseDbm: полосы УКВ…СПУТНИК, NF 3…10 дБ
    for bw_khz in (25.0, 200.0, 3000.0, 28000.0, 36000.0):
        for nf_db in (3.0, 6.0, 10.0):
            pts.append({"model": "NOISE_FLOOR_DB", "bw_khz": bw_khz, "nf_db": nf_db})
    # Дифракция J(v) ⇄ PROP.knifeEdgeJ: сетка МИНУЕТ полосу расхождения порогов
    # (−0.78; −0.7] (XVER-KNIFE-THRESH); значение внутри полосы измеряется отдельно.
    for v in (-2.0, -1.0, -0.8, -0.5, -0.2, 0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 6.0, 8.0, 10.0):
        pts.append({"model": "DIFFRACTION_J", "v": v})
    # NET-01 ⇄ FIBER.loss: два комплекта параметров (дефолт ENG6 0.22/0.05/0.3 и
    # дефолт Python 0.35/0.1/0.5), длины от внутриобъектных до магистральных
    for att, splice, conn in ((0.22, 0.05, 0.3), (0.35, 0.1, 0.5)):
        for km in (0.5, 1.0, 4.0, 8.0, 13.7, 25.0, 100.0):
            pts.append({"model": "FIBER_LOSS_DB", "len_km": km,
                        "att_db_km": att, "splice_db": splice, "conn_db": conn})
    # Композитный уровень ⇄ ENG6.calcLink (РРЛ, flat, open, rain/fog/misc=0).
    # f ≤ 900 МГц: gas=0 — совпадение без подстановок; f ≥ 1400 МГц: в стадии
    # ALIGNED применяется XVER-GAS-SUBST (член газа из канонического источника,
    # RF-07.gas_absorption_db; композиция проверяется, модель газа — зазор OBS-11).
    for f in (150.0, 450.0, 900.0, 1400.0, 2000.0, 7000.0):
        for d in (1.0, 5.0, 10.0, 20.0, 50.0):
            for tx, g in ((43.0, 30.0), (33.0, 14.0)):
                base = {"f_mhz": f, "d_km": d, "tx_dbm": tx, "gain_dbi": g,
                        "bw_khz": 25.0, "nf_db": 6.0}
                pts.append({"model": "CALC_LINK_PROP_DB", **base})
                pts.append({"model": "CALC_LINK_RX_DBM", **base})
                pts.append({"model": "CALC_LINK_NOISE_DBM", **base})
    return pts


def _run_stage(a: ComputeBackend, b: ComputeBackend, grid: List[Dict[str, Any]],
               aligned: bool) -> Dict[str, Any]:
    """Одна стадия сверки. aligned=True → применяются ALIGNMENTS, исключаются KNOWN_INCOMPATIBLE."""
    # Путь B — пакетный: один subprocess на всю стадию.
    probe_rows = [p for p in grid
                  if not (aligned and (p["model"], p.get("env")) in KNOWN_INCOMPATIBLE)]
    b_raw: Dict[int, Dict[str, Any]] = {}
    if probe_rows and isinstance(b, Eng6ProbeBackend):
        results = b.eval_points(probe_rows)
        for p, r in zip(probe_rows, results):
            b_raw[id(p)] = r

    # XVER-GAS-SUBST: удельное поглощение в газах из канонического источника
    # (вендор ENG6) — для точек ALIGNMENTS с gas_substitute при f > 900 МГц.
    gas_by_f: Dict[float, float] = {}
    if aligned and isinstance(b, Eng6ProbeBackend):
        freqs = sorted({float(p["f_mhz"]) for p in grid
                        if ALIGNMENTS.get(p["model"], {}).get("gas_substitute")
                        and float(p.get("f_mhz", 0)) > 900.0})
        if freqs:
            for f, gv in zip(freqs, b.gas_gamma_batch(freqs)):
                gas_by_f[f] = gv

    rows: List[Dict[str, Any]] = []
    n_consistent = n_degraded = n_failed = n_excluded = 0
    max_delta: Dict[str, float] = {}

    for p in grid:
        model = p["model"]
        pair = PAIRED_MODELS.get(model)
        if pair is None:
            continue
        row: Dict[str, Any] = {"point": p, "tolerance": pair["tol"], "unit": pair["unit"]}

        if aligned and (model, p.get("env")) in KNOWN_INCOMPATIBLE:
            inc = KNOWN_INCOMPATIBLE[(model, p.get("env"))]
            row["verdict"] = "EXCLUDED"
            row["exclusion_id"] = inc["id"]
            row["exclusion_reason"] = inc["reason"]
            n_excluded += 1
            rows.append(row)
            continue

        point_a = dict(p)
        scale_div = 1.0
        if aligned:
            al = ALIGNMENTS.get(model)
            if al:
                if "a_kwargs" in al:
                    point_a["a_kwargs"] = dict(al["a_kwargs"])
                if al.get("gas_substitute"):
                    gv = gas_by_f.get(float(p.get("f_mhz", 0.0)), 0.0)
                    gas_db = gv * float(p.get("d_km", 0.0))
                    point_a["a_kwargs"] = {**(point_a.get("a_kwargs") or {}),
                                           "gas_absorption_db": gas_db}
                    row["alignment_gas_db"] = round(gas_db, 6)
                scale_div = float(al.get("b_scale_div", 1.0))
                row["alignment_id"] = al["id"]

        try:
            row["a"] = round(a.evaluate(point_a), 6)
        except Exception as e:
            row["a_error"] = f"{type(e).__name__}: {e}"
        br = b_raw.get(id(p))
        try:
            if br is not None:
                if not br.get("ok"):
                    raise BackendError(str(br.get("error")))
                vb = float(br["value"])
            else:
                vb = b.evaluate(point_a)
            row["b_raw"] = round(vb, 6)
            row["b"] = round(vb / scale_div, 6) if scale_div != 1.0 else round(vb, 6)
        except Exception as e:
            row["b_error"] = f"{type(e).__name__}: {e}"

        if "a" in row and "b" in row:
            delta = abs(row["a"] - row["b"])
            row["delta"] = round(delta, 6)
            row["verdict"] = "CONSISTENT" if delta <= pair["tol"] else "DEGRADED"
            max_delta[model] = max(max_delta.get(model, 0.0), delta)
            if delta <= pair["tol"]:
                n_consistent += 1
            else:
                n_degraded += 1
        else:
            row["verdict"] = "FAIL"
            n_failed += 1
        rows.append(row)

    verdict = "FAIL" if n_failed else ("PASS" if n_degraded == 0 else "DEGRADED")
    return {
        "stage": "ALIGNED" if aligned else "RAW",
        "points": len(rows),
        "summary": {"consistent": n_consistent, "degraded": n_degraded, "failed": n_failed,
                    "excluded": n_excluded,
                    "max_delta_by_model": {k: round(v, 6) for k, v in max_delta.items()}},
        "verdict": verdict,
        "rows": rows,
    }


def verify_arc002(backend_a: ComputeBackend, backend_b: ComputeBackend,
                  grid: Optional[Sequence[Dict[str, Any]]] = None,
                  protocol_path: Optional[str] = None) -> Dict[str, Any]:
    """Один расчёт — два вычислительных пути — сверка в допуске (ARC-002).

    Протокол двухстадийный: RAW (фактические расхождения) + ALIGNED
    (с документированными согласованиями). Итоговый вердикт — по ALIGNED:
      PASS     — все точки в допуске (исключения документированы);
      DEGRADED — есть точки вне допуска (измерено, не замалчивается);
      FAIL     — отказ одного из путей.
    """
    grid = list(grid if grid is not None else default_grid())
    fa, fb = backend_a.fingerprint(), backend_b.fingerprint()
    raw = _run_stage(backend_a, backend_b, grid, aligned=False)
    aligned = _run_stage(backend_a, backend_b, grid, aligned=True)
    protocol = {
        "document": "ПРОТОКОЛ ARC-002 — сверка двух вычислительных путей (XVER, 2 стадии)",
        "protocol_version": VERSION,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "requirement": {"id": "ARC-002", "priority": "P0",
                        "criteria": "Один расчёт воспроизведён на двух типах вычислительного пути в пределах допуска"},
        "path_a": fa.to_dict(),
        "path_b": fb.to_dict(),
        "grid_points": len(grid),
        "tolerances": {k: {"tol": v["tol"], "unit": v["unit"]} for k, v in PAIRED_MODELS.items()},
        "model_notes": {k: v["note"] for k, v in PAIRED_MODELS.items() if v.get("note")},
        "alignments": {k: {"id": v["id"], "note": v["note"]} for k, v in ALIGNMENTS.items()},
        "known_incompatible": [dict(v, model=k[0], env=k[1]) for k, v in KNOWN_INCOMPATIBLE.items()],
        "stages": {"raw": raw, "aligned": aligned},
        # верхний уровень — для совместимости с потребителями (итог = ALIGNED)
        "summary": aligned["summary"],
        "verdict": aligned["verdict"],
        "rows": aligned["rows"],
    }
    if protocol_path:
        os.makedirs(os.path.dirname(protocol_path) or ".", exist_ok=True)
        with open(protocol_path, "w", encoding="utf-8") as f:
            json.dump(protocol, f, ensure_ascii=False, indent=1)
    return protocol


def make_passport(function: str, model_id: str, inputs: Dict[str, Any],
                  backends: Sequence[BackendFingerprint], mode: str = "Verification") -> Passport:
    """Паспорт запуска (ENG-001) с фиксацией ВСЕХ использованных бэкендов (ARC-002)."""
    p = Passport.new(function=function, model_id=model_id, model_version=VERSION,
                     mode=mode, level="L2", inputs=inputs)
    p.environment["backend"] = " + ".join(b.backend_id for b in backends)
    p.environment["backends"] = json.dumps([b.to_dict() for b in backends], ensure_ascii=False)
    return p


__all__ = ["ComputeBackend", "PythonRefBackend", "Eng6ProbeBackend", "Eng6PlanBackend",
           "BackendFingerprint", "BackendError", "PAIRED_MODELS", "ALIGNMENTS",
           "KNOWN_INCOMPATIBLE", "default_grid", "verify_arc002", "make_passport",
           "verify_reproducibility", "stable_hash", "Passport"]
