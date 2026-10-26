"""Контроль размерностей (ENG-004, MAT-02).

Передача несовместимых единиц блокируется с объяснением. Подстановка
условного значения вместо неизвестного запрещена (EQP-001): None — «неизвестно».
"""
from dataclasses import dataclass
from typing import Optional
import math

# базовые размерности: (масса, длина, время, ток, температура, количество, сила света) + лог-единицы отдельно
DIM = {
    "1": (0, 0, 0, 0, 0, 0, 0),
    "м": (0, 1, 0, 0, 0, 0, 0), "км": (0, 1, 0, 0, 0, 0, 0),
    "с": (0, 0, 1, 0, 0, 0, 0), "мин": (0, 0, 1, 0, 0, 0, 0), "ч": (0, 0, 1, 0, 0, 0, 0),
    "Гц": (0, 0, -1, 0, 0, 0, 0), "МГц": (0, 0, -1, 0, 0, 0, 0), "ГГц": (0, 0, -1, 0, 0, 0, 0),
    "Вт": (1, 2, -3, 0, 0, 0, 0), "кВт": (1, 2, -3, 0, 0, 0, 0),
    "В": (1, 2, -3, -1, 0, 0, 0), "А": (0, 0, 0, 1, 0, 0, 0),
    "К": (0, 0, 0, 0, 1, 0, 0), "°C": (0, 0, 0, 0, 1, 0, 0),
    "м/с": (0, 1, -1, 0, 0, 0, 0),
    "Дж": (1, 2, -2, 0, 0, 0, 0),
}
# логарифмические единицы — отдельное семейство (не смешиваются с линейными)
LOG_UNITS = {"дБ", "дБм", "дБи", "дБ/м"}
FACTOR = {"км": 1e3, "мин": 60.0, "ч": 3600.0, "МГц": 1e6, "ГГц": 1e9, "кВт": 1e3, "1": 1.0,
          "м": 1.0, "с": 1.0, "Гц": 1.0, "Вт": 1.0, "В": 1.0, "А": 1.0, "К": 1.0, "м/с": 1.0, "Дж": 1.0}


class DimensionError(ValueError):
    """Несовместимые единицы/размерности (ENG-004)."""


@dataclass(frozen=True)
class Q:
    """Величина с единицей. value=None — «неизвестно» (EQP-001), арифметика запрещена."""
    value: Optional[float]
    unit: str

    def __post_init__(self):
        if self.value is not None and self.unit not in DIM and self.unit not in LOG_UNITS:
            raise DimensionError(f"неизвестная единица «{self.unit}» (реестр единиц: {sorted(DIM)})")

    # --- проверки ---
    def known(self) -> bool:
        return self.value is not None

    def require(self, ctx: str = "") -> float:
        if self.value is None:
            raise DimensionError(f"значение «неизвестно» — арифметика запрещена (EQP-001){': ' + ctx if ctx else ''}")
        return self.value

    def _dim(self):
        if self.unit in LOG_UNITS:
            return ("LOG", self.unit)
        return DIM[self.unit]

    def to(self, unit: str) -> "Q":
        """Пересчёт в совместимую единицу. Только линейные единицы одного семейства."""
        if self.value is None:
            return Q(None, unit)
        if self.unit == unit:
            return self
        if self.unit in LOG_UNITS or unit in LOG_UNITS:
            if self.unit != unit:
                raise DimensionError(f"лог-единицы «{self.unit}»→«{unit}» не конвертируются автоматически: задайте явное преобразование (дБм↔мВт)")
            return self
        if DIM[self.unit] != DIM[unit]:
            raise DimensionError(
                f"несовместимые размерности: «{self.unit}» {DIM[self.unit]} → «{unit}» {DIM[unit]} — передача заблокирована (ENG-004)")
        return Q(self.value * FACTOR[self.unit] / FACTOR[unit], unit)

    # --- арифметика (линейные единицы) ---
    def __add__(self, o: "Q") -> "Q":
        return Q(self.require("+") + o.to(self.unit).require("+"), self.unit)

    def __sub__(self, o: "Q") -> "Q":
        return Q(self.require("-") - o.to(self.unit).require("-"), self.unit)

    def __mul__(self, o) -> "Q":
        if isinstance(o, Q):
            if o.unit != "1":
                raise DimensionError(f"умножение величин «{self.unit}»×«{o.unit}»: упростите размерность явно")
            return Q(self.require("*") * o.require("*"), self.unit)
        return Q(self.require("*") * float(o), self.unit)

    __rmul__ = __mul__

    def __truediv__(self, o) -> "Q":
        if isinstance(o, Q):
            if o.unit != "1":
                raise DimensionError(f"деление «{self.unit}»/«{o.unit}»: упростите размерность явно")
            return Q(self.require("/") / o.require("/"), self.unit)
        return Q(self.require("/") / float(o), self.unit)


def dbm_to_w(dbm: float) -> float:
    return 10.0 ** ((dbm - 30.0) / 10.0)


def w_to_dbm(w: float) -> float:
    if w <= 0:
        raise DimensionError("мощность должна быть > 0 для перевода в дБм")
    return 10.0 * math.log10(w) + 30.0


def db_add(db_values) -> float:
    """Сложение логарифмических величин в дБ (с контролем None)."""
    s = 0.0
    for v in db_values:
        if v is None:
            raise DimensionError("компонент бюджета «неизвестен» — расчёт заблокирован (EQP-001)")
        s += float(v)
    return s
