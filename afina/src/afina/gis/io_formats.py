"""Форматы обмена (§4.1.1): GeoJSON, KML, Shapefile (собственные реализации, stdlib).

Round-trip импорт→экспорт→импорт без необъяснимых потерь (GIS-003/004):
потери формата фиксируются в протоколе (см. roundtrip.py).
SXF/MAP/SIT/SITX — до подтверждения лицензий работают через конвертацию на
стороне заказчика в GeoJSON/Shapefile (путь B решения G0.2).
"""
import json, struct, os
from typing import List, Dict, Optional

VERSION = "1.0.0"


# ============================ GeoJSON ============================
def geojson_export(features: List[Dict], crs_note: str = "WGS84 (RFC 7946)") -> str:
    fc = {"type": "FeatureCollection", "crs_note": crs_note, "features": features}
    return json.dumps(fc, ensure_ascii=False)


def geojson_import(text: str) -> List[Dict]:
    d = json.loads(text)
    if d.get("type") != "FeatureCollection":
        raise ValueError("GeoJSON: ожидается FeatureCollection")
    return d["features"]


# ============================ KML ============================
def kml_export(features: List[Dict], name: str = "Афина") -> str:
    from xml.sax.saxutils import escape
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>',
           f'<name>{escape(name)}</name>']
    for f in features:
        g = f.get("geometry") or {}
        props = f.get("properties") or {}
        out.append('<Placemark>')
        out.append(f'<name>{escape(str(props.get("name", f.get("id", ""))))}</name>')
        if props:
            out.append('<ExtendedData>' + ''.join(
                f'<Data name="{escape(str(k))}"><value>{escape(str(v))}</value></Data>'
                for k, v in props.items() if k != "name") + '</ExtendedData>')
        if g.get("type") == "Point":
            lon, lat = g["coordinates"][0], g["coordinates"][1]
            z = g["coordinates"][2] if len(g["coordinates"]) > 2 else 0
            out.append(f'<Point><coordinates>{lon},{lat},{z}</coordinates></Point>')
        elif g.get("type") == "LineString":
            cs = ' '.join(f'{c[0]},{c[1]},{c[2] if len(c) > 2 else 0}' for c in g["coordinates"])
            out.append(f'<LineString><coordinates>{cs}</coordinates></LineString>')
        out.append('</Placemark>')
    out.append('</Document></kml>')
    return '\n'.join(out)


def kml_import(text: str) -> List[Dict]:
    import xml.etree.ElementTree as ET
    ns = {"k": "http://www.opengis.net/kml/2.2"}
    root = ET.fromstring(text)
    features = []
    for pm in root.iter("{http://www.opengis.net/kml/2.2}Placemark"):
        name = pm.findtext("k:name", "", ns)
        props = {"name": name} if name else {}
        for d in pm.iter("{http://www.opengis.net/kml/2.2}Data"):
            props[d.get("name")] = (d.findtext("k:value", "", ns))
        geom = None
        pt = pm.find(".//k:Point/k:coordinates", ns)
        ln = pm.find(".//k:LineString/k:coordinates", ns)
        if pt is not None:
            c = [float(x) for x in pt.text.strip().split(",")]
            geom = {"type": "Point", "coordinates": c}
        elif ln is not None:
            coords = [[float(x) for x in part.split(",")] for part in ln.text.strip().split()]
            geom = {"type": "LineString", "coordinates": coords}
        features.append({"type": "Feature", "id": f"kml-{len(features)}", "properties": props, "geometry": geom})
    return features


# ============================ Shapefile (SHP+SHX+DBF) ============================
SHP_POINT, SHP_POLYLINE, SHP_POLYGON = 1, 3, 5


def _bbox(coords):
    xs = [c[0] for c in coords]; ys = [c[1] for c in coords]
    return (min(xs), min(ys), max(xs), max(ys))


def _collect_coords(geom):
    t = geom["type"]
    if t == "Point":
        return [geom["coordinates"]], (min(geom["coordinates"][:2]), min(geom["coordinates"][:2]),
                                       max(geom["coordinates"][:2]), max(geom["coordinates"][:2]))
    if t == "LineString":
        return [geom["coordinates"]], _bbox(geom["coordinates"])
    if t == "Polygon":
        return geom["coordinates"], _bbox([c for ring in geom["coordinates"] for c in ring])
    raise ValueError(f"shapefile: тип «{t}» не поддержан в базовой реализации (Point/LineString/Polygon)")


def shapefile_export(path_base: str, features: List[Dict], fields: List[tuple] = None):
    """fields: [(name, 'C'|'N', size, dec)]. Запись SHP+SHX+DBF (big-endian file code 9994)."""
    import datetime
    recs = []
    for f in features:
        g = f.get("geometry")
        if not g: continue
        parts, bbox = _collect_coords(g)
        recs.append((g["type"], parts, bbox, f.get("properties") or {}))
    if not recs:
        raise ValueError("shapefile: нет геометрий для экспорта")
    shp_type = {"Point": SHP_POINT, "LineString": SHP_POLYLINE, "Polygon": SHP_POLYGON}[recs[0][0]]
    allb = [min(r[2][0] for r in recs), min(r[2][1] for r in recs), max(r[2][2] for r in recs), max(r[2][3] for r in recs)]

    shp = bytearray(); shx = bytearray()
    header = bytearray(100)
    struct.pack_into(">i", header, 0, 9994)
    struct.pack_into("<i", header, 28, 1000)
    struct.pack_into("<i", header, 32, shp_type)
    struct.pack_into("<8d", header, 36, *allb, 0.0, 0.0, 0.0, 0.0)
    shp += header; shx += bytes(header)
    offset = 50  # в 16-битных словах
    for typ, parts, bbox, props in recs:
        if typ == "Point":
            content = struct.pack("<i2d", SHP_POINT, parts[0][0], parts[0][1])
        else:
            npts = sum(len(p) for p in parts)
            content = struct.pack("<i4d", {"LineString": SHP_POLYLINE, "Polygon": SHP_POLYGON}[typ], *bbox)
            content += struct.pack("<2i", len(parts), npts)
            off = 0; offs = []
            for p in parts:
                offs.append(off); off += len(p)
            content += struct.pack(f"<{len(offs)}i", *offs)
            for p in parts:
                for c in p:
                    content += struct.pack("<2d", c[0], c[1])
        clen = len(content) // 2
        shp += struct.pack(">2i", len(shp) // 2, clen) + content
        shx += struct.pack(">2i", offset, clen)
        offset += clen + 4

    # DBF
    fields = fields or _infer_fields(recs)
    today = datetime.date.today()
    n = len(recs)
    hlen = 32 + 32 * len(fields) + 1
    rlen = 1 + sum(f[2] for f in fields)
    dbf = bytearray(struct.pack("<4B2H17x4B", 3, today.year - 1900, today.month, today.day,
                                (n & 0xFF), (n >> 8) & 0xFF, hlen, rlen) if False else b"")
    hdr = bytearray(32)
    hdr[0] = 3
    hdr[1] = today.year - 1900; hdr[2] = today.month; hdr[3] = today.day
    struct.pack_into("<I", hdr, 4, n)
    struct.pack_into("<H", hdr, 8, hlen)
    struct.pack_into("<H", hdr, 10, rlen)
    dbf = hdr
    for name, typ, size, dec in fields:
        fd = bytearray(32)
        nb = name.encode("cp866", "replace")[:11]
        fd[0:11] = nb + b"\0" * (11 - len(nb))
        fd[11] = ord(typ)
        fd[16] = size
        fd[17] = dec
        dbf += fd
    dbf += b"\r"
    for typ, parts, bbox, props in recs:
        row = bytearray(b" ")  # deletion flag
        for name, ftyp, size, dec in fields:
            v = props.get(name, "")
            if ftyp == "N":
                sv = (("%.*f" % (dec, float(v))) if v not in ("", None) else "")[:size]
            else:
                sv = str(v).encode("cp866", "replace").decode("cp866")[:size]
            row += sv.ljust(size).encode("cp866", "replace")[:size]
        dbf += row
    dbf += b"\x1a"

    with open(path_base + ".shp", "wb") as f: f.write(bytes(shp))
    with open(path_base + ".shx", "wb") as f: f.write(bytes(shx))
    with open(path_base + ".dbf", "wb") as f: f.write(bytes(dbf))
    return {"files": [path_base + e for e in (".shp", ".shx", ".dbf")], "records": n, "type": shp_type}


def _infer_fields(recs):
    keys = []
    for r in recs:
        for k in r[3]:
            if k not in keys: keys.append(k)
    fields = []
    for k in keys[:20]:
        vals = [r[3].get(k) for r in recs]
        numeric = all(isinstance(v, (int, float)) for v in vals if v is not None)
        fields.append((k[:10], "N" if numeric else "C", 19 if numeric else 64, 6 if numeric else 0))
    return fields


def shapefile_import(path_base: str) -> List[Dict]:
    with open(path_base + ".shp", "rb") as f: shp = f.read()
    feats = []
    pos = 100
    shp_type = struct.unpack_from("<i", shp, 32)[0]
    while pos + 8 <= len(shp):
        rec_no, clen = struct.unpack_from(">2i", shp, pos)
        content_pos = pos + 8
        cpos = content_pos * 1  # clen в словах
        if clen == 0:
            pos += 8; continue
        st = struct.unpack_from("<i", shp, content_pos)[0]
        if st == SHP_POINT:
            x, y = struct.unpack_from("<2d", shp, content_pos + 4)
            geom = {"type": "Point", "coordinates": [x, y]}
        elif st in (SHP_POLYLINE, SHP_POLYGON):
            bbox = struct.unpack_from("<4d", shp, content_pos + 4)
            nparts, npoints = struct.unpack_from("<2i", shp, content_pos + 36)
            offs = list(struct.unpack_from(f"<{nparts}i", shp, content_pos + 44))
            pts_start = content_pos + 44 + 4 * nparts
            pts = [list(struct.unpack_from("<2d", shp, pts_start + 16 * i)) for i in range(npoints)]
            parts = []
            for i in range(nparts):
                a = offs[i]; b = offs[i + 1] if i + 1 < nparts else npoints
                parts.append(pts[a:b])
            geom = {"type": "LineString" if st == SHP_POLYLINE else "Polygon",
                    "coordinates": parts[0] if st == SHP_POLYLINE else parts}
        else:
            raise ValueError(f"shapefile: неподдержанный тип {st}")
        feats.append({"type": "Feature", "id": f"shp-{len(feats)}", "properties": {}, "geometry": geom})
        pos = content_pos + clen * 2
    # DBF атрибуты
    if os.path.exists(path_base + ".dbf"):
        with open(path_base + ".dbf", "rb") as f: dbf = f.read()
        n = struct.unpack_from("<I", dbf, 4)[0]
        hlen = struct.unpack_from("<H", dbf, 8)[0]
        rlen = struct.unpack_from("<H", dbf, 10)[0]
        nf = (hlen - 33) // 32
        flds = []
        for i in range(nf):
            o = 32 + 32 * i
            nm = dbf[o:o + 11].split(b"\0")[0].decode("cp866", "replace")
            typ = chr(dbf[o + 11]); size = dbf[o + 16]
            flds.append((nm, typ, size))
        for i in range(min(n, len(feats))):
            ro = hlen + i * rlen + 1
            col = 0
            for nm, typ, size in flds:
                raw = dbf[ro + col: ro + col + size].decode("cp866", "replace").strip()
                col += size
                if raw == "": continue
                if typ == "N":
                    try: raw = float(raw) if "." in raw else int(raw)
                    except ValueError: pass
                feats[i]["properties"][nm] = raw
    return feats
