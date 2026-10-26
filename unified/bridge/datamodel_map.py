"""unified/bridge/datamodel_map.py — единая модель данных (шаг 7 плана совмещения).

Маппинг: Приложение Г «Афины» (19 сущностей) ⇄ контракт ENG6-PLAN
(topology.nodes / topology.links / fleet / demands) и плоские таблицы Пути А
(map_object, link_line, route, deployment_wave …).

Контракт ENG6-PLAN (сверен с core/schema.mjs, examples/battalion.json и
integrations/erp_connector.mjs — побайтово точные имена полей):
  node  : {id, type ∈ cp|relay|radio|sat|observer|ew_jam|ew_rlr|ew_rer,
           name?, lat, lng (6 знаков — NFR-03), status?, parentId?, unitId?}
  link  : {id, from, to, type ∈ VHF|UHF|HF|RRL|SAT|FOC, priority?,
           freqMhz?, bwKhz?, modulation?, fec?, cost?}   (радио-дефолты — из LINK_TYPES ядра)
  fleet : {unitId, kind, nodeType, condition ∈ green|yellow|red,
           costDeploy?, deployTimeMin?}
  demand: {id, from, to, rateKbps, maxDelayMs?, maxHops?, priority?,
           minAvailability?, protected?}

Соответствия сущностям Прил. Г:
  ASSET + EQUIPMENT_MODEL      → fleet[] (unitId=инвентарник, kind=образец)
  TECHNICAL_STATE (Прил. В TS) → condition: исправна→green, ограниченно→yellow, неисправна→red
                                 (семантика FR-RES-03 🟢🟡🔴 совпадает напрямую)
  GEO_OBJECT                   → topology.nodes[] (+ unitId — привязка к ASSET)
  ORG_UNIT (иерархия ОШС)      → nodes[].parentId / subordination-путь
  связи GEO_OBJECT             → topology.links[] (автовыбор типа по дальности — FR-MAP-06)
  заявки (ORDER/CAPABILITY)    → demands[]
  CALCULATION_RUN              ↔ паспорт + core.engineSha256/engineVersion ответа

Единицы (ENG-004): координаты — град WGS-84 (6 знаков), полоса — кбит/с,
дальность — км, время — мин, стоимость — у.е. Конвертация единиц запрещена
молча: при несовпадении — MappingError со стабильным машинным кодом.

stdlib-only, детерминированно, чистые функции (без БД/сети).
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

VERSION = "1.1.0"

# --- семантика технического состояния (Прил. В классификатор TS ⇄ FR-RES-03) ---
TECH_STATE_TO_CONDITION = {
    "исправна": "green", "исправно": "green", "green": "green", "ok": "green",
    "ограниченно пригодна": "yellow", "ограниченно пригодно": "yellow", "yellow": "yellow",
    "limited": "yellow",
    "неисправна": "red", "неисправно": "red", "red": "red", "fault": "red",
}

# --- автовыбор типа линии по дальности (FR-MAP-06, таблица линии B) ---
LINK_TYPE_BY_RANGE_KM = ((12.0, "FOC"), (25.0, "VHF"), (45.0, "UHF"), (90.0, "RRL"))
LINK_FALLBACK = "SAT"

VALID_NODE_TYPES = {"cp", "relay", "radio", "sat", "observer", "ew_jam", "ew_rlr", "ew_rer"}
VALID_LINK_TYPES = {"VHF", "UHF", "HF", "RRL", "SAT", "FOC"}
VALID_CONDITIONS = {"green", "yellow", "red"}


class MappingError(ValueError):
    """Ошибка маппинга (единицы/домен/ссылка) — стабильный машинный код в сообщении."""


def round6(x: float) -> float:
    """NFR-03: координаты до 6 знаков (~10 см)."""
    return round(float(x) + 0.0, 6)


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Та же формула и константа R=6371 км, что в ENG6 (побитовое совпадение режимов)."""
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    a = math.sin(d_lat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lng / 2) ** 2
    return 2 * 6371.0 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def pick_link_type(d_km: float, forced: Optional[str] = None) -> str:
    if forced:
        if forced not in VALID_LINK_TYPES:
            raise MappingError(f"REF: неизвестный тип линии «{forced}» (допустимы {sorted(VALID_LINK_TYPES)})")
        return forced
    for limit, t in LINK_TYPE_BY_RANGE_KM:
        if d_km <= limit:
            return t
    return LINK_FALLBACK


# ---------- платформа → ENG6-PLAN ----------

def tech_state_to_condition(state: Optional[str]) -> str:
    if state is None:
        return "green"
    key = str(state).strip().lower()
    if key not in TECH_STATE_TO_CONDITION:
        raise MappingError(f"ENUM: техническое состояние «{state}» вне классификатора TS (Прил. В)")
    return TECH_STATE_TO_CONDITION[key]


def asset_to_fleet_item(asset: Dict[str, Any]) -> Dict[str, Any]:
    """ASSET (+ EQUIPMENT_MODEL/TECHNICAL_STATE) → элемент fleet[]."""
    inv = asset.get("inventoryId") or asset.get("unitId")
    model = asset.get("model") or asset.get("kind")
    if not inv or not model:
        raise MappingError("REQUIRED: ASSET.inventoryId и ASSET.model обязательны для fleet")
    node_type = asset.get("nodeType") or "radio"
    if node_type not in VALID_NODE_TYPES:
        raise MappingError(f"ENUM: nodeType «{node_type}» вне {sorted(VALID_NODE_TYPES)}")
    item: Dict[str, Any] = {
        "unitId": str(inv),
        "kind": str(model),
        "nodeType": node_type,
        "condition": tech_state_to_condition(asset.get("techState")
                                             or (asset.get("technicalState") or {}).get("state")),
    }
    # единицы совпадают с контрактом: стоимость — у.е., время — минуты (перенос как есть)
    if asset.get("cost") is not None:
        item["costDeploy"] = float(asset["cost"])
    if asset.get("deployTimeMin") is not None:
        item["deployTimeMin"] = float(asset["deployTimeMin"])
    return item


def geo_object_to_node(obj: Dict[str, Any], org_units: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """GEO_OBJECT (+ ORG_UNIT) → topology.nodes[]."""
    for req in ("id", "lat", "lng"):
        if obj.get(req) is None:
            raise MappingError(f"REQUIRED: GEO_OBJECT.{req} обязателен")
    lat, lng = float(obj["lat"]), float(obj["lng"])
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
        raise MappingError(f"RANGE: координаты ({lat}, {lng}) вне WGS-84")
    ntype = obj.get("type") or "radio"
    if ntype not in VALID_NODE_TYPES:
        raise MappingError(f"ENUM: тип узла «{ntype}» вне {sorted(VALID_NODE_TYPES)}")
    node: Dict[str, Any] = {"id": str(obj["id"]), "type": ntype,
                            "lat": round6(lat), "lng": round6(lng)}
    if obj.get("name"):
        node["name"] = str(obj["name"])
    node["status"] = obj.get("status") or "active"
    if obj.get("unitId"):
        node["unitId"] = str(obj["unitId"])          # привязка к ASSET (fleet.unitId)
    # ОШС: контракт ENG6-PLAN выражает подчинённость иерархией УЗЛОВ (parentId →
    # другой узел, например узел КП). ORG_UNIT без узла-представителя остаётся
    # в слое платформы и в запрос не попадает (валидатор отклоняет внешние id).
    parent = obj.get("parentId")
    org_id = obj.get("orgUnitId")
    if parent is None and org_id and org_units:
        org = org_units.get(str(org_id))
        if org is None:
            raise MappingError(f"REF: GEO_OBJECT «{obj['id']}» ссылается на неизвестный ORG_UNIT «{org_id}»")
        parent = org.get("nodeId")
    if parent is not None and str(parent) != node["id"]:   # защита от самоссылки
        node["parentId"] = str(parent)
    return node


def relation_to_link(rel: Dict[str, Any], nodes_index: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Связь двух GEO_OBJECT → topology.links[] с автовыбором типа по дальности."""
    a = nodes_index.get(str(rel.get("a") or rel.get("from")))
    b = nodes_index.get(str(rel.get("b") or rel.get("to")))
    if not a or not b:
        raise MappingError(f"REF: связь {rel.get('a', rel.get('from'))}—{rel.get('b', rel.get('to'))} "
                           "ссылается на несуществующий узел")
    d = haversine_km(a["lat"], a["lng"], b["lat"], b["lng"])
    ltype = pick_link_type(d, rel.get("linkType") or rel.get("type"))
    link: Dict[str, Any] = {"id": str(rel.get("id") or f"l-{a['id']}-{b['id']}"),
                            "from": a["id"], "to": b["id"], "type": ltype}
    # радио-параметры — только если заданы явно (иначе дефолты LINK_TYPES ядра)
    for opt in ("freqMhz", "bwKhz", "modulation", "fec", "priority", "cost"):
        if rel.get(opt) is not None:
            link[opt] = rel[opt]
    return link


def order_to_demand(order: Dict[str, Any]) -> Dict[str, Any]:
    """Заявка (ORDER из ERP/диспетчеризации либо потребность CAPABILITY) → demands[]."""
    did = order.get("id")
    src = order.get("src") or order.get("from")
    dst = order.get("dst") or order.get("to")
    if not did or not src or not dst:
        raise MappingError("REQUIRED: заявка.id/.src/.dst обязательны")
    rate = order.get("rateKbps")
    if rate is None:
        rate = order.get("bwKbps")
    if rate is None:
        raise MappingError("REQUIRED: заявка.rateKbps обязательна")
    if float(rate) <= 0:
        raise MappingError(f"RANGE: rateKbps «{rate}» ≤ 0")
    d: Dict[str, Any] = {"id": str(did), "from": str(src), "to": str(dst), "rateKbps": float(rate)}
    for opt in ("maxDelayMs", "maxHops", "priority", "minAvailability", "protected"):
        if order.get(opt) is not None:
            d[opt] = order[opt]
    return d


def build_plan_request(assets: Sequence[Dict[str, Any]],
                       geo_objects: Sequence[Dict[str, Any]],
                       relations: Sequence[Dict[str, Any]],
                       orders: Sequence[Dict[str, Any]],
                       org_units: Optional[Dict[str, Dict[str, Any]]] = None,
                       constraints: Optional[Dict[str, Any]] = None,
                       environment: Optional[Dict[str, Any]] = None,
                       meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Сборка полного запроса ENG6-PLAN из сущностей платформы (Прил. Г)."""
    fleet = [asset_to_fleet_item(a) for a in assets]
    if len({f["unitId"] for f in fleet}) != len(fleet):
        raise MappingError("DUPLICATE: инвентарные номера ASSET не уникальны")
    nodes = [geo_object_to_node(o, org_units) for o in geo_objects]
    index = {n["id"]: n for n in nodes}
    if len(index) != len(nodes):
        raise MappingError("DUPLICATE: идентификаторы GEO_OBJECT не уникальны")
    links = [relation_to_link(r, index) for r in relations]
    demands = [order_to_demand(o) for o in orders]
    for d in demands:
        for side in ("from", "to"):
            if d[side] not in index:
                raise MappingError(f"REF: заявка «{d['id']}» указывает несуществующий узел {side}=«{d[side]}»")
    # привязка узлов к технике: unitId обязан существовать в парке (если парк задан)
    if fleet:
        fleet_ids = {f["unitId"] for f in fleet}
        for n in nodes:
            if n.get("unitId") and n["unitId"] not in fleet_ids:
                raise MappingError(f"REF: узел «{n['id']}» привязан к несуществующему ASSET «{n['unitId']}»")
    # ОШС: parentId обязан указывать на существующий узел (контракт валидатора)
    for n in nodes:
        if n.get("parentId") and n["parentId"] not in index:
            raise MappingError(f"REF: узел «{n['id']}» имеет parentId «{n['parentId']}» вне состава узлов")
    req: Dict[str, Any] = {"topology": {"nodes": nodes, "links": links}}
    if meta:
        req["meta"] = meta
    if fleet:
        req["fleet"] = fleet
    if demands:
        req["demands"] = demands
    if constraints:
        req["constraints"] = constraints
    if environment:
        req["environment"] = environment
    return req


# ---------- ENG6-PLAN → платформа (обратный ход, таблицы Пути А / К-03) ----------

def plan_to_platform_tables(plan_response: Dict[str, Any],
                            run_id: Optional[str] = None) -> Dict[str, List[Dict[str, Any]]]:
    """Ответ plan() → плоские таблицы для БД платформы (структура идентична Пути А).

    Состав (совпадает с выгрузкой erp_connector К-03, чтобы обе линии отдавали
    в БД заказчика идентичную структуру): map_object, link_line, link_rejected,
    route, deployment_wave, schedule_summary, survivability, metrics, issues, calc_run.
    """
    status = plan_response.get("status")
    if status in (None, "INVALID", "ERROR"):
        raise MappingError(f"STATUS: ответ статуса «{status}» не содержит плана — выгрузка невозможна")
    plan = plan_response.get("plan") or {}
    core = plan_response.get("core") or {}
    out: Dict[str, Any] = {
        "map_object": [], "link_line": [], "link_rejected": [], "route": [],
        "deployment_wave": [], "schedule_summary": [], "survivability": [],
        "metrics": [], "issues": [], "calc_run": [],
    }
    for u in plan.get("deployedUnits") or []:
        out["map_object"].append({
            "object_id": u.get("nodeId"), "object_type": u.get("nodeType"),
            "unit_id": u.get("unitId"), "condition": u.get("condition"),
            "geo_lat": round6(u.get("lat", 0.0)), "geo_lng": round6(u.get("lng", 0.0)),
            "cost": u.get("cost"), "wave_index": u.get("wave"), "start_min": u.get("startMin")})
    for l in plan.get("activeLinks") or []:
        out["link_line"].append({
            "line_id": l.get("id"), "a": l.get("from"), "b": l.get("to"), "kind": l.get("type"),
            "length_km": l.get("distKm"), "margin_db": l.get("marginDb"),
            "capacity_kbps": l.get("capacityKbps"), "load_kbps": l.get("loadKbps"),
            "util_pct": l.get("utilPct"), "delay_ms": l.get("delayMs"),
            "availability_pct": l.get("availabilityPct"), "cost": l.get("cost"), "role": l.get("role")})
    for l in plan.get("rejectedLinks") or []:
        out["link_rejected"].append({"line_id": l.get("id"), "a": l.get("from"), "b": l.get("to"),
                                     "kind": l.get("type"), "code": l.get("code"),
                                     "reason": l.get("reason"), "margin_db": l.get("marginDb")})
    for r in plan.get("routes") or []:
        out["route"].append({"demand_id": r.get("demandId"), "status": r.get("status"),
                             "granted_kbps": r.get("grantedKbps"), "deficit_kbps": r.get("deficitKbps"),
                             "delay_ms": r.get("delayMs"), "hops": r.get("hops"),
                             "reason": r.get("reason"), "redundant": bool(r.get("redundant")),
                             "path_nodes": r.get("path"), "path_lines": r.get("links")})
    sch = plan.get("schedule") or {}
    for w in sch.get("waves") or []:
        out["deployment_wave"].append({
            "wave_index": w.get("index"), "t_start_min": w.get("tStartMin"),
            "t_end_min": w.get("tEndMin"), "duration_min": w.get("durationMin"),
            "crews_used": w.get("crewsUsed"),
            "items": [{"object_id": i.get("nodeId"), "unit_id": i.get("unitId"),
                       "crew": i.get("crew"), "start_min": i.get("startMin"),
                       "end_min": i.get("endMin")} for i in (w.get("items") or [])]})
    if sch:
        out["schedule_summary"].append({"makespan_min": sch.get("makespanMin"), "crews": sch.get("crews"),
                                        "crew_util_pct": sch.get("crewUtilPct"),
                                        "critical_path": [c.get("id") for c in (sch.get("criticalPath") or [])]})
    if plan.get("survivability"):
        out["survivability"].append(plan["survivability"])
    if plan_response.get("metrics"):
        m = dict(plan_response["metrics"])
        m["_run_id"] = run_id
        out["metrics"].append(m)
    for i in (plan_response.get("diagnostics") or {}).get("issues") or []:
        out["issues"].append({"sev": i.get("sev"), "code": i.get("code"), "message": i.get("message")})
    out["calc_run"].append({  # CALCULATION_RUN ⇄ паспорт/задача сервиса
        "run_id": run_id, "status": status,
        "backend": f"ENG6-JS {core.get('engineVersion') or core.get('version') or '?'} "
                   f"{str(core.get('engineSha256') or core.get('sha256') or '')[:16]}",
        "planner_version": core.get("planner"),
        "compute_ms": (plan_response.get("timings") or {}).get("totalMs")})
    return out


__all__ = ["MappingError", "build_plan_request", "plan_to_platform_tables",
           "asset_to_fleet_item", "geo_object_to_node", "relation_to_link",
           "order_to_demand", "tech_state_to_condition", "pick_link_type",
           "haversine_km", "round6", "VERSION"]
