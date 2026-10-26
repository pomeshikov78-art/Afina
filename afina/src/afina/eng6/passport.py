"""Паспорт расчётного запуска (§4.5.10, ENG-001, MOD-002/005).

Каждый запуск фиксирует: ID, модель и версию, хэши входов, параметры точности,
CRS и единицы, среду, время, статус, предупреждения, оценку применимости,
раздельные компоненты неопределённости.
"""
import hashlib, json, platform, time, uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = "1.0"


def stable_hash(obj: Any) -> str:
    s = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


@dataclass
class Passport:
    run_id: str
    function: str                       # RF-03, THM-01, …
    model_id: str
    model_version: str
    mode: str                           # Preview | Engineering | HighFidelity | Verification
    level: str                          # L0…L4
    inputs_hash: str
    inputs_ref: Dict[str, str]          # имя входа → источник/версия
    crs: str
    units: Dict[str, str]
    precision: Dict[str, Any]
    environment: Dict[str, str]
    started_at: float
    finished_at: Optional[float] = None
    status: str = "running"             # running|done|error|blocked
    warnings: List[str] = field(default_factory=list)
    applicability: Optional[Dict] = None
    uncertainty_db: Optional[Dict[str, float]] = None
    results_ref: Optional[str] = None
    reproducibility: Optional[Dict] = None

    @staticmethod
    def new(function, model_id, model_version, mode, level, inputs, crs="WGS84", units=None, precision=None):
        return Passport(
            run_id="CALC-" + time.strftime("%Y") + "-" + uuid.uuid4().hex[:6].upper(),
            function=function, model_id=model_id, model_version=model_version,
            mode=mode, level=level,
            inputs_hash=stable_hash(inputs),
            inputs_ref=inputs if isinstance(inputs, dict) else {"payload": stable_hash(inputs)},
            crs=crs, units=units or {}, precision=precision or {"grid_m": None, "tol": 1e-6},
            environment={"python": platform.python_version(), "platform": platform.platform(),
                         "backend": "CPU-reference (абстракция ARC-002: Vulkan/CUDA/OpenCL — целевые)"},
            started_at=time.time())

    def finish(self, status="done", results_ref=None, warnings=None, applicability=None, uncertainty_db=None):
        self.finished_at = time.time()
        self.status = status
        self.results_ref = results_ref
        if warnings: self.warnings.extend(warnings)
        self.applicability = applicability
        self.uncertainty_db = uncertainty_db
        return self

    def duration_s(self) -> float:
        return round((self.finished_at or time.time()) - self.started_at, 3)

    def to_dict(self) -> Dict:
        d = asdict(self)
        d["duration_s"] = self.duration_s()
        d["schema_version"] = SCHEMA_VERSION
        return d

    def self_hash(self) -> str:
        return stable_hash(self.to_dict())


def verify_reproducibility(p1: Passport, p2: Passport, result1: float, result2: float,
                           tolerance: float) -> Dict:
    """MOD-002: повторный запуск на том же снимке данных — результат в допуске.

    Расхождения CPU/GPU проверяются относительно того же допуска.
    """
    same_inputs = p1.inputs_hash == p2.inputs_hash
    delta = abs(result1 - result2)
    ok = same_inputs and delta <= tolerance
    return {"same_inputs_hash": same_inputs, "delta": round(delta, 6),
            "tolerance": tolerance, "in_tolerance": ok,
            "cpu_gpu_note": "сверка выполняется между прогонами на разных бэкендах (ARC-002); допуск единый",
            "verdict": "PASS" if ok else "FAIL"}
