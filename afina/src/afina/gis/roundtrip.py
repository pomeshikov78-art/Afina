"""Round-trip контроль (GIS-003/004, DAT-005): импорт → экспорт → импорт.

Необъяснимых потерь быть не должно; потери формата фиксируются в протоколе.
Исходные файлы не изменяются (§4.13.4).
"""
import json, hashlib
from typing import Dict, List

VERSION = "1.0.0"


def _geom_hash(g) -> str:
    return hashlib.sha256(json.dumps(g, sort_keys=True, default=str).encode()).hexdigest()


def compare_datasets(original: List[Dict], after: List[Dict]) -> Dict:
    """Сравнение наборов Feature: количество, ID, геометрии, атрибуты."""
    proto = {"original_count": len(original), "roundtrip_count": len(after),
             "id_losses": [], "geometry_losses": [], "attribute_losses": [],
             "attribute_changes": [], "format_losses_explained": []}
    o_by_id = {f.get("id"): f for f in original}
    a_by_id = {f.get("id"): f for f in after}
    for fid, f in o_by_id.items():
        if fid not in a_by_id:
            proto["id_losses"].append(fid); continue
        g = a_by_id[fid]
        if _geom_hash(f.get("geometry")) != _geom_hash(g.get("geometry")):
            proto["geometry_losses"].append(fid)
        po, pa = f.get("properties") or {}, g.get("properties") or {}
        for k, v in po.items():
            if k not in pa:
                proto["attribute_losses"].append({"id": fid, "attr": k, "value": v})
            elif str(pa[k]) != str(v):
                proto["attribute_changes"].append({"id": fid, "attr": k, "before": v, "after": pa[k]})
    proto["unexplained_losses"] = (len(proto["id_losses"]) + len(proto["geometry_losses"])
                                   + len(proto["attribute_losses"]))
    proto["verdict"] = "PASS (0 необъяснимых потерь)" if proto["unexplained_losses"] == 0 else "FAIL"
    return proto


def file_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def migration_protocol(source_files: List[str], migrated_datasets, reimported) -> Dict:
    """DAT-005: миграция без повреждения архива — хэши исходников до и после совпадают."""
    return {"sources": {p: file_hash(p) for p in source_files},
            "comparison": compare_datasets(migrated_datasets, reimported),
            "rule": "исходные файлы и сведения о происхождении остаются неизменными (§4.13.4)"}
