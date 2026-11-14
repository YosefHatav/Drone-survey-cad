"""DXF writer: produces an AutoCAD-ready drawing (R2018 DXF) in project coordinates
with layers, symbol blocks with elevation attributes, 3D polylines, contours,
coordinate grid, title block and a referenced orthophoto."""
import datetime
import math

import ezdxf
from ezdxf.enums import TextEntityAlignment

from codes import codes_by_key

EXTRA_LAYERS = [
    ("TOPO-POINTS", 7, "נקודות גובה תלת־ממדיות (לבניית משטח)"),
    ("TOPO-SPOT-TEXT", 7, "כיתוב רומים"),
    ("TOPO-CONTOUR-MAJOR", 34, "קווי גובה ראשיים"),
    ("TOPO-CONTOUR-MINOR", 252, "קווי גובה משניים"),
    ("TOPO-CONTOUR-TEXT", 34, "כיתוב קווי גובה"),
    ("SURV-GRID", 8, "רשת קואורדינטות"),
    ("SURV-TITLE", 7, "טבלת כותרת"),
    ("SURV-CHECK", 3, "נקודות ביקורת"),
    ("ORTHO", 7, "אורתופוטו"),
    ("TOPO-TEXT", 7, "כיתובים"),
]


def _sym(doc, name, draw):
    blk = doc.blocks.new(name=name)
    draw(blk)
    blk.add_attdef("ELEV", (0.7, 0.3), dxfattribs={"height": 0.5, "layer": "0", "style": "SURVEY"})
    blk.add_attdef("CODE", (0.7, -0.5), dxfattribs={"height": 0.35, "layer": "0", "style": "SURVEY",
                                                      "flags": 1})  # invisible
    return blk


def _define_blocks(doc):
    # Unit-size symbols (≈1 drawing unit), scaled at insert by the drawing scale.
    _sym(doc, "SYM_SPOT", lambda b: (b.add_line((-0.25, 0), (0.25, 0)), b.add_line((0, -0.25), (0, 0.25))))
    _sym(doc, "SYM_POLE", lambda b: (b.add_circle((0, 0), 0.3), b.add_line((-0.3, 0), (0.3, 0)),
                                     b.add_line((0, -0.3), (0, 0.3))))

    def tree(b):
        b.add_circle((0, 0), 0.5)
        b.add_circle((0, 0), 0.08)
        for k in range(8):
            a = k * math.pi / 4
            b.add_line((0.5 * math.cos(a), 0.5 * math.sin(a)), (0.65 * math.cos(a), 0.65 * math.sin(a)))
    _sym(doc, "SYM_TREE", tree)
    _sym(doc, "SYM_MH", lambda b: (b.add_lwpolyline([(-0.3, -0.3), (0.3, -0.3), (0.3, 0.3), (-0.3, 0.3)], close=True),
                                   b.add_line((-0.3, -0.3), (0.3, 0.3)), b.add_line((-0.3, 0.3), (0.3, -0.3))))
    _sym(doc, "SYM_HYD", lambda b: (b.add_circle((0, 0), 0.3), b.add_circle((0, 0), 0.12)))
    _sym(doc, "SYM_SIGN", lambda b: (b.add_lwpolyline([(-0.3, 0), (0.3, 0), (0, 0.5)], close=True),
                                     b.add_line((0, 0), (0, -0.3))))
    _sym(doc, "SYM_GCP", lambda b: (b.add_lwpolyline([(-0.4, -0.25), (0.4, -0.25), (0, 0.45)], close=True),
                                    b.add_circle((0, 0), 0.05)))


def build_dxf(project, features, out_path, contours=None, spots=None, ortho_ref=None,
              checks=None):
    """project: dict with name, epsg, scale, meta; features: list of dicts
    {code, geom: point|line|polygon, coords:[[e,n,z],...], text?}."""
    codes = codes_by_key(project["codes"])
    scale = float(project.get("scale", 250))
    k = scale / 1000.0                  # drawing units (m) per paper mm
    txt_h = 2.0 * k                     # 2 mm text on paper
    sym = 2.5 * k                       # ~2.5 mm symbols

    doc = ezdxf.new("R2018", setup=True)
    doc.units = ezdxf.units.M
    doc.header["$INSUNITS"] = 6
    doc.header["$MEASUREMENT"] = 1
    doc.header["$LUNITS"] = 2
    doc.header["$LUPREC"] = 3
    doc.styles.new("SURVEY", dxfattribs={"font": "arial.ttf"})
    doc.styles.new("HEBREW", dxfattribs={"font": "david.ttf"})

    for c in project["codes"]:
        if c["layer"] not in doc.layers:
            lay = doc.layers.add(c["layer"], color=c["color"])
            lay.description = c["label"]
    for name, color, desc in EXTRA_LAYERS:
        if name not in doc.layers:
            lay = doc.layers.add(name, color=color)
            lay.description = desc
    if "TOPO-FENCE" in doc.layers and "DASHED" in doc.linetypes:
        doc.layers.get("TOPO-FENCE").dxf.linetype = "DASHED"
    _define_blocks(doc)
    msp = doc.modelspace()

    if ortho_ref:
        img_path, (w, h), insert, size = ortho_ref
        image_def = doc.add_image_def(filename=img_path, size_in_pixel=(w, h))
        img = msp.add_image(image_def, insert=insert, size_in_units=size, rotation=0,
                            dxfattribs={"layer": "ORTHO"})
        img.dxf.flags = 1 | 2 | 8  # show, show unaligned, use clip

    n_written = 0
    for f in features:
        c = codes.get(f["code"])
        if not c:
            continue
        layer = c["layer"]
        pts = [tuple(p) if len(p) == 3 and p[2] is not None else (p[0], p[1], 0.0) for p in f["coords"]]
        if f["geom"] == "point":
            e, n, z = pts[0]
            blk = c.get("block") or "SYM_SPOT"
            ref = msp.add_blockref(blk, (e, n, z), dxfattribs={"layer": layer, "xscale": sym, "yscale": sym,
                                                               "zscale": sym})
            ref.add_auto_attribs({"ELEV": f"{z:.2f}", "CODE": f["code"]})
            msp.add_point((e, n, z), dxfattribs={"layer": "TOPO-POINTS"})
        else:
            closed = f["geom"] == "polygon"
            msp.add_polyline3d(pts, close=closed, dxfattribs={"layer": layer})
        if f.get("text"):
            e, n, _ = pts[0]
            msp.add_text(f["text"], height=txt_h, dxfattribs={"layer": "TOPO-TEXT", "style": "HEBREW"}
                         ).set_placement((e, n + txt_h), align=TextEntityAlignment.BOTTOM_LEFT)
        n_written += 1

    for e, n, z in spots or []:
        ref = msp.add_blockref("SYM_SPOT", (e, n, z), dxfattribs={"layer": "TOPO-SPOT", "xscale": sym,
                                                                  "yscale": sym, "zscale": sym})
        ref.add_auto_attribs({"ELEV": f"{z:.2f}", "CODE": "SPOT"})
        msp.add_point((e, n, z), dxfattribs={"layer": "TOPO-POINTS"})

    for cl in contours or []:
        layer = "TOPO-CONTOUR-MAJOR" if cl["major"] else "TOPO-CONTOUR-MINOR"
        pl = msp.add_lwpolyline(cl["coords"], dxfattribs={"layer": layer, "elevation": cl["z"]})
        if cl["major"] and len(cl["coords"]) > 10:
            i = len(cl["coords"]) // 2
            (x1, y1), (x2, y2) = cl["coords"][i - 1], cl["coords"][i + 1]
            ang = math.degrees(math.atan2(y2 - y1, x2 - x1))
            if ang > 90 or ang < -90:
                ang += 180
            msp.add_text(f"{cl['z']:.1f}", height=txt_h * 0.9, rotation=ang,
                         dxfattribs={"layer": "TOPO-CONTOUR-TEXT", "style": "SURVEY", "elevation": cl["z"]}
                         ).set_placement(cl["coords"][i], align=TextEntityAlignment.MIDDLE_CENTER)
        del pl

    for ck in checks or []:
        if ck.get("measured") is None:
            continue
        e, n, z = ck["E"], ck["N"], ck["H"]
        ref = msp.add_blockref("SYM_GCP", (e, n, z), dxfattribs={"layer": "SURV-CHECK" if ck["kind"] == "check"
                                                                  else "SURV-CONTROL",
                                                                  "xscale": sym, "yscale": sym, "zscale": sym})
        ref.add_auto_attribs({"ELEV": f"{z:.3f}", "CODE": ck["name"]})
        msp.add_text(ck["name"], height=txt_h, dxfattribs={"layer": "SURV-CHECK", "style": "SURVEY"}
                     ).set_placement((e + sym, n - sym), align=TextEntityAlignment.TOP_LEFT)

    bounds = project.get("bounds")
    if bounds:
        _grid(msp, bounds, scale, txt_h)
        _title_block(msp, project, bounds, k, txt_h)

    doc.saveas(out_path, encoding="utf-8")
    return n_written


def _grid(msp, bounds, scale, txt_h):
    step = 50 if scale <= 500 else 100
    left, bottom, right, top = bounds
    cross = 1.5 * txt_h
    e = math.ceil(left / step) * step
    while e <= right:
        n = math.ceil(bottom / step) * step
        while n <= top:
            msp.add_line((e - cross, n), (e + cross, n), dxfattribs={"layer": "SURV-GRID"})
            msp.add_line((e, n - cross), (e, n + cross), dxfattribs={"layer": "SURV-GRID"})
            msp.add_text(f"{int(e)}", height=txt_h * 0.8, rotation=90, dxfattribs={"layer": "SURV-GRID",
                                                                                  "style": "SURVEY"}
                         ).set_placement((e - txt_h * 0.3, n + cross * 1.2), align=TextEntityAlignment.BOTTOM_LEFT)
            msp.add_text(f"{int(n)}", height=txt_h * 0.8, dxfattribs={"layer": "SURV-GRID", "style": "SURVEY"}
                         ).set_placement((e + cross * 1.2, n + txt_h * 0.3), align=TextEntityAlignment.BOTTOM_LEFT)
            n += step
        e += step


def _title_block(msp, project, bounds, k, txt_h):
    meta = project.get("meta", {})
    left, bottom, right, top = bounds
    w, h = 120 * k, 60 * k
    x0, y0 = right + 10 * k, bottom
    msp.add_lwpolyline([(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)], close=True,
                       dxfattribs={"layer": "SURV-TITLE"})
    lines = [
        ("פרויקט", project.get("name", "")),
        ("מזמין", meta.get("client", "")),
        ("גוש / חלקה", meta.get("parcel", "")),
        ("מודד מוסמך", meta.get("surveyor", "")),
        ("מס' רישיון", meta.get("license", "")),
        ("תאריך מדידה", meta.get("date", datetime.date.today().isoformat())),
        ("קנ\"מ", f"1:{int(project.get('scale', 250))}"),
        ("רשת קואורדינטות", "רשת ישראל החדשה (ITM)" if project.get("epsg") == 2039 else f"EPSG:{project.get('epsg')}"),
        ("גובה", meta.get("datum", "גובה אורתומטרי מעל פני הים")),
        ("שיטה", "פוטוגרמטריה מרחפן"),
    ]
    text = "\\P".join(f"{a}: {b}" for a, b in lines)
    mt = msp.add_mtext(text, dxfattribs={"layer": "SURV-TITLE", "style": "HEBREW", "char_height": txt_h * 1.1,
                                         "width": w - 4 * k})
    mt.set_location((x0 + w - 2 * k, y0 + h - 2 * k), attachment_point=3)  # top-right, RTL friendly
    # north arrow
    ax, ay = x0 + 12 * k, y0 + h + 15 * k
    msp.add_lwpolyline([(ax, ay), (ax - 3 * k, ay - 10 * k), (ax, ay - 7 * k), (ax + 3 * k, ay - 10 * k)],
                       close=True, dxfattribs={"layer": "SURV-TITLE"})
    msp.add_text("N", height=txt_h * 2, dxfattribs={"layer": "SURV-TITLE", "style": "SURVEY"}
                 ).set_placement((ax, ay + 2 * k), align=TextEntityAlignment.BOTTOM_CENTER)


def write_pnezd_csv(features, spots, out_path):
    """Point file in PNEZD (Point, Northing, Easting, Elevation, Description) for Civil 3D / Carlson."""
    i = 1
    with open(out_path, "w", encoding="utf-8") as f:
        for ft in features:
            if ft["geom"] != "point":
                continue
            e, n, z = ft["coords"][0]
            f.write(f"{i},{n:.3f},{e:.3f},{(z or 0):.3f},{ft['code']}\n")
            i += 1
        for e, n, z in spots or []:
            f.write(f"{i},{n:.3f},{e:.3f},{z:.3f},SPOT\n")
            i += 1
    return i - 1
