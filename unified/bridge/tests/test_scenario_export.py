"""Тесты экспорта обстановки платформы (scenario_export, UNIF-18) — офлайн, без сервера."""
import json
import os
import sys
import unittest

BRIDGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BRIDGE)
REPO = os.path.dirname(os.path.dirname(BRIDGE))
sys.path.insert(0, os.path.join(REPO, "afina", "src"))

from afina.gis.crs import latlon_to_utm  # noqa: E402  (прямой ход для подготовки входа)
from datamodel_map import MappingError  # noqa: E402
from scenario_export import export_entities, export_request  # noqa: E402


class FakeClient:
    def __init__(self, rows):
        self._rows = rows

    def list_objects(self):
        return self._rows


def com(oid, x, y, **attrs):
    return {"id": oid, "class": "COM", "name": oid, "x": x, "y": y, "attrs": attrs}


class TestScenarioExport(unittest.TestCase):
    def test_degrees_passthrough(self):
        rows = [com("n1", 37.62, 55.75, node_type="cp",
                    unit={"inventoryId": "У1", "model": "П-240Б", "techState": "исправна"})]
        ent = export_entities(FakeClient(rows))
        self.assertEqual(ent["geo_objects"][0]["lat"], 55.75)
        self.assertEqual(ent["geo_objects"][0]["lng"], 37.62)
        self.assertEqual(ent["conversions"], [])

    def test_utm_roundtrip_via_line_a_gis(self):
        # прямой ход ГИС-ядра линии A → метры UTM-37N; экспорт обязан вернуть градусы
        utm = latlon_to_utm(55.75, 37.62)
        rows = [com("n1", utm["easting"], utm["northing"], node_type="cp", crs="UTM-37N")]
        ent = export_entities(FakeClient(rows))
        g = ent["geo_objects"][0]
        self.assertAlmostEqual(g["lat"], 55.75, places=6)
        self.assertAlmostEqual(g["lng"], 37.62, places=6)
        self.assertEqual(len(ent["conversions"]), 1)
        self.assertIn("UTM-37N", ent["conversions"][0])
        self.assertEqual(ent["warnings"], [])

    def test_meters_without_crs_skipped_honestly(self):
        rows = [com("n1", 413380.7, 6179118.1, node_type="cp")]   # метры, crs не объявлен
        ent = export_entities(FakeClient(rows))
        self.assertEqual(ent["geo_objects"], [])
        self.assertEqual(len(ent["warnings"]), 1)
        self.assertIn("ENG-004", ent["warnings"][0])   # молчаливая интерпретация запрещена

    def test_southern_hemisphere(self):
        utm = latlon_to_utm(-33.86, 151.21)             # Сидней → UTM-56S
        rows = [com("s1", utm["easting"], utm["northing"], node_type="radio",
                    crs=f"UTM-{utm['zone']}{'N' if utm['north'] else 'S'}")]
        ent = export_entities(FakeClient(rows))
        g = ent["geo_objects"][0]
        self.assertAlmostEqual(g["lat"], -33.86, places=6)
        self.assertAlmostEqual(g["lng"], 151.21, places=6)

    def test_double_encoded_attrs_legacy(self):
        row = {"id": "n1", "class": "COM", "name": "n1", "x": 37.6, "y": 55.7,
               "attrs": json.dumps(json.dumps({"node_type": "relay"}))}  # legacy: строка в строке
        ent = export_entities(FakeClient([row]))
        self.assertEqual(ent["geo_objects"][0]["type"], "relay")

    def test_ts_code_fallback(self):
        # techState не задан в attrs → берётся ts_code строки (классификатор Прил. В)
        rows = [{"id": "n1", "class": "COM", "name": "n1", "x": 37.6, "y": 55.7, "ts_code": "TS-4",
                 "attrs": {"node_type": "radio", "unit": {"inventoryId": "У9", "model": "Р-168"}}}]
        ent = export_entities(FakeClient(rows))
        self.assertEqual(ent["assets"][0]["techState"], "неисправна")

    def test_full_export_builds_valid_request(self):
        rows = [
            com("kp", 37.62, 55.75, node_type="cp", org_unit="u1",
                unit={"inventoryId": "КП-1", "model": "П-240Б", "techState": "исправна",
                      "cost": 30, "deployTimeMin": 45}),
            com("r1", 37.72, 55.80, node_type="radio", org_unit="u1",
                unit={"inventoryId": "РС-1", "model": "Р-168", "techState": "исправна",
                      "cost": 10, "deployTimeMin": 15}),
            {"id": "l1", "class": "LINK", "name": "l1", "x": 0, "y": 0,
             "attrs": {"a": "kp", "b": "r1", "link_type": "VHF"}},
            {"id": "d1", "class": "ORDER", "name": "d1", "x": 0, "y": 0,
             "attrs": {"src": "kp", "dst": "r1", "rate_kbps": 16, "priority": 1}},
            {"id": "u1", "class": "ORG", "name": "батальон", "x": 0, "y": 0,
             "attrs": {"org_id": "u1", "path": "батальон", "node_id": "kp"}},
            {"id": "doc1", "class": "DOC", "name": "посторонний", "x": 0, "y": 0, "attrs": {}},
        ]
        res = export_request(FakeClient(rows))
        self.assertEqual(res["counts"], {"assets": 2, "nodes": 2, "links": 1, "demands": 1})
        req = res["request"]
        by_id = {n["id"]: n for n in req["topology"]["nodes"]}
        self.assertEqual(by_id["r1"]["parentId"], "kp")     # ОШС через узел-представитель
        self.assertNotIn("parentId", by_id["kp"])           # самоссылка исключена
        self.assertEqual(req["demands"][0]["rateKbps"], 16.0)
        self.assertTrue(any("DOC" in w for w in res["warnings"]))  # посторонний класс — честно

    def test_broken_order_ref_raises(self):
        rows = [com("kp", 37.62, 55.75, node_type="cp"),
                {"id": "d1", "class": "ORDER", "name": "d1", "x": 0, "y": 0,
                 "attrs": {"src": "kp", "dst": "нет-такого", "rate_kbps": 8}}]
        with self.assertRaises(MappingError):
            export_request(FakeClient(rows))


if __name__ == "__main__":
    unittest.main()
