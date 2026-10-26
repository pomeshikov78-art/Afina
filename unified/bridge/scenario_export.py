"""unified/bridge/scenario_export.py — обстановка платформы → контракт ENG6-PLAN (UNIF-18).

Читает объекты платформы (GET /api/v1/objects, таблица objects Прил. Г) и
собирает сущности для build_plan_request(). Соглашения маппинга (фиксируются
в протоколе экспорта; расширение — данными в attrs, а не кодом):

  class = "COM"   → GEO_OBJECT: x=lng, y=lat (ГРАДУСЫ WGS-84; при |x|>180 или
                    |y|>90 объект честно пропускается с предупреждением —
                    вероятно, CRS проекта в метрах, требуется пересчёт на
                    стороне платформы, AUT-001);
                    attrs (JSON): {"node_type": "cp|relay|radio|sat|observer|ew_*",
                                   "org_unit": "u-1",
                                   "unit": {"inventoryId","model","techState",
                                            "cost","deployTimeMin"}}
  class = "LINK"  → связь: attrs {"a","b","link_type"?,"priority"?}
  class = "ORDER" → заявка: attrs {"src","dst","rate_kbps","priority"?,
                                   "max_delay_ms"?,"protected"?}
  org_units       → attrs объектов COM с ключом "org_path"/"org_node" либо
                    отдельный объект class="ORG" (attrs {"org_id","path","node_id"}).

techState объектов дополнительно сверяется с tech_states платформы (ts_code
строки objects): «исправен/TS-0» → исправна и т.д. — классификатор Прил. В.

Выход: {"entities": {...}, "request": <контракт ENG6-PLAN>, "warnings": [...]}
CLI: python3 scenario_export.py --platform-url http://127.0.0.1:8470 \
        --login unified-admin --password … --out scenario.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
if BRIDGE_DIR not in sys.path:
    sys.path.insert(0, BRIDGE_DIR)

from datamodel_map import build_plan_request  # noqa: E402
from platform_client import PlatformClient  # noqa: E402

VERSION = "1.0.0"

# Классификатор техсостояния Прил. В (ts_code строки objects) → TEK-состояние ASSET
TS_CODE_TO_STATE = {
    "TS-0": "исправна", "TS-1": "исправна",
    "TS-2": "ограниченно пригодна", "TS-3": "ограниченно пригодна",
    "TS-4": "неисправна", "TS-5": "неисправна",
}


def _attrs(row: Dict[str, Any]) -> Dict[str, Any]:
    """attrs строки objects → dict. Штатно сервер хранит JSON-объект; защита от
    двойного кодирования (attrs уже был строкой при записи) — для legacy-данных."""
    a = row.get("attrs")
    for _ in range(2):
        if isinstance(a, dict):
            return a
        if isinstance(a, str) and a.strip():
            try:
                a = json.loads(a)
            except json.JSONDecodeError:
                return {}
        else:
            return {}
    return a if isinstance(a, dict) else {}


def export_entities(client: PlatformClient, project: Optional[str] = None) -> Dict[str, Any]:
    """Объекты платформы → сущности Прил. Г (assets/geo/relations/orders/org_units)."""
    rows = client.list_objects()
    if project:
        rows = [r for r in rows if r.get("project") == project]
    assets: Dict[str, Dict[str, Any]] = {}
    geo: List[Dict[str, Any]] = []
    rels: List[Dict[str, Any]] = []
    orders: List[Dict[str, Any]] = []
    org_units: Dict[str, Dict[str, Any]] = {}
    warnings: List[str] = []

    for r in rows:
        cls = (r.get("class") or "").upper()
        a = _attrs(r)
        oid = str(r.get("id"))
        if cls == "ORG":
            org_id = str(a.get("org_id") or oid)
            org_units[org_id] = {"path": a.get("path") or org_id}
            if a.get("node_id"):
                org_units[org_id]["nodeId"] = str(a["node_id"])
        elif cls == "COM":
            x, y = r.get("x"), r.get("y")
            if x is None or y is None:
                warnings.append(f"COM/{oid}: нет координат — пропущен")
                continue
            if abs(float(x)) > 180.0 or abs(float(y)) > 90.0:
                warnings.append(f"COM/{oid}: координаты ({x},{y}) не в градусах WGS-84 "
                                f"(CRS проекта в метрах?) — объект пропущен; требуется "
                                f"пересчёт на стороне платформы (AUT-001)")
                continue
            node = {"id": oid, "lat": float(y), "lng": float(x),
                    "type": a.get("node_type") or "radio",
                    "name": r.get("name") or oid}
            if a.get("org_unit"):
                node["orgUnitId"] = str(a["org_unit"])
                if node["orgUnitId"] not in org_units:
                    org_units[node["orgUnitId"]] = {"path": node["orgUnitId"],
                                                    **({"nodeId": str(a["org_node"])} if a.get("org_node") else {})}
            unit = a.get("unit") or {}
            if unit.get("inventoryId"):
                node["unitId"] = str(unit["inventoryId"])
                ts_state = TS_CODE_TO_STATE.get(str(r.get("ts_code") or "TS-0"), "исправна")
                assets[str(unit["inventoryId"])] = {
                    "inventoryId": str(unit["inventoryId"]),
                    "model": str(unit.get("model") or "не указан"),
                    "nodeType": node["type"],
                    # приоритет: явное techState в attrs; иначе — ts_code строки (Прил. В)
                    "techState": unit.get("techState") or ts_state,
                    **({"cost": unit["cost"]} if unit.get("cost") is not None else {}),
                    **({"deployTimeMin": unit["deployTimeMin"]} if unit.get("deployTimeMin") is not None else {}),
                }
            geo.append(node)
        elif cls == "LINK":
            if not (a.get("a") and a.get("b")):
                warnings.append(f"LINK/{oid}: нет attrs.a/attrs.b — пропущен")
                continue
            rel = {"id": oid, "a": str(a["a"]), "b": str(a["b"])}
            for k_src, k_dst in (("link_type", "linkType"), ("priority", "priority"),
                                 ("freq_mhz", "freqMhz"), ("bw_khz", "bwKhz")):
                if a.get(k_src) is not None:
                    rel[k_dst] = a[k_src]
            rels.append(rel)
        elif cls == "ORDER":
            if not (a.get("src") and a.get("dst")):
                warnings.append(f"ORDER/{oid}: нет attrs.src/attrs.dst — пропущен")
                continue
            order = {"id": oid, "src": str(a["src"]), "dst": str(a["dst"]),
                     "rateKbps": float(a.get("rate_kbps", 16))}
            for k_src, k_dst in (("priority", "priority"), ("max_delay_ms", "maxDelayMs"),
                                 ("protected", "protected")):
                if a.get(k_src) is not None:
                    order[k_dst] = a[k_src]
            orders.append(order)
        else:
            warnings.append(f"{cls}/{oid}: класс не участвует в планировании — пропущен")

    return {"assets": list(assets.values()), "geo_objects": geo, "relations": rels,
            "orders": orders, "org_units": org_units or None, "warnings": warnings}


def export_request(client: PlatformClient, project: Optional[str] = None,
                   constraints: Optional[Dict[str, Any]] = None,
                   environment: Optional[Dict[str, Any]] = None,
                   meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Полный цикл: объекты платформы → контракт ENG6-PLAN (+ предупреждения)."""
    ent = export_entities(client, project=project)
    request = build_plan_request(
        ent["assets"], ent["geo_objects"], ent["relations"], ent["orders"],
        org_units=ent["org_units"],
        constraints=constraints or {"budget": 400, "physics": "fast"},
        environment=environment or {"clutterType": "open", "terrain": {"source": "flat"}},
        meta=meta or {"name": f"Экспорт обстановки платформы {time.strftime('%Y-%m-%d %H:%M:%S')}",
                      "author": "scenario_export"})
    return {"entities": ent, "request": request, "warnings": ent["warnings"],
            "counts": {"assets": len(ent["assets"]), "nodes": len(ent["geo_objects"]),
                       "links": len(ent["relations"]), "demands": len(ent["orders"])}}


def main() -> int:
    ap = argparse.ArgumentParser(description="Экспорт обстановки платформы «Афина» в контракт ENG6-PLAN")
    ap.add_argument("--platform-url", default=os.environ.get("AFINA_PLATFORM_URL", "http://127.0.0.1:8470"))
    ap.add_argument("--login", default="unified-admin")
    ap.add_argument("--password", default=os.environ.get("AFINA_PASSWORD", ""))
    ap.add_argument("--project", default=None)
    ap.add_argument("--out", default=None, help="файл сценария JSON (для загрузки в рабочее место)")
    args = ap.parse_args()
    if not args.password:
        print("нужен --password (или AFINA_PASSWORD)", file=sys.stderr)
        return 2

    client = PlatformClient(args.platform_url)
    client.login(args.login, args.password)
    res = export_request(client, project=args.project)
    out = json.dumps(res["request"], ensure_ascii=False, indent=1)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(out)
        print(f"сценарий: {args.out} · {json.dumps(res['counts'], ensure_ascii=False)}")
    else:
        print(out)
    for w in res["warnings"]:
        print(f"  ⚠ {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
