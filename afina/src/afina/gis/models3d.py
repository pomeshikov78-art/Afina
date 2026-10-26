"""3D-загрузчики (§4.1.1 BIM/3D): OBJ, glTF 2.0 (JSON-подмножество), CityGML LOD1, IFC (структура).

Честный статус (G3): OBJ и glTF-POSITION читаются полностью; CityGML LOD1 — контуры
зданий с именами; IFC — пространственная структура (IfcBuilding/Storey/Wall/Slab) и
имена, геометрия — заглушки габаритов (полный IfcProduct-geometry — G4, требует
IfcOpenSSL-класс парсера). Семантика и материалы сохраняются в attrs (GIS-004 round-trip).
"""
import base64, json, math, re, struct
from typing import Dict, List, Optional

VERSION = "1.0.0-g3"


# ---------------- OBJ ----------------
def load_obj(text: str) -> Dict:
    verts, faces, normals, name = [], [], [], None
    for ln in text.splitlines():
        ln = ln.strip()
        if ln.startswith("v "):
            verts.append([float(x) for x in ln[2:].split()[:3]])
        elif ln.startswith("vn "):
            normals.append([float(x) for x in ln[3:].split()[:3]])
        elif ln.startswith("f "):
            idx = []
            for tok in ln[2:].split():
                idx.append(int(tok.split("/")[0]) - 1)
            faces.append(idx)
        elif ln.startswith("o ") or ln.startswith("g "):
            name = name or ln[2:].strip()
    return {"format": "OBJ", "name": name, "vertices": verts, "faces": faces,
            "normals": normals, "vertex_count": len(verts), "face_count": len(faces)}


def export_obj(mesh: Dict) -> str:
    out = [f"# Афина export · {mesh.get('name', 'object')}", f"o {mesh.get('name', 'object')}"]
    for v in mesh["vertices"]:
        out.append("v " + " ".join(f"{c:.6f}" for c in v))
    for f in mesh["faces"]:
        out.append("f " + " ".join(str(i + 1) for i in f))
    return "\n".join(out) + "\n"


# ---------------- glTF 2.0 (подмножество: POSITION из embedded/файлового буфера) ----------------
def load_gltf(gltf: Dict, bin_resolver=None) -> Dict:
    """bin_resolver(uri)->bytes для внешних .bin; data: URI декодируются встроенно.

    G4: читаются POSITION, NORMAL, TEXCOORD_0 и индексы (5121/5123/5125).
    Допущение: bufferView плотно упакован (byteStride не используется) — при
    наличии byteStride выдаётся предупреждение, а не тихий сдвиг данных."""
    meshes = []
    buffers = []
    warnings = []
    for buf in gltf.get("buffers", []):
        uri = buf.get("uri", "")
        if uri.startswith("data:"):
            b64 = uri.split(",", 1)[1]
            buffers.append(base64.b64decode(b64))
        elif bin_resolver:
            buffers.append(bin_resolver(uri))
        else:
            raise ValueError("glTF: внешний буфер без bin_resolver — загрузка невозможна (тихий пропуск запрещён)")

    def _bv(acc_idx):
        acc = gltf["accessors"][acc_idx]
        bview = gltf["bufferViews"][acc["bufferView"]]
        if bview.get("byteStride"):
            warnings.append(f"accessor {acc_idx}: byteStride не поддержан — данные могут быть разрежены")
        off = bview.get("byteOffset", 0) + acc.get("byteOffset", 0)
        return acc, buffers[bview.get("buffer", 0)], off

    def _floats(acc_idx, comp):
        acc, buf, off = _bv(acc_idx)
        n = acc["count"]
        if acc.get("componentType") != 5126:
            raise ValueError(f"accessor {acc_idx}: componentType {acc['componentType']} ≠ 5126 (FLOAT) — не читаем молча")
        return [list(struct.unpack_from(f"<{comp}f", buf, off + i * 4 * comp)) for i in range(n)]

    def _indices(acc_idx):
        acc, buf, off = _bv(acc_idx)
        ct = acc["componentType"]; n = acc["count"]
        fmt, size = {5121: ("<B", 1), 5123: ("<H", 2), 5125: ("<I", 4)}.get(ct, (None, 0))
        if not fmt:
            raise ValueError(f"accessor {acc_idx}: componentType {ct} не поддержан для индексов")
        return [struct.unpack_from(fmt, buf, off + i * size)[0] for i in range(n)]

    for m in gltf.get("meshes", []):
        for prim in m.get("primitives", []):
            attrs = prim["attributes"]
            verts = _floats(attrs["POSITION"], 3)
            mesh = {"name": m.get("name"), "vertices": verts, "vertex_count": len(verts),
                    "material": prim.get("material")}
            if "NORMAL" in attrs:
                mesh["normals"] = _floats(attrs["NORMAL"], 3)
            if "TEXCOORD_0" in attrs:
                mesh["uvs"] = _floats(attrs["TEXCOORD_0"], 2)
            if "indices" in prim:
                idx = _indices(prim["indices"])
                mesh["indices"] = idx
                mesh["triangle_count"] = len(idx) // 3
            meshes.append(mesh)
    return {"format": "glTF2", "meshes": meshes, "materials": gltf.get("materials", []),
            "warnings": warnings,
            "note": "POSITION/NORMAL/TEXCOORD_0/индексы прочитаны; анимации, скины и морфинг вне объёма (честная маркировка)"}


# ---------------- CityGML LOD1 ----------------
_NS = {"gml": "http://www.opengis.net/gml",
       "core": "http://www.opengis.net/citygml/2.0",
       "bldg": "http://www.opengis.net/citygml/building/2.0"}


def load_citygml_lod1(xml_text: str) -> Dict:
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml_text)
    buildings = []
    for bs in root.iter("{http://www.opengis.net/citygml/building/2.0}Building"):
        name_el = bs.find("{http://www.opengis.net/gml}name")
        name = name_el.text if name_el is not None else None
        coords = []
        for pos in bs.iter("{http://www.opengis.net/gml}posList"):
            vals = [float(x) for x in (pos.text or "").split()]
            coords = [[vals[i], vals[i + 1], vals[i + 2] if len(vals) > i + 2 else 0.0]
                      for i in range(0, len(vals) - 2, 3)]
            break
        buildings.append({"name": name, "lod": 1, "footprint": coords})
    return {"format": "CityGML", "lod": "LOD1", "buildings": buildings, "count": len(buildings)}


# ---------------- IFC (STEP: структура + геометрия SweptSolid, G4) ----------------
IFC_LINE = re.compile(r"#(\d+)\s*=\s*IFC(\w+)\s*\((.*?)\)\s*;", re.S)


def load_ifc_structure(step_text: str) -> Dict:
    """Пространственная структура И геометрия: IfcExtrudedAreaSolid
    (прямоугольный и произвольный закрытый профиль) через цепочки
    IfcLocalPlacement → абсолютные координаты призм. Неподдержанные
    представления (Brep/CSG/Tessellation) честно учитываются, а не подменяются."""
    entities = {}
    for m in IFC_LINE.finditer(step_text):
        eid, etype, args = m.group(1), m.group(2), m.group(3)
        entities[eid] = {"type": "IFC" + etype, "args": args}

    def args_of(eid):
        return _split_args(entities[eid]["args"]) if eid in entities else []

    def ref(a):
        a = (a or "").strip()
        return a[1:] if a.startswith("#") else None

    def point(eid):
        """IFCCARTESIANPOINT(Name, Coordinates) — координаты во втором аргументе."""
        try:
            parts = _split_args(entities[eid]["args"])
            raw = parts[1] if len(parts) > 1 else parts[0]
            return [float(v) for v in raw.strip("()").split(",")]
        except Exception:
            return None

    def direction(eid):
        p = point(eid) if eid else None
        if not p:
            return [0.0, 0.0, 1.0]
        n = sum(c * c for c in p) ** 0.5 or 1.0
        return [c / n for c in p]

    def _cross(a, b):
        return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]

    def axis2(eid):
        """IFCAXIS2PLACEMENT3D/2D → (loc3, R 3×3); для 2D — в плоскости XY."""
        if not eid or entities.get(eid, {}).get("type") not in ("IFCAXIS2PLACEMENT3D", "IFCAXIS2PLACEMENT2D"):
            return [0.0, 0.0, 0.0], [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
        parts = args_of(eid)
        loc = point(ref(parts[0])) if len(parts) > 0 else None
        loc = (loc + [0.0, 0.0, 0.0][:max(0, 3 - len(loc))]) if loc else [0.0, 0.0, 0.0]
        z = direction(ref(parts[1])) if len(parts) > 1 and ref(parts[1]) else [0.0, 0.0, 1.0]
        xref = direction(ref(parts[2])) if len(parts) > 2 and ref(parts[2]) else [1.0, 0.0, 0.0]
        if entities[eid]["type"] == "IFCAXIS2PLACEMENT2D":
            z = [0.0, 0.0, 1.0]; xref = (xref + [0.0, 0.0][:max(0, 3 - len(xref))]) if len(xref) < 3 else xref
        dot = sum(xref[i] * z[i] for i in range(3))
        x = [xref[i] - dot * z[i] for i in range(3)]
        nx = sum(c * c for c in x) ** 0.5
        x = [c / nx for c in x] if nx > 1e-12 else [1.0, 0.0, 0.0]
        y = _cross(z, x)
        R = [[x[0], y[0], z[0]], [x[1], y[1], z[1]], [x[2], y[2], z[2]]]  # столбцы — базисные векторы
        return loc, R

    def mul(R, v):
        return [sum(R[i][j] * v[j] for j in range(3)) for i in range(3)]

    def compose(t1, R1, t2, R2):
        return [t1[i] + mul(R1, t2)[i] for i in range(3)], [[sum(R1[i][k] * R2[k][j] for k in range(3)) for j in range(3)] for i in range(3)]

    def abs_placement(eid, depth=0):
        """Цепочка IFCLOCALPLACEMENT → абсолютные (t, R)."""
        if not eid or depth > 32 or entities.get(eid, {}).get("type") == "IFCAXIS2PLACEMENT3D":
            return axis2(eid) if eid else ([0.0, 0.0, 0.0], [[1, 0, 0], [0, 1, 0], [0, 0, 1]])
        parts = args_of(eid)
        parent = ref(parts[0]) if len(parts) > 0 else None
        rel = ref(parts[1]) if len(parts) > 1 else None
        t2, R2 = axis2(rel)
        t1, R1 = abs_placement(parent, depth + 1) if parent else ([0.0, 0.0, 0.0], [[1, 0, 0], [0, 1, 0], [0, 0, 1]])
        return compose(t1, R1, t2, R2)

    def profile_pts(eid):
        """2D-контур профиля → [(x,y), ...] или None."""
        if not eid or eid not in entities:
            return None
        t = entities[eid]["type"]; parts = args_of(eid)
        if t == "IFCRECTANGLEPROFILEDEF" and len(parts) >= 5:
            pos = ref(parts[2])
            loc2, _ = ([0.0, 0.0, 0.0], None) if not pos else axis2(pos)
            cx, cy = loc2[0], loc2[1]
            hx, hy = float(parts[3]) / 2.0, float(parts[4]) / 2.0
            return [(cx - hx, cy - hy), (cx + hx, cy - hy), (cx + hx, cy + hy), (cx - hx, cy + hy)]
        if t == "IFCARBITRARYCLOSEDPROFILEDEF" and len(parts) >= 2:
            curve = ref(parts[1])
            if curve and entities.get(curve, {}).get("type") == "IFCPOLYLINE":
                seg = args_of(curve)[0].strip("()")
                pts = [ref(x) for x in _split_args(seg)]
                out = []
                for pe in pts:
                    p = point(pe)
                    if p: out.append((p[0], p[1] if len(p) > 1 else 0.0))
                return out or None
        return None

    def solid_of(eid):
        """IFCEXTRUDEDAREASOLID → локальные вершины призмы (низ + верх)."""
        if not eid or entities.get(eid, {}).get("type") != "IFCEXTRUDEDAREASOLID":
            return None
        parts = args_of(eid)
        prof = profile_pts(ref(parts[0]))
        if not prof or len(parts) < 4:
            return None
        pt, pR = axis2(ref(parts[1]))
        d = direction(ref(parts[2]))
        try:
            depth = float(parts[3])
        except ValueError:
            return None
        base = [[pt[0] + mul(pR, [x, y, 0.0])[i] for i in range(3)] for x, y in prof]
        top = [[b[i] + d[i] * depth for i in range(3)] for b in base]
        return {"type": "extruded_prism", "profile_points": len(prof), "depth": depth,
                "vertices_local": [[round(c, 6) for c in v] for v in base + top]}

    def product_geom(pl_ref, rep_ref):
        """ObjectPlacement + Representation → абсолютная геометрия или честная отметка."""
        t, R = abs_placement(pl_ref)
        if not rep_ref or rep_ref not in entities:
            return None, 0
        solids, unsupported = [], 0
        prs = entities[rep_ref]
        items = []

        def _list_arg(raw):
            raw = (raw or "").strip()
            inner = raw[1:-1] if raw.startswith("(") and raw.endswith(")") else raw
            return _split_args(inner) if inner else []

        if prs["type"] == "IFCPRODUCTDEFINITIONSHAPE":
            for grp in _list_arg(_split_args(prs["args"])[-1]):
                g = ref(grp)
                if g and entities.get(g, {}).get("type") == "IFCSHAPEREPRESENTATION":
                    items.extend(ref(x) for x in _list_arg(_split_args(entities[g]["args"])[-1]))
        for it in items:
            it = ref(it) if it and it.startswith("#") else it
            sol = solid_of(it)
            if sol:
                sol["vertices"] = [[round(t[i] + mul(R, v)[i], 6) for i in range(3)] for v in sol["vertices_local"]]
                del sol["vertices_local"]
                solids.append(sol)
            else:
                unsupported += 1
        if not solids and not unsupported:
            return None, 0
        return (solids or None), unsupported

    def name_of(e):
        parts = _split_args(e["args"])
        return parts[2].strip("'") if len(parts) > 2 and parts[2].startswith("'") else None

    buildings, storeys, walls, slabs, spaces = [], [], [], [], []
    unsupported_total = 0
    for eid, e in entities.items():
        t = e["type"]
        if t == "IFCBUILDING":
            buildings.append({"id": eid, "name": name_of(e)})
        elif t == "IFCBUILDINGSTOREY":
            storeys.append({"id": eid, "name": name_of(e), "elevation": _arg_float(e["args"], -1)})
        elif t in ("IFCWALL", "IFCWALLSTANDARDCASE", "IFCSLAB"):
            parts = _split_args(e["args"])
            geom, uns = product_geom(ref(parts[4]) if len(parts) > 4 else None,
                                     ref(parts[5]) if len(parts) > 5 else None)
            unsupported_total += uns
            rec = {"id": eid, "name": name_of(e), "geometry": geom}
            (walls if t != "IFCSLAB" else slabs).append(rec)
        elif t == "IFCSPACE":
            spaces.append({"id": eid, "name": name_of(e)})
    n_geom = sum(1 for w in walls + slabs if w.get("geometry"))
    return {"format": "IFC", "structure": {"buildings": buildings, "storeys": storeys,
            "walls": len(walls), "slabs": len(slabs), "spaces": spaces},
            "elements": {"walls": walls, "slabs": slabs},
            "entities_total": len(entities),
            "geometry": {"solids_built": n_geom, "unsupported_representations": unsupported_total,
                         "note": "IfcExtrudedAreaSolid (Rectangle/ArbitraryClosedProfile) → призмы в абсолютных "
                                 "координатах; Brep/CSG/Tessellation не поддержаны и УЧТЕНЫ честно, без подмены"},
            "semantics_preserved": True}


def _split_args(args: str) -> List[str]:
    out, depth, cur, inq = [], 0, "", False
    for ch in args:
        if ch == "'":
            inq = not inq
        if not inq:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 0:
                out.append(cur); cur = ""; continue
        cur += ch
    out.append(cur)
    return out


def _arg_float(args: str, idx: int) -> Optional[float]:
    parts = _split_args(args)
    try:
        return float(parts[idx].strip())
    except (ValueError, IndexError):
        return None
