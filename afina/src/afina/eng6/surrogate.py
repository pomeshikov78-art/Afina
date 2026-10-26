"""Суррогатные модели (§4.5.8, MOD-006).

Полиномиальная аппроксимация (МНК, stdlib). Обязательное поведение:
- обучение ТОЛЬКО на проверенных расчётах (вход — точки основного решателя);
- хранение обучающего набора и его хэша;
- оценка ошибки (RMSE, max);
- область применения (бокс min/max по каждому входу);
- детекция выхода за обучающий диапазон — результат вне области НЕ выдаётся
  как достоверный (по аналогии с MOD-003);
- сравнение с основным решателем на независимых точках;
- возможность отключения (disabled) без влияния на ядро;
- суррогат ВСЕГДА маркируется и не приравнивается к физическому решателю.
"""
import hashlib, json
from typing import Callable, Dict, List, Optional, Sequence

VERSION = "1.0.0"
MARK = "СУРРОГАТ — аппроксимация, не приравнивается к физическому решателю (§4.5.8)"


def _features(x: Sequence[float], degree: int) -> List[float]:
    """Полиномиальный базис: degree=1 → [1, x...]; degree=2 → [1, x..., x_i², x_i·x_j]."""
    f = [1.0] + list(x)
    if degree >= 2:
        n = len(x)
        f += [x[i] * x[i] for i in range(n)]
        f += [x[i] * x[j] for i in range(n) for j in range(i + 1, n)]
    return f


def _solve_ls(a: List[List[float]], b: List[float]) -> Optional[List[float]]:
    """МНК через нормальные уравнения + Гаусс с выбором ведущего элемента (stdlib)."""
    m = len(a[0])
    ata = [[sum(a[r][i] * a[r][j] for r in range(len(a))) for j in range(m)] for i in range(m)]
    atb = [sum(a[r][i] * b[r] for r in range(len(a))) for i in range(m)]
    # регуляризация гребня 1e-9 — защита от вырожденности (честно: не меняет результат при m<<n)
    for i in range(m):
        ata[i][i] += 1e-9
    for col in range(m):
        piv = max(range(col, m), key=lambda r: abs(ata[r][col]))
        if abs(ata[piv][col]) < 1e-12:
            return None
        ata[col], ata[piv] = ata[piv], ata[col]
        atb[col], atb[piv] = atb[piv], atb[col]
        for r in range(col + 1, m):
            k = ata[r][col] / ata[col][col]
            for c in range(col, m):
                ata[r][c] -= k * ata[col][c]
            atb[r] -= k * atb[col]
    x = [0.0] * m
    for r in range(m - 1, -1, -1):
        x[r] = (atb[r] - sum(ata[r][c] * x[c] for c in range(r + 1, m))) / ata[r][r]
    return x


class PolynomialSurrogate:
    def __init__(self, sid: str, model_ref: str, degree: int = 2, feature_names: Optional[List[str]] = None):
        self.id = sid
        self.model_ref = model_ref          # ссылка на основной решатель (§4.5.9)
        self.degree = degree
        self.feature_names = feature_names or []
        self.coeffs: Optional[List[float]] = None
        self.domain: Optional[Dict[str, List[float]]] = None   # имя/индекс → [min,max]
        self.train_set: List[List[float]] = []
        self.train_hash: str = ""
        self.rmse: Optional[float] = None
        self.max_err: Optional[float] = None
        self.status = "Разрабатывается"      # статусы реестра §4.5.9
        self.disabled = False
        self.n_features = 0
        self.mu: List[float] = []            # нормировка входов (обусловленность МНК)
        self.sd: List[float] = []

    # ---------- обучение ----------
    def train(self, points: List[List[float]], min_points_factor: int = 3) -> Dict:
        """points: [[x1..xn, y], ...] — результаты ПРОВЕРЕННОГО основного решателя.

        Честный отказ при недостатке точек (< factor × число коэффициентов)."""
        if not points:
            return {"status": "error", "error": "пустой обучающий набор"}
        n_in = len(points[0]) - 1
        self.n_features = len(_features([0.0] * n_in, self.degree))
        if len(points) < self.n_features * min_points_factor // 2:
            return {"status": "insufficient_data",
                    "error": f"точек {len(points)} < требуемых {self.n_features * min_points_factor // 2} "
                             f"(базис {self.n_features} коэфф.) — обучение ненадёжно, суррогат не создаётся",
                    "policy": "суррогат без достаточного обучающего набора не выдаётся (§4.5.8)"}
        xs = [p[:n_in] for p in points]; ys = [p[-1] for p in points]
        names = self.feature_names or [f"x{i+1}" for i in range(n_in)]
        # нормировка входов; постоянный признак — честный отказ (коэффициент не идентифицируется)
        self.mu = [sum(x[i] for x in xs) / len(xs) for i in range(n_in)]
        self.sd = [((sum((x[i] - self.mu[i]) ** 2 for x in xs) / len(xs)) ** 0.5) for i in range(n_in)]
        const = [names[i] for i in range(n_in) if self.sd[i] < 1e-12]
        if const:
            return {"status": "insufficient_data",
                    "error": f"признак(и) {const} не варьируются в обучающем наборе — коэффициенты не идентифицируются",
                    "policy": "суррогат по вырожденному набору не создаётся (§4.5.8); варьируйте входы решателя"}
        zs = [[(x[i] - self.mu[i]) / self.sd[i] for i in range(n_in)] for x in xs]
        a = [_features(z, self.degree) for z in zs]
        coeffs = _solve_ls(a, ys)
        if coeffs is None:
            return {"status": "error", "error": "вырожденная система (дубли точек?) — обучение невозможно"}
        self.coeffs = coeffs
        self.train_set = [list(p) for p in points]
        self.train_hash = hashlib.sha256(json.dumps(self.train_set, sort_keys=True).encode()).hexdigest()
        errs = [ys[i] - sum(c * f for c, f in zip(coeffs, a[i])) for i in range(len(ys))]
        self.rmse = (sum(e * e for e in errs) / len(errs)) ** 0.5
        self.max_err = max(abs(e) for e in errs)
        self.domain = {names[i] if i < len(names) else f"x{i+1}":
                       [min(x[i] for x in xs), max(x[i] for x in xs)] for i in range(n_in)}
        self.status = "Проверяется"
        return {"status": "trained", "id": self.id, "model_ref": self.model_ref, "degree": self.degree,
                "n_points": len(points), "n_coeffs": len(coeffs), "rmse": round(self.rmse, 6),
                "max_err": round(self.max_err, 6), "domain": self.domain,
                "train_hash": self.train_hash[:16], "marked": MARK,
                "next": "сравнение с основным решателем на НЕЗАВИСИМОМ наборе → статус «Валидирована для указанной области»"}

    # ---------- предсказание ----------
    def predict(self, x: Sequence[float]) -> Dict:
        if self.disabled:
            return {"status": "disabled", "error": "суррогат отключён (§4.5.8: возможность отключения); используйте основной решатель"}
        if self.coeffs is None:
            return {"status": "error", "error": "суррогат не обучен"}
        names = list(self.domain.keys())
        if len(x) != len(names):
            return {"status": "error", "error": f"ожидалось входов: {len(names)}, получено {len(x)}"}
        outside = [f"{names[i]}={x[i]} вне [{self.domain[names[i]][0]}, {self.domain[names[i]][1]}]"
                   for i in range(len(x))
                   if not (self.domain[names[i]][0] <= x[i] <= self.domain[names[i]][1])]
        z = [(x[i] - self.mu[i]) / self.sd[i] for i in range(len(x))]
        y = sum(c * f for c, f in zip(self.coeffs, _features(z, self.degree)))
        if outside:
            return {"status": "out_of_domain", "value": round(y, 6), "marked": MARK,
                    "outside": outside,
                    "policy": "выход за обучающий диапазон: значение НЕ выдаётся как достоверное (MOD-003/MOD-006); запустите основной решатель"}
        return {"status": "ok", "value": round(y, 6), "marked": MARK,
                "rmse_train": round(self.rmse, 6), "surrogate_status": self.status}

    # ---------- сравнение с основным решателем ----------
    def compare(self, solver: Callable[[Sequence[float]], float], test_points: List[Sequence[float]]) -> Dict:
        """Сравнение на независимом наборе (§4.5.8). solver(x)->y — основной решатель."""
        if self.coeffs is None:
            return {"status": "error", "error": "суррогат не обучен"}
        if not test_points:
            return {"status": "insufficient_data", "error": "нет независимых точек для сравнения"}
        rows, errs = [], []
        for x in test_points:
            ps = self.predict(x)
            ys = solver(x)
            in_dom = ps["status"] == "ok"
            e = abs(ps.get("value", float("nan")) - ys)
            errs.append(e)
            rows.append({"x": list(x), "solver": round(ys, 6), "surrogate": ps.get("value"),
                         "abs_err": round(e, 6), "in_domain": in_dom})
        rmse_ind = (sum(e * e for e in errs) / len(errs)) ** 0.5
        return {"status": "compared", "n_test": len(test_points), "rmse_independent": round(rmse_ind, 6),
                "max_abs_err": round(max(errs), 6), "rows": rows[:50],
                "rmse_train": round(self.rmse, 6),
                "note": "обучающая и независимая ошибки разделены; совпадение «слишком хорошее» — признак утечки набора"}

    # ---------- сериализация ----------
    def to_dict(self) -> Dict:
        return {"id": self.id, "model_ref": self.model_ref, "degree": self.degree, "coeffs": self.coeffs,
                "domain": self.domain, "train_set": self.train_set, "train_hash": self.train_hash,
                "rmse": self.rmse, "max_err": self.max_err, "status": self.status,
                "disabled": self.disabled, "feature_names": self.feature_names, "marked": MARK,
                "mu": self.mu, "sd": self.sd, "version": VERSION}

    @classmethod
    def from_dict(cls, d: Dict) -> "PolynomialSurrogate":
        s = cls(d["id"], d["model_ref"], d.get("degree", 2), d.get("feature_names"))
        s.coeffs = d.get("coeffs"); s.domain = d.get("domain"); s.train_set = d.get("train_set") or []
        s.train_hash = d.get("train_hash", ""); s.rmse = d.get("rmse"); s.max_err = d.get("max_err")
        s.status = d.get("status", "Разрабатывается"); s.disabled = bool(d.get("disabled"))
        s.n_features = len(s.coeffs) if s.coeffs else 0
        s.mu = d.get("mu") or []; s.sd = d.get("sd") or []
        return s


# ---------- штатный сценарий сравнения: суррогат RF-01 против path_loss_db ----------
def rf01_solver(model_key: str = "OKUMURA_HATA", env: str = "urban") -> Callable[[Sequence[float]], float]:
    from . import rf
    def f(x):  # x = [f_mhz, d_km, h_bs, h_ms]
        return rf.path_loss_db(model_key, float(x[0]), float(x[1]), float(x[2]), float(x[3]), env=env)
    return f
