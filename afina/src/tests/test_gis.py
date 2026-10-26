"""Тесты ГИС-ядра (GIS-001…005, MAT-контрольные точки)."""
import json, math, os, sys, tempfile, unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from afina.gis import crs, io_formats, roundtrip
from afina.gis.mtw import HeightMatrix
from afina.gis.tiles_rsc import TileStore, latlon_to_tile, tile_bounds, default_classifier, make_offline_manifest


class CRSTests(unittest.TestCase):
    def test_utm_roundtrip(self):
        for lat, lon in [(55.7522, 37.6156), (43.12, 131.9), (-33.86, 151.2)]:
            u = crs.latlon_to_utm(lat, lon)
            b = crs.utm_to_latlon(u["easting"], u["northing"], u["zone"], u["north"])
            self.assertAlmostEqual(b["lat"], lat, places=6)
            self.assertAlmostEqual(b["lon"], lon, places=6)

    def test_utm_known_zone(self):
        u = crs.latlon_to_utm(55.7522, 37.6156)
        self.assertEqual(u["zone"], 37)
        self.assertTrue(u["north"])

    def test_molodensky_small_shift(self):
        r = crs.wgs84_to_sk42(55.7522, 37.6156)
        dlat = abs(r["lat"] - 55.7522) * 111320
        dlon = abs(r["lon"] - 37.6156) * 111320 * math.cos(math.radians(55.75))
        self.assertLess(math.hypot(dlat, dlon), 200)   # единицы-десятки метров
        self.assertGreater(math.hypot(dlat, dlon), 0)  # пересчёт ФАКТИЧЕСКИЙ (GIS-002)
        self.assertIn("datum_shift", r["operation"])  # операция записана (GIS-002)

    def test_unregistered_route_blocked(self):
        with self.assertRaises(ValueError):
            crs.transform_geometry([(55, 37)], "MSK-50", "WGS84")  # маршрут не зарегистрирован — блок, а не смена подписи

    def test_transform_records_operation(self):
        r = crs.transform_geometry([(55.7522, 37.6156)], "WGS84", "UTM-37N")
        self.assertIn("operation", r)


class FormatTests(unittest.TestCase):
    FEATURES = [
        {"type": "Feature", "id": "f-1", "properties": {"name": "КП Заря", "мощность": 50, "состояние": "TS-1"},
         "geometry": {"type": "Point", "coordinates": [37.6156, 55.7522, 0]}},
        {"type": "Feature", "id": "f-2", "properties": {"name": "Линия ВОЛС"},
         "geometry": {"type": "LineString", "coordinates": [[37.6, 55.75, 0], [37.62, 55.76, 0], [37.63, 55.755, 0]]}},
    ]

    def test_geojson_roundtrip_zero_loss(self):
        txt = io_formats.geojson_export(self.FEATURES)
        back = io_formats.geojson_import(txt)
        proto = roundtrip.compare_datasets(self.FEATURES, back)
        self.assertEqual(proto["unexplained_losses"], 0, proto)

    def test_kml_roundtrip(self):
        txt = io_formats.kml_export(self.FEATURES)
        back = io_formats.kml_import(txt)
        self.assertEqual(len(back), 2)
        self.assertAlmostEqual(back[0]["geometry"]["coordinates"][0], 37.6156, places=6)

    def test_shapefile_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            base = os.path.join(td, "test")
            io_formats.shapefile_export(base, self.FEATURES[:1])  # Point
            back = io_formats.shapefile_import(base)
            self.assertEqual(len(back), 1)
            c = back[0]["geometry"]["coordinates"]
            self.assertAlmostEqual(c[0], 37.6156, places=9)
            self.assertAlmostEqual(c[1], 55.7522, places=9)
            self.assertEqual(back[0]["properties"].get("name"), "КП Заря")
            # LineString
            base2 = os.path.join(td, "lines")
            io_formats.shapefile_export(base2, [self.FEATURES[1]])
            back2 = io_formats.shapefile_import(base2)
            self.assertEqual(back2[0]["geometry"]["type"], "LineString")
            self.assertEqual(len(back2[0]["geometry"]["coordinates"]), 3)

    def test_loss_detected_when_dropped(self):
        damaged = [dict(self.FEATURES[0])]
        damaged[0] = {**damaged[0], "properties": {"name": "КП Заря"}}  # потеря 2 атрибутов
        proto = roundtrip.compare_datasets(self.FEATURES[:1], damaged)
        self.assertEqual(len(proto["attribute_losses"]), 2)
        self.assertEqual(proto["verdict"], "FAIL")


class MTWTests(unittest.TestCase):
    def test_build_and_roundtrip(self):
        pts = [(0, 0, 100), (500, 0, 150), (0, 500, 120), (500, 500, 200), (250, 250, 170)]
        hm = HeightMatrix.from_points(pts, cell_m=50, source="учебный набор", accuracy_m=2.0)
        self.assertGreater(hm.rows, 5)
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "relief.mtw")
            hm.save(p)
            hm2 = HeightMatrix.load(p)
            self.assertEqual((hm2.rows, hm2.cols), (hm.rows, hm.cols))
            self.assertAlmostEqual(hm2.data[5][5], hm.data[5][5], places=3)
            self.assertEqual(hm2.meta["source"], "учебный набор")   # GIS-001: метаданные сохранены
            self.assertEqual(hm2.meta["accuracy_m"], 2.0)

    def test_extrema(self):
        data = [[10, 10, 10], [10, 55, 10], [10, 10, 10]]
        hm = HeightMatrix(3, 3, 100, 0, 0, data)
        ex = hm.extrema()
        self.assertTrue(any(e["type"] == "максимум" and e["h"] == 55 for e in ex))

    def test_profile_with_structures(self):
        data = [[100.0] * 10 for _ in range(10)]
        hm = HeightMatrix(10, 10, 100, 0, 0, data)
        pr = hm.profile(50, 50, 850, 850, steps=8, structures=[{"x": 450, "y": 450, "r": 60, "top_h": 130}])
        hs = [p["h_m"] for p in pr["profile"]]
        self.assertEqual(max(hs), 130)  # относительная высота сооружения учтена


class TilesRSCTests(unittest.TestCase):
    def test_tile_math(self):
        x, y = latlon_to_tile(55.7522, 37.6156, 12)
        b = tile_bounds(x, y, 12)
        self.assertTrue(b[0] <= 55.7522 <= b[2])
        self.assertTrue(b[1] <= 37.6156 <= b[3])

    def test_store_and_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            st = TileStore(td)
            st.put(12, 2358, 1126, b"fake-tile-bytes")
            self.assertEqual(st.get(12, 2358, 1126), b"fake-tile-bytes")
            m = st.manifest(levels=range(10, 14))
            self.assertEqual(m["total"], 1)
            man = make_offline_manifest(st)
            self.assertEqual(man["count"], 1)
            v = st.verify_integrity(man["tile_hashes"])
            self.assertTrue(v["ok"])
            v2 = st.verify_integrity({"12/2358/1126.png": "0" * 64})
            self.assertFalse(v2["ok"])  # подмена обнаружена

    def test_classifier_generalization(self):
        c = default_classifier()
        g_small = c.generalize(1_000_000)     # мелкий масштаб
        g_large = c.generalize(25_000)        # крупный масштаб
        self.assertLess(len(g_small["signs"]), len(g_large["signs"]))
        self.assertIn("40001", g_small["signs"])      # КП виден на 1:1 000 000
        self.assertNotIn("50001", g_small["signs"])   # горизонтали отброшены (генерализация)
        self.assertIn("50001", g_large["signs"])
        # локальное расширение без изменения ядра
        c.extend_local({"signs": [{"code": "90001", "name": "Локальный знак заказчика", "kind": "point",
                                   "min_scale": 1, "max_scale": 100_000, "priority": 240}]})
        self.assertIn("90001", c.generalize(50_000)["signs"])
        txt = c.export_json()
        c2 = type(c).import_json(txt)
        self.assertIn("90001", c2.signs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
