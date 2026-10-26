"""Тесты маппинга единой модели данных (Прил. Г ⇄ контракт ENG6-PLAN)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datamodel_map import (MappingError, asset_to_fleet_item, build_plan_request,  # noqa: E402
                           geo_object_to_node, haversine_km, pick_link_type,
                           plan_to_platform_tables, tech_state_to_condition)

ASSETS = [
    {"inventoryId": "Р-168-001", "model": "Р-168-100У", "nodeType": "radio", "techState": "исправна",
     "cost": 10, "deployTimeMin": 20},
    {"inventoryId": "Р-419-002", "model": "Р-419Л1", "nodeType": "relay", "techState": "ограниченно пригодна",
     "cost": 20, "deployTimeMin": 40},
    {"inventoryId": "УШБ-003", "model": "П-240Б", "nodeType": "cp", "techState": "неисправна"},
]
ORGS = {"u-1": {"path": "батальон/рота1", "nodeId": "kp"}, "u-2": {"path": "батальон/рота2"}}
GEO = [
    {"id": "kp", "type": "cp", "lat": 56.1234567891, "lng": 40.987654321, "orgUnitId": "u-1"},
    {"id": "r1", "type": "relay", "lat": 56.2, "lng": 41.1, "unitId": "Р-419-002", "orgUnitId": "u-1"},
    {"id": "r2", "type": "radio", "lat": 56.3, "lng": 41.3, "unitId": "Р-168-001", "orgUnitId": "u-2"},
]
RELS = [{"id": "l1", "a": "kp", "b": "r1"}, {"id": "l2", "a": "r1", "b": "r2"}]
ORDERS = [{"id": "d1", "src": "kp", "dst": "r2", "rateKbps": 64, "priority": 1}]


class TestForwardMapping(unittest.TestCase):
    def test_tech_state_classifier(self):
        # FR-RES-03: 🟢 исправна → green, 🟡 ограниченно → yellow, 🔴 неисправна → red
        self.assertEqual(tech_state_to_condition("исправна"), "green")
        self.assertEqual(tech_state_to_condition("ограниченно пригодна"), "yellow")
        self.assertEqual(tech_state_to_condition("неисправна"), "red")
        self.assertEqual(tech_state_to_condition(None), "green")
        with self.assertRaises(MappingError) as c:
            tech_state_to_condition("сломана немного")
        self.assertIn("ENUM", str(c.exception))

    def test_asset_to_fleet(self):
        f = asset_to_fleet_item(ASSETS[0])
        self.assertEqual(f, {"unitId": "Р-168-001", "kind": "Р-168-100У", "nodeType": "radio",
                             "condition": "green", "costDeploy": 10.0, "deployTimeMin": 20.0})
        with self.assertRaises(MappingError):
            asset_to_fleet_item({"inventoryId": "x"})  # REQUIRED: model
        with self.assertRaises(MappingError) as c:
            asset_to_fleet_item({"inventoryId": "x", "model": "m", "nodeType": "вертолёт"})
        self.assertIn("ENUM", str(c.exception))

    def test_node_coordinates_round6_and_range(self):
        n = geo_object_to_node(GEO[0], ORGS)
        self.assertEqual(n["lat"], 56.123457)          # NFR-03: 6 знаков
        self.assertNotIn("parentId", n)                # КП сам представляет свой ORG_UNIT — самоссылка исключена
        self.assertEqual(n["status"], "active")
        n1 = geo_object_to_node(GEO[1], ORGS)
        self.assertEqual(n1["parentId"], "kp")         # ОШС: иерархия узлов (рота-1 → узел КП)
        n2 = geo_object_to_node(GEO[2], ORGS)
        self.assertNotIn("parentId", n2)               # u-2 без узла-представителя — остаётся в слое платформы
        with self.assertRaises(MappingError) as c:
            geo_object_to_node({"id": "x", "lat": 999, "lng": 0})
        self.assertIn("RANGE", str(c.exception))
        with self.assertRaises(MappingError) as c:
            geo_object_to_node({"id": "x", "lat": 1, "lng": 1, "orgUnitId": "нет-такого"}, ORGS)
        self.assertIn("REF", str(c.exception))
        # явный parentId вне состава узлов → REF на сборке
        with self.assertRaises(MappingError) as c:
            build_plan_request([], [{"id": "a", "lat": 1, "lng": 1, "parentId": "призрак"}], [], [])
        self.assertIn("REF", str(c.exception))

    def test_link_type_by_range(self):
        self.assertEqual(pick_link_type(5), "FOC")
        self.assertEqual(pick_link_type(20), "VHF")
        self.assertEqual(pick_link_type(40), "UHF")
        self.assertEqual(pick_link_type(80), "RRL")
        self.assertEqual(pick_link_type(500), "SAT")
        self.assertEqual(pick_link_type(500, "HF"), "HF")

    def test_full_request_assembly(self):
        req = build_plan_request(ASSETS, GEO, RELS, ORDERS, ORGS,
                                 constraints={"budget": 100},
                                 environment={"clutterType": "open", "terrain": {"source": "flat"}})
        self.assertEqual(len(req["topology"]["nodes"]), 3)
        self.assertEqual(len(req["topology"]["links"]), 2)
        self.assertEqual(len(req["fleet"]), 3)
        self.assertEqual(req["demands"][0]["rateKbps"], 64.0)
        self.assertEqual(req["demands"][0]["from"], "kp")
        # l1: kp→r1 ≈ 11 км → FOC (ВОЛС ≤ 12 км); l2: r1→r2 ≈ 16.6 км → VHF (≤ 25)
        links = {l["id"]: l for l in req["topology"]["links"]}
        self.assertEqual(links["l1"]["type"], "FOC")
        self.assertEqual(links["l2"]["type"], "VHF")
        self.assertEqual(links["l1"]["from"], "kp")
        self.assertEqual(links["l1"]["to"], "r1")
        # битая заявка → REF
        with self.assertRaises(MappingError) as c:
            build_plan_request([], GEO, RELS, [{"id": "d", "src": "kp", "dst": "призрак", "rateKbps": 8}])
        self.assertIn("REF", str(c.exception))
        # узел ссылается на несуществующий ASSET → REF
        with self.assertRaises(MappingError) as c:
            build_plan_request(ASSETS, GEO + [{"id": "x", "lat": 1, "lng": 1, "unitId": "НЕТ-000"}],
                               RELS, ORDERS)
        self.assertIn("REF", str(c.exception))

    def test_haversine_matches_eng6_constant(self):
        # R=6371 км; 1° широты ≈ 111.195 км
        d = haversine_km(56.0, 41.0, 57.0, 41.0)
        self.assertAlmostEqual(d, 111.195, delta=0.01)


class TestReverseMapping(unittest.TestCase):
    RESP = {
        "status": "PARTIAL",
        "core": {"engineVersion": "5.1.0", "engineSha256": "adb610c9" + "0" * 56, "planner": "1.0.0"},
        "timings": {"totalMs": 12.3},
        "metrics": {"grade": "B"},
        "plan": {
            "deployedUnits": [{"nodeId": "r2", "nodeType": "radio", "unitId": "Р-168-001",
                               "condition": "green", "lat": 56.3, "lng": 41.3, "cost": 10,
                               "wave": 1, "startMin": 0}],
            "activeLinks": [{"id": "l2", "from": "r1", "to": "r2", "type": "VHF", "distKm": 16.6,
                             "marginDb": 12.0, "capacityKbps": 100, "loadKbps": 64, "utilPct": 64,
                             "delayMs": 5.1, "availabilityPct": 99.0, "cost": 0, "role": "primary"}],
            "rejectedLinks": [{"id": "l9", "from": "a", "to": "b", "type": "UHF",
                               "code": "BELOW_MARGIN", "reason": "запас ниже порога", "marginDb": -3}],
            "routes": [{"demandId": "d1", "status": "ROUTED", "grantedKbps": 64, "deficitKbps": 0,
                        "delayMs": 5.1, "hops": 2, "reason": None, "path": ["kp", "r1", "r2"],
                        "links": ["l1", "l2"]}],
            "schedule": {"makespanMin": 60.0, "crews": 2, "crewUtilPct": 55.0,
                         "criticalPath": [{"id": "kp"}, {"id": "r1"}],
                         "waves": [{"index": 1, "tStartMin": 0, "tEndMin": 60, "durationMin": 60,
                                    "crewsUsed": 2,
                                    "items": [{"nodeId": "kp", "unitId": "УШБ-003", "crew": 1,
                                               "startMin": 0, "endMin": 45},
                                              {"nodeId": "r1", "unitId": "Р-419-002", "crew": 2,
                                               "startMin": 10, "endMin": 60}]}]},
            "survivability": {"spofCount": 1},
        },
        "diagnostics": {"issues": [{"sev": "warn", "code": "BRIDGE_LINK", "message": "мост"}]},
    }

    def test_tables_match_path_a_shape(self):
        t = plan_to_platform_tables(self.RESP, run_id="CALC-2026-TEST")
        self.assertEqual(len(t["map_object"]), 1)
        self.assertEqual(t["map_object"][0]["object_id"], "r2")
        self.assertEqual(t["link_line"][0]["line_id"], "l2")
        self.assertEqual(t["link_line"][0]["capacity_kbps"], 100)
        self.assertEqual(t["link_rejected"][0]["code"], "BELOW_MARGIN")
        self.assertEqual(t["route"][0]["status"], "ROUTED")
        self.assertEqual(t["route"][0]["path_nodes"], ["kp", "r1", "r2"])
        self.assertEqual(len(t["deployment_wave"][0]["items"]), 2)
        self.assertEqual(t["schedule_summary"][0]["makespan_min"], 60.0)
        self.assertEqual(t["schedule_summary"][0]["critical_path"], ["kp", "r1"])
        self.assertEqual(t["survivability"][0]["spofCount"], 1)
        self.assertEqual(t["calc_run"][0]["run_id"], "CALC-2026-TEST")
        self.assertIn("ENG6-JS 5.1.0", t["calc_run"][0]["backend"])

    def test_invalid_response_rejected(self):
        with self.assertRaises(MappingError) as c:
            plan_to_platform_tables({"status": "INVALID"})
        self.assertIn("STATUS", str(c.exception))


if __name__ == "__main__":
    unittest.main()
