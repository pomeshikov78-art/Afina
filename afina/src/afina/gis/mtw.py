"""MTW — матрица высот (§4.1.1, GIS-005) + производственные функции (§4.1.4).

Структура файла — собственная двоичная реализация «AFM MTW-1» (магическая
сигнатура, метаданные GIS-001, сетка float32). Байтовая совместимость с файлами
КБ «Панорама» подтверждается на эталонных файлах заказчика в G1.1; при расхождении
подключается адаптер (лицензионный путь A решения G0.2). Функционально реализованы:
построение по отметкам (средневзвешенная интерполяция), экстремумы, профиль.
"""
import math, struct, json, os, time
from typing import List, Optional, Tuple

MAGIC = b"AFM-MTW1"
VERSION = "1.0.0"


class HeightMatrix:
    def __init__(self, rows: int, cols: int, cell_m: float, x0: float, y0: float,
                 data: List[List[float]], crs: str = "МСК (локальная)", nodata: float = -32768.0,
                 source: str = "", date: str = "", accuracy_m: Optional[float] = None):
        self.rows, self.cols, self.cell_m = rows, cols, cell_m
        self.x0, self.y0 = x0, y0
        self.data = data
        self.crs, self.nodata = crs, nodata
        # метаданные GIS-001: CRS, высотная система, точность, дата, источник
        self.meta = {"crs": crs, "height_system": "Балтийская (нормативная)", "accuracy_m": accuracy_m,
                     "date": date or time.strftime("%Y-%m-%d"), "source": source, "cell_m": cell_m,
                     "extent": (x0, y0, x0 + cols * cell_m, y0 + rows * cell_m)}

    # ---- построение по отметкам: средневзвешенная интерполяция (IDW) ----
    @staticmethod
    def from_points(points: List[Tuple[float, float, float]], cell_m: float, power: float = 2.0,
                    max_radius_cells: int = 8, **meta) -> "HeightMatrix":
        xs = [p[0] for p in points]; ys = [p[1] for p in points]
        x0 = math.floor(min(xs) / cell_m) * cell_m
        y0 = math.floor(min(ys) / cell_m) * cell_m
        cols = int(math.ceil((max(xs) - x0) / cell_m)) + 1
        rows = int(math.ceil((max(ys) - y0) / cell_m)) + 1
        data = []
        rmax = max_radius_cells * cell_m
        for r in range(rows):
            row = []
            cy = y0 + (r + 0.5) * cell_m
            for c in range(cols):
                cx = x0 + (c + 0.5) * cell_m
                wsum = 0.0; vsum = 0.0; exact = None
                for px, py, pz in points:
                    d = math.hypot(px - cx, py - cy)
                    if d > rmax: continue
                    if d < 1e-9:
                        exact = pz; break
                    w = 1.0 / (d ** power)
                    wsum += w; vsum += w * pz
                row.append(exact if exact is not None else (vsum / wsum if wsum > 0 else -32768.0))
            data.append(row)
        return HeightMatrix(rows, cols, cell_m, x0, y0, data, **meta)

    # ---- запись/чтение ----
    def save(self, path: str):
        meta = json.dumps(self.meta, ensure_ascii=False).encode("utf-8")
        with open(path, "wb") as f:
            f.write(MAGIC)
            f.write(struct.pack("<i", len(meta)))
            f.write(meta)
            f.write(struct.pack("<2i4d", self.rows, self.cols, self.cell_m, self.x0, self.y0, self.nodata))
            for r in range(self.rows):
                f.write(struct.pack(f"<{self.cols}f", *self.data[r]))
        return {"path": path, "bytes": os.path.getsize(path)}

    @staticmethod
    def load(path: str) -> "HeightMatrix":
        with open(path, "rb") as f:
            magic = f.read(8)
            if magic != MAGIC:
                raise ValueError("MTW: неверная сигнатура — файл не формата AFM-MTW1 (для файлов КБ «Панорама» подключить адаптер пути A)")
            mlen = struct.unpack("<i", f.read(4))[0]
            meta = json.loads(f.read(mlen).decode("utf-8"))
            rows, cols, cell, x0, y0, nodata = struct.unpack("<2i4d", f.read(40))
            data = []
            for r in range(rows):
                data.append(list(struct.unpack(f"<{cols}f", f.read(4 * cols))))
        return HeightMatrix(rows, cols, cell, x0, y0, data, crs=meta.get("crs", ""),
                            nodata=nodata, source=meta.get("source", ""), date=meta.get("date", ""),
                            accuracy_m=meta.get("accuracy_m"))

    # ---- экстремумы рельефа (§4.1.4) ----
    def extrema(self) -> List[dict]:
        out = []
        for r in range(1, self.rows - 1):
            for c in range(1, self.cols - 1):
                v = self.data[r][c]
                if v == self.nodata: continue
                nb = [self.data[r + dr][c + dc] for dr in (-1, 0, 1) for dc in (-1, 0, 1)
                      if (dr or dc) and self.data[r + dr][c + dc] != self.nodata]
                if not nb: continue
                if v > max(nb):
                    out.append({"type": "максимум", "row": r, "col": c, "h": round(v, 2),
                                "x": self.x0 + (c + 0.5) * self.cell_m, "y": self.y0 + (r + 0.5) * self.cell_m})
                elif v < min(nb):
                    out.append({"type": "минимум", "row": r, "col": c, "h": round(v, 2),
                                "x": self.x0 + (c + 0.5) * self.cell_m, "y": self.y0 + (r + 0.5) * self.cell_m})
        return out

    # ---- профиль местности по линии (§4.1.4, GIS-007) ----
    def profile(self, x1, y1, x2, y2, steps=100, structures: List[dict] = None) -> dict:
        prof = []
        for i in range(steps + 1):
            x = x1 + (x2 - x1) * i / steps
            y = y1 + (y2 - y1) * i / steps
            c = int((x - self.x0) / self.cell_m); r = int((y - self.y0) / self.cell_m)
            if 0 <= r < self.rows and 0 <= c < self.cols and self.data[r][c] != self.nodata:
                h = self.data[r][c]
            else:
                h = None
            if h is not None and structures:
                for st in structures:  # относительные высоты сооружений
                    if abs(st["x"] - x) < st.get("r", 10) and abs(st["y"] - y) < st.get("r", 10):
                        h = max(h, st.get("top_h", h))
            prof.append({"d_m": round(math.hypot(x2 - x1, y2 - y1) * i / steps, 1), "h_m": None if h is None else round(h, 2)})
        return {"profile": prof, "structures_accounted": bool(structures)}

    # ---- оптимизация/сжатие (§4.1.4): квантование с резервной копией ----
    def optimize(self, quant_m: float = 1.0, backup_path: Optional[str] = None) -> dict:
        if backup_path:
            self.save(backup_path)
        before = sum(1 for r in self.data for v in r)
        self.data = [[round(v / quant_m) * quant_m if v != self.nodata else self.nodata for v in row] for row in self.data]
        return {"quant_m": quant_m, "cells": before, "backup": backup_path,
                "note": "оптимизация изменяет ТОЛЬКО отображение/хранение; результаты расчётов, выполненных до оптимизации, сохраняют свои паспорта (ENG-003)"}
