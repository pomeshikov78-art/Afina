"""Тайловые карты (XYZ/TMS, DB3D-совместимая структура кэша) и классификатор
условных знаков RSC-профиль с генерализацией (§4.1.1, GIS-006/008).

ВАЖНО: сами тайлы Евразии — ДАННЫЕ, а не код. В поставке: структура кэша,
манифест, офлайн-загрузчик (выполняется ТАМ, где разрешён доступ к источнику,
далее — перенос по процедуре AUT-004/005 в изолированный контур) и
процедура верификации целостности. Лицензионная чистота источника тайлов
подтверждается заказчиком (G0.2).
"""
import math, json, os, hashlib
from typing import Optional

VERSION = "1.0.0"


def latlon_to_tile(lat, lon, z) -> tuple:
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    latr = math.radians(lat)
    y = int((1.0 - math.log(math.tan(latr) + 1 / math.cos(latr)) / math.pi) / 2.0 * n)
    return x, y


def tile_bounds(x, y, z):
    n = 2 ** z
    lon1 = x / n * 360.0 - 180.0
    lon2 = (x + 1) / n * 360.0 - 180.0
    lat1 = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    lat2 = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return (min(lat1, lat2), lon1, max(lat1, lat2), lon2)


class TileStore:
    """Локальный кэш тайлов: <root>/<scheme>/<z>/<x>/<y>.<ext> + manifest.json."""

    def __init__(self, root: str, scheme: str = "GoogleMapsCompatible"):
        # scheme: GoogleMapsCompatible (EPSG:3857) | GoogleCrs84Quad (EPSG:4326) — как DB3D
        assert scheme in ("GoogleMapsCompatible", "GoogleCrs84Quad")
        self.root = os.path.join(root, scheme)
        self.scheme = scheme
        os.makedirs(self.root, exist_ok=True)

    def path(self, z, x, y, ext="png"):
        return os.path.join(self.root, str(z), str(x), f"{y}.{ext}")

    def put(self, z, x, y, data: bytes, ext="png") -> str:
        p = self.path(z, x, y, ext)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def get(self, z, x, y, ext="png") -> Optional[bytes]:
        p = self.path(z, x, y, ext)
        return open(p, "rb").read() if os.path.exists(p) else None

    def manifest(self, levels=range(0, 23)) -> dict:
        counts = {}
        for z in levels:
            zd = os.path.join(self.root, str(z))
            if os.path.isdir(zd):
                counts[z] = sum(len(files) for _, _, files in os.walk(zd))
        return {"scheme": self.scheme, "tiles_per_level": counts,
                "total": sum(counts.values()), "version": VERSION}

    def verify_integrity(self, hashes: dict) -> dict:
        """hashes: {'z/x/y.ext': sha256hex} — контроль целостности при переносе (AUT-004)."""
        bad = []
        for key, h in hashes.items():
            p = os.path.join(self.root, key)
            if not os.path.exists(p):
                bad.append({"tile": key, "problem": "отсутствует"}); continue
            real = hashlib.sha256(open(p, "rb").read()).hexdigest()
            if real != h:
                bad.append({"tile": key, "problem": "хэш не совпал"})
        return {"ok": len(bad) == 0, "checked": len(hashes), "problems": bad}


def make_offline_manifest(store: TileStore) -> dict:
    """Манифест для контролируемого переноса в изолированный контур (AUT-004/005)."""
    hashes = {}
    for dirpath, _, files in os.walk(store.root):
        for fn in files:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, store.root)
            hashes[rel] = hashlib.sha256(open(full, "rb").read()).hexdigest()
    return {"generated": "авто", "scheme": store.scheme, "tile_hashes": hashes,
            "count": len(hashes),
            "procedure": "перенос только через разрешённый носитель по утверждённой процедуре; сетевого моста не возникает (AUT-005)"}


def download_script_note():
    return ("Загрузчик тайлов выполняется ВНЕ изолированного контура (рабочее место с доступом к "
            "лицензированному источнику). Скачанный кэш + make_offline_manifest() переносятся в контур "
            "по процедуре AUT-004. В Системе загрузчик по умолчанию отключён (AUT-002/008).")


# ================== Классификатор условных знаков (RSC-профиль) ==================
class SignClassifier:
    """Классификатор: коды, семантика, приоритеты 0–255, границы видимости по масштабу,
    генерализация, локальные расширения без изменения ядра (§4.1.1 RSC, EQP-003-подобная расширяемость)."""

    def __init__(self):
        self.signs = {}     # code → sign
        self.semantics = {} # семантический код → описание
        self.extensions = []

    def add(self, code: str, name: str, kind: str, min_scale: int, max_scale: int,
            priority: int = 128, color: str = "#000000", semantics: dict = None):
        assert 0 <= priority <= 255, "приоритет отображения 0–255"
        self.signs[code] = {"code": code, "name": name, "kind": kind,
                            "min_scale": min_scale, "max_scale": max_scale,
                            "priority": priority, "color": color, "semantics": semantics or {}}
        for k, v in (semantics or {}).items():
            self.semantics.setdefault(k, []).append({"code": code, "value": v})

    def visible_at(self, scale_denom: int):
        """Знаки, видимые при знаменателе масштаба (1:scale_denom)."""
        return [s for s in self.signs.values() if s["min_scale"] <= scale_denom <= s["max_scale"]]

    def generalize(self, scale_denom: int):
        """Генерализация: отбор по видимости + приоритету (крупнее приоритет — важнее)."""
        vis = self.visible_at(scale_denom)
        vis.sort(key=lambda s: -s["priority"])
        return {"scale": scale_denom, "signs": [s["code"] for s in vis],
                "dropped": sorted(set(self.signs) - {s["code"] for s in vis})}

    def extend_local(self, pack: dict):
        """Локальное расширение без изменения ядра."""
        for s in pack.get("signs", []):
            self.add(**s)
        self.extensions.append({"date": "auto", "count": len(pack.get("signs", []))})

    def export_json(self) -> str:
        return json.dumps({"version": VERSION, "signs": self.signs, "semantics": self.semantics,
                           "extensions": self.extensions}, ensure_ascii=False)

    @staticmethod
    def import_json(text: str) -> "SignClassifier":
        d = json.loads(text)
        c = SignClassifier()
        c.signs = d["signs"]; c.semantics = d.get("semantics", {}); c.extensions = d.get("extensions", [])
        return c


def default_classifier() -> SignClassifier:
    """Базовый набор (профиль оперативной обстановки + топооснова). Расширяется локально."""
    c = SignClassifier()
    base = [
        ("10001", "Населённый пункт (город)", "area", 1, 1_000_000, 200, "#c0392b"),
        ("10002", "Населённый пункт (посёлок)", "area", 1, 200_000, 180, "#c0392b"),
        ("20001", "Дорога магистральная", "line", 1, 500_000, 170, "#7f8c8d"),
        ("20002", "Дорога грунтовая", "line", 1, 50_000, 90, "#95a5a6"),
        ("30001", "Река", "line", 1, 1_000_000, 160, "#2980b9"),
        ("40001", "Узел связи (КП)", "point", 1, 1_000_000, 250, "#8e44ad"),
        ("40002", "Радиорелейная станция", "point", 1, 200_000, 220, "#8e44ad"),
        ("40003", "Антенна секторная", "point", 1, 25_000, 150, "#9b59b6"),
        ("40004", "Линия связи ВОЛС", "line", 1, 500_000, 140, "#27ae60"),
        ("40005", "Линия связи радио", "line", 1, 200_000, 130, "#2ecc71"),
        ("50001", "Горизонталь основная", "line", 1, 25_000, 60, "#a0522d"),
        ("50002", "Отметка высоты", "point", 1, 50_000, 80, "#a0522d"),
    ]
    for code, name, kind, smin, smax, pri, colr in base:
        c.add(code, name, kind, smin, smax, pri, colr)
    c.signs["40001"]["semantics"] = {"тип": "КП", "состояние": None}
    return c
