"""ГИС-ядро: системы координат и трансформации (GIS-002/003, §4.1.1).

WGS84 / UTM / СК-42 (Красовского) / СК-95 / МСК (локальные, параметры задаются).
Преобразование выполняет ФАКТИЧЕСКИЙ пересчёт геометрии с записью операции и
параметров; смена только подписи CRS запрещена (GIS-002).
Точность Молоденского для пары WGS84↔СК-42 — единицы метров; финальные параметры
трансформации утверждаются реестром проекта (версионируется).
"""
import math
from dataclasses import dataclass, field
from typing import List, Tuple, Optional

VERSION = "1.0.0"

# --- эллипсоиды ---
WGS84 = {"a": 6378137.0, "f": 1 / 298.257223563}
KRASSOVSKY = {"a": 6378245.0, "f": 1 / 298.3}          # СК-42
GRS80 = {"a": 6378137.0, "f": 1 / 298.257222101}       # СК-95 ≈ GRS80

# параметры Молоденского WGS84 → СК-42 (Пулково-1942), опубликованные значения;
# подлежат уточнению по региональному реестру трансформаций (версионируется)
MOL_WGS84_TO_SK42 = {"dX": 23.57, "dY": -140.95, "dZ": -79.80}


def utm_zone(lon_deg: float) -> int:
    return int((lon_deg + 180) // 6) + 1


def _series_en(a, f):
    e2 = f * (2 - f)
    ep2 = e2 / (1 - e2)
    n = f / (2 - f)
    A0 = a * (1 - n + 5 / 4 * (n * n - n ** 3) + 81 / 64 * n ** 4)
    return e2, ep2, n, A0


def latlon_to_utm(lat: float, lon: float, zone: Optional[int] = None, north: Optional[bool] = None) -> dict:
    """WGS84 (градусы) → UTM (метры). Классические ряды Снайдера."""
    zone = zone or utm_zone(lon)
    north = (lat >= 0) if north is None else north
    a, f = WGS84["a"], WGS84["f"]
    e2, ep2, n, A0 = _series_en(a, f)
    k0 = 0.9996
    lon0 = math.radians((zone - 1) * 6 - 180 + 3)
    phi = math.radians(lat); lam = math.radians(lon)
    N = a / math.sqrt(1 - e2 * math.sin(phi) ** 2)
    T = math.tan(phi) ** 2
    C = ep2 * math.cos(phi) ** 2
    A = (lam - lon0) * math.cos(phi)
    M = a * ((1 - e2 / 4 - 3 * e2 ** 2 / 64 - 5 * e2 ** 3 / 256) * phi
             - (3 * e2 / 8 + 3 * e2 ** 2 / 32 + 45 * e2 ** 3 / 1024) * math.sin(2 * phi)
             + (15 * e2 ** 2 / 256 + 45 * e2 ** 3 / 1024) * math.sin(4 * phi)
             - (35 * e2 ** 3 / 3072) * math.sin(6 * phi))
    x = k0 * N * (A + (1 - T + C) * A ** 3 / 6 + (5 - 18 * T + T * T + 72 * C - 58 * ep2) * A ** 5 / 120) + 500000.0
    y = k0 * (M + N * math.tan(phi) * (A * A / 2 + (5 - T + 9 * C + 4 * C * C) * A ** 4 / 24
              + (61 - 58 * T + T * T + 600 * C - 330 * ep2) * A ** 6 / 720))
    if not north:
        y += 10000000.0
    return {"easting": x, "northing": y, "zone": zone, "north": north,
            "crs": f"UTM-{zone}{'N' if north else 'S'} (WGS84)", "operation": "latlon_to_utm v" + VERSION}


def utm_to_latlon(easting: float, northing: float, zone: int, north: bool = True) -> dict:
    """UTM (метры) → WGS84 (градусы), итеративное обращение."""
    a, f = WGS84["a"], WGS84["f"]
    e2 = f * (2 - f); ep2 = e2 / (1 - e2); k0 = 0.9996
    x = easting - 500000.0
    y = northing if north else northing - 10000000.0
    M = y / k0
    mu = M / (a * (1 - e2 / 4 - 3 * e2 ** 2 / 64 - 5 * e2 ** 3 / 256))
    e1 = (1 - math.sqrt(1 - e2)) / (1 + math.sqrt(1 - e2))
    phi1 = (mu + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu)
            + (21 * e1 ** 2 / 16 - 55 * e1 ** 3 / 32) * math.sin(4 * mu)
            + (151 * e1 ** 3 / 96) * math.sin(6 * mu))
    N1 = a / math.sqrt(1 - e2 * math.sin(phi1) ** 2)
    T1 = math.tan(phi1) ** 2
    C1 = ep2 * math.cos(phi1) ** 2
    R1 = a * (1 - e2) / (1 - e2 * math.sin(phi1) ** 2) ** 1.5
    D = x / (N1 * k0)
    lat = phi1 - (N1 * math.tan(phi1) / R1) * (
        D * D / 2 - (5 + 3 * T1 + 10 * C1 - 4 * C1 * C1 - 9 * ep2) * D ** 4 / 24
        + (61 + 90 * T1 + 298 * C1 + 45 * T1 * T1 - 252 * ep2 - 3 * C1 * C1) * D ** 6 / 720)
    lon = (D - (1 + 2 * T1 + C1) * D ** 3 / 6
           + (5 - 2 * C1 + 28 * T1 - 3 * C1 * C1 + 8 * ep2 + 24 * T1 * T1) * D ** 5 / 120) / math.cos(phi1)
    lon_deg = math.degrees(lon) + (zone - 1) * 6 - 180 + 3
    return {"lat": math.degrees(lat), "lon": lon_deg,
            "crs": "WGS84", "operation": f"utm_to_latlon v{VERSION} zone={zone}"}


def geodetic_to_ecef(lat, lon, h, ell):
    a, f = ell["a"], ell["f"]
    e2 = f * (2 - f)
    phi = math.radians(lat); lam = math.radians(lon)
    N = a / math.sqrt(1 - e2 * math.sin(phi) ** 2)
    return ((N + h) * math.cos(phi) * math.cos(lam),
            (N + h) * math.cos(phi) * math.sin(lam),
            (N * (1 - e2) + h) * math.sin(phi))


def ecef_to_geodetic(x, y, z, ell, iters=8):
    """Итеративное обращение (фиксированная точка, сходится за 3–5 итераций)."""
    a, f = ell["a"], ell["f"]
    e2 = f * (2 - f)
    p = math.sqrt(x * x + y * y)
    lam = math.atan2(y, x)
    phi = math.atan2(z, p * (1 - e2))
    for _ in range(iters):
        N = a / math.sqrt(1 - e2 * math.sin(phi) ** 2)
        h = p / math.cos(phi) - N
        phi = math.atan2(z, p * (1 - e2 * N / (N + h)))
    N = a / math.sqrt(1 - e2 * math.sin(phi) ** 2)
    h = p / math.cos(phi) - N
    return math.degrees(phi), math.degrees(lam), h


def datum_shift(lat, lon, h, src, dst, dX, dY, dZ):
    """3-параметрическая трансформация через ECEF (надёжнее рядов Молоденского при больших dZ).

    src/dst — эллипсоиды; dX,dY,dZ — сдвиг центра в метрах (src → dst).
    Точность — единицы метров; финальные параметры утверждаются региональным
    реестром трансформаций (версионируется, GIS-003).
    """
    x, y, z = geodetic_to_ecef(lat, lon, h, src)
    return ecef_to_geodetic(x + dX, y + dY, z + dZ, dst)


def wgs84_to_sk42(lat, lon, h=0.0):
    la, lo, hh = datum_shift(lat, lon, h, WGS84, KRASSOVSKY, **MOL_WGS84_TO_SK42)
    return {"lat": la, "lon": lo, "h": hh, "crs": "СК-42 (Пулково-1942, эллипсоид Красовского)",
            "operation": f"datum_shift_ecef {MOL_WGS84_TO_SK42} v{VERSION}",
            "accuracy_note": "единицы метров; финальные параметры — по региональному реестру трансформаций"}


def sk42_to_wgs84(lat, lon, h=0.0):
    d = {k: -v for k, v in MOL_WGS84_TO_SK42.items()}
    la, lo, hh = datum_shift(lat, lon, h, KRASSOVSKY, WGS84, **d)
    return {"lat": la, "lon": lo, "h": hh, "crs": "WGS84", "operation": f"datum_shift_ecef_inv {d} v{VERSION}"}


# --- реестр проекций и библиотека трансформаций (GIS-003, версионируется) ---
PROJECTION_REGISTRY = {
    "WGS84": {"type": "geodetic", "ellipsoid": "WGS84"},
    "SK42": {"type": "geodetic", "ellipsoid": "Krassovsky"},
    "SK95": {"type": "geodetic", "ellipsoid": "GRS80"},
    "UTM": {"type": "projected", "base": "WGS84", "zones": "1–60"},
    "EPSG:3857": {"type": "projected", "base": "WGS84", "note": "веб-меркатор (тайлы)"},
    "MSK-50": {"type": "local", "params_ref": "региональный реестр (задаётся заказчиком)"},
}

TRANSFORM_REGISTRY = [
    {"id": "T-001", "from": "WGS84", "to": "UTM", "method": "гаусс-крюгер (Снайдер)", "version": VERSION},
    {"id": "T-002", "from": "WGS84", "to": "SK42", "method": "3-параметрическая через ECEF", "params": MOL_WGS84_TO_SK42, "version": VERSION,
     "accuracy": "единицы м (требует уточнения по региональному реестру)"},
    {"id": "T-003", "from": "SK42", "to": "WGS84", "method": "3-параметрическая через ECEF (обратная)", "version": VERSION},
    {"id": "T-004", "from": "WGS84", "to": "EPSG:3857", "method": "веб-меркатор", "version": VERSION},
]


def latlon_to_web_mercator(lat, lon):
    R = 6378137.0
    x = R * math.radians(lon)
    y = R * math.log(math.tan(math.pi / 4 + math.radians(max(min(lat, 85.05), -85.05)) / 2))
    return {"x": x, "y": y, "crs": "EPSG:3857", "operation": "web_mercator v" + VERSION}


def transform_geometry(coords: List[Tuple[float, float]], src_crs: str, dst_crs: str) -> dict:
    """Пакетная трансформация с ОБЯЗАТЕЛЬНОЙ записью операции (GIS-002)."""
    if src_crs == dst_crs:
        return {"coords": coords, "operation": "identity", "crs": dst_crs,
                "note": "CRS совпадают — пересчёт не требуется"}
    if src_crs == "WGS84" and dst_crs.startswith("UTM"):
        zone = int(dst_crs.split("-")[1].rstrip("NS")) if "-" in dst_crs else None
        out = [latlon_to_utm(la, lo, zone) for la, lo in coords]
        return {"coords": [(o["easting"], o["northing"]) for o in out], "crs": out[0]["crs"],
                "operation": out[0]["operation"], "zone": out[0]["zone"]}
    if src_crs == "WGS84" and dst_crs == "SK42":
        out = [wgs84_to_sk42(la, lo) for la, lo in coords]
        return {"coords": [(o["lat"], o["lon"]) for o in out], "crs": "SK42", "operation": out[0]["operation"]}
    if src_crs == "SK42" and dst_crs == "WGS84":
        out = [sk42_to_wgs84(la, lo) for la, lo in coords]
        return {"coords": [(o["lat"], o["lon"]) for o in out], "crs": "WGS84", "operation": out[0]["operation"]}
    if src_crs == "WGS84" and dst_crs == "EPSG:3857":
        out = [latlon_to_web_mercator(la, lo) for la, lo in coords]
        return {"coords": [(o["x"], o["y"]) for o in out], "crs": "EPSG:3857", "operation": out[0]["operation"]}
    raise ValueError(f"трансформация {src_crs}→{dst_crs}: маршрут не зарегистрирован в библиотеке TRANSFORM_REGISTRY — требуется явная настройка (запрещено менять только подпись CRS)")
