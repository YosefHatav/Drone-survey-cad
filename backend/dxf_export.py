"""DXF writer: produces an AutoCAD-ready drawing in project coordinates with layers,
symbol blocks with elevation attributes, 3D polylines, contours, coordinate grid,
title block and a referenced orthophoto.

Two options shape the output:
* template  – an office DXF (layers, blocks, text styles). Its layers/blocks are reused,
              its model space content is discarded.
* strict    – "מבא"ת / מפרט מיפוי לאומי" drafting rules: no POINT/TEXT/MTEXT/CIRCLE
              entities in model space (all labels are blocks with attributes), colours
              ByLayer, HEBTXT font, 6-decimal precision, -444 for unknown values, sheet
              frame / title / north arrow on a non-printing ("ignore") layer."""
import datetime
import math

import ezdxf
from ezdxf.enums import TextEntityAlignment

from codes import codes_by_key

MISSING = -444.0

EXTRA_LAYERS = [
    ("TOPO-POINTS", 7, "נקודות גובה תלת־ממדיות (לבניית משטח)"),
    ("TOPO-CONTOUR-MAJOR", 34, "קווי גובה ראשיים"),
    ("TOPO-CONTOUR-MINOR", 252, "קווי גובה משניים"),
    ("TOPO-CONTOUR-TEXT", 34, "כיתוב קווי גובה"),
    ("SURV-GRID", 8, "רשת קואורדינטות"),
    ("SURV-TITLE", 7, "טבלת כותרת"),
    ("SURV-CHECK", 3, "נקודות ביקורת"),
    ("ORTHO", 7, "אורתופוטו"),
    ("TOPO-TEXT", 7, "כיתובים"),
]

ELEV_TAGS = {"ELEV", "Z", "H", "HEIGHT", "LEVEL", "גובה", "רום", "HGT", "ELEVATION"}
CODE_TAGS = {"CODE", "DESC", "DESCRIPTION", "קוד", "תיאור"}
NAME_TAGS = {"NAME", "ID", "NUM", "NUMBER", "POINT", "שם", "מספר"}
TEXT_TAGS = {"TEXT", "LABEL", "TXT", "כיתוב", "טקסט"}


def default_options():
    return {"strict": False, "ignore_layer": "1201", "text_style_font": "arial.ttf",
            "hebrew_font": "david.ttf", "layer_map": {}}


class Writer:
    def __init__(self, project, template=None):
        self.p = project
        self.opt = {**default_options(), **project.get("cad", {})}
        self.strict = bool(self.opt["strict"])
        self.lmap = self.opt.get("layer_map") or {}          # internal extra layer -> office layer
        scale = float(project.get("scale", 250))
        self.scale = scale
        self.k = scale / 1000.0                               # metres per paper mm
        self.txt_h = 2.0 * self.k
        self.sym = 2.5 * self.k
        if template:
            self.doc = ezdxf.readfile(template)
            msp = self.doc.modelspace()
            for e in list(msp):
                msp.delete_entity(e)
        else:
            self.doc = ezdxf.new("R2018", setup=True)
        d = self.doc
        d.units = ezdxf.units.M
        d.header["$INSUNITS"] = 6
        d.header["$MEASUREMENT"] = 1
        d.header["$LUNITS"] = 2
        d.header["$LUPREC"] = 6 if self.strict else 3
        if self.strict:
            self.style = self.hstyle = "HEBTXT"
            if "HEBTXT" not in d.styles:
                d.styles.new("HEBTXT", dxfattribs={"font": "hebtxt.shx"})
        else:
            self.style, self.hstyle = "SURVEY", "HEBREW"
            if "SURVEY" not in d.styles:
                d.styles.new("SURVEY", dxfattribs={"font": self.opt["text_style_font"]})
            if "HEBREW" not in d.styles:
                d.styles.new("HEBREW", dxfattribs={"font": self.opt["hebrew_font"]})
        for c in project["codes"]:
            self._layer(c["layer"], c["color"], c["label"])
        for name, color, desc in EXTRA_LAYERS:
            self._layer(self.L(name), color, desc)
        if self.strict:
            self._layer(self.opt["ignore_layer"], 7, "שכבת התעלם – לא מודפסת (מסגרת, כותרת, חץ צפון)")
        if not template and "DASHED" in d.linetypes:
            fence = codes_by_key(project["codes"]).get("FENCE")
            if fence and fence["layer"] in d.layers:
                d.layers.get(fence["layer"]).dxf.linetype = "DASHED"
        self._define_blocks()
        self.msp = d.modelspace()

    # ---------------------------------------------------------------- helpers
    def L(self, name):
        return self.lmap.get(name, name)

    def _layer(self, name, color, desc):
        if name not in self.doc.layers:
            lay = self.doc.layers.add(name, color=color)
            lay.description = desc

    def _sym(self, name, draw):
        if name in self.doc.blocks:
            return
        blk = self.doc.blocks.new(name=name)
        draw(blk)
        blk.add_attdef("ELEV", (0.7, 0.3), dxfattribs={"height": 0.5, "layer": "0", "style": self.style})
        blk.add_attdef("CODE", (0.7, -0.5), dxfattribs={"height": 0.35, "layer": "0", "style": self.style,
                                                          "flags": 1})  # invisible

    def _define_blocks(self):
        # Unit-size symbols (≈1 drawing unit), scaled at insert by the drawing scale.
        # Strict mode forbids CIRCLE entities, so circles are drawn as closed polylines.
        def circ(b, r, c=(0, 0)):
            if self.strict:
                b.add_lwpolyline([(c[0] + r * math.cos(t * math.pi / 12), c[1] + r * math.sin(t * math.pi / 12))
                                  for t in range(24)], close=True)
            else:
                b.add_circle(c, r)

        self._sym("SYM_SPOT", lambda b: (b.add_line((-0.25, 0), (0.25, 0)), b.add_line((0, -0.25), (0, 0.25))))
        self._sym("SYM_POLE", lambda b: (circ(b, 0.3), b.add_line((-0.3, 0), (0.3, 0)), b.add_line((0, -0.3), (0, 0.3))))

        def tree(b):
            circ(b, 0.5)
            circ(b, 0.08)
            for k in range(8):
                a = k * math.pi / 4
                b.add_line((0.5 * math.cos(a), 0.5 * math.sin(a)), (0.65 * math.cos(a), 0.65 * math.sin(a)))
        self._sym("SYM_TREE", tree)
        self._sym("SYM_MH", lambda b: (b.add_lwpolyline([(-0.3, -0.3), (0.3, -0.3), (0.3, 0.3), (-0.3, 0.3)], close=True),
                                       b.add_line((-0.3, -0.3), (0.3, 0.3)), b.add_line((-0.3, 0.3), (0.3, -0.3))))
        self._sym("SYM_HYD", lambda b: (circ(b, 0.3), circ(b, 0.12)))
        self._sym("SYM_SIGN", lambda b: (b.add_lwpolyline([(-0.3, 0), (0.3, 0), (0, 0.5)], close=True),
                                         b.add_line((0, 0), (0, -0.3))))
        self._sym("SYM_GCP", lambda b: (b.add_lwpolyline([(-0.4, -0.25), (0.4, -0.25), (0, 0.45)], close=True),
                                        circ(b, 0.05)))
        if "LBL_TEXT" not in self.doc.blocks:   # label block: the only way to put text in strict mode
            blk = self.doc.blocks.new(name="LBL_TEXT")
            blk.add_attdef("TEXT", (0, 0), dxfattribs={"height": 1.0, "layer": "0", "style": self.style})

    def insert(self, blk, layer, pos, scale, elev=None, code=None, name=None, text=None):
        if blk not in self.doc.blocks:
            blk = "SYM_SPOT"
        ref = self.msp.add_blockref(blk, pos, dxfattribs={"layer": layer, "xscale": scale, "yscale": scale,
                                                           "zscale": scale})
        values = {}
        for ad in self.doc.blocks[blk].attdefs():
            tag = ad.dxf.tag
            up = tag.upper()
            if up in ELEV_TAGS:
                values[tag] = f"{(MISSING if elev is None else elev):.2f}"
            elif up in CODE_TAGS and code is not None:
                values[tag] = code
            elif up in NAME_TAGS and name is not None:
                values[tag] = name
            elif up in TEXT_TAGS and text is not None:
                values[tag] = text
            else:
                values[tag] = ad.dxf.text if ad.dxf.text else (str(int(MISSING)) if self.strict else "")
        if values:
            ref.add_auto_attribs(values)
        return ref

    def label(self, text, pos, h, layer, rotation=0.0, style=None, align=TextEntityAlignment.BOTTOM_LEFT):
        if self.strict:
            ref = self.msp.add_blockref("LBL_TEXT", pos, dxfattribs={"layer": layer, "xscale": h, "yscale": h,
                                                                      "rotation": rotation})
            ref.add_auto_attribs({"TEXT": text})
        else:
            self.msp.add_text(text, height=h, rotation=rotation,
                              dxfattribs={"layer": layer, "style": style or self.style}).set_placement(pos, align=align)

    # ---------------------------------------------------------------- content
    def write(self, features, contours, spots, ortho_ref, checks):
        codes = codes_by_key(self.p["codes"])
        msp, sym = self.msp, self.sym
        if ortho_ref:
            img_path, (w, h), ins, size = ortho_ref
            image_def = self.doc.add_image_def(filename=img_path, size_in_pixel=(w, h))
            img = msp.add_image(image_def, insert=ins, size_in_units=size, rotation=0,
                                dxfattribs={"layer": self.L("ORTHO") if not self.strict else self.opt["ignore_layer"]})
            img.dxf.flags = 1 | 2 | 8

        n = 0
        for f in features:
            c = codes.get(f["code"])
            if not c:
                continue
            layer = c["layer"]
            pts = [(p[0], p[1], p[2] if len(p) > 2 and p[2] is not None else (MISSING if self.strict else 0.0))
                   for p in f["coords"]]
            if f["geom"] == "point":
                e, nn, z = pts[0]
                self.insert(c.get("block") or "SYM_SPOT", layer, (e, nn, z), sym, elev=z, code=f["code"],
                            text=f.get("text"))
                if not self.strict:
                    msp.add_point((e, nn, z), dxfattribs={"layer": self.L("TOPO-POINTS")})
            else:
                msp.add_polyline3d(pts, close=f["geom"] == "polygon", dxfattribs={"layer": layer})
            if f.get("text") and f["geom"] != "point":
                e, nn, _ = pts[0]
                self.label(f["text"], (e, nn + self.txt_h), self.txt_h, self.L("TOPO-TEXT"), style=self.hstyle)
            n += 1

        spot_code = codes.get("SPOT", {"layer": "TOPO-SPOT", "block": "SYM_SPOT"})
        for e, nn, z in spots or []:
            self.insert(spot_code.get("block") or "SYM_SPOT", spot_code["layer"], (e, nn, z), sym, elev=z, code="SPOT")
            if not self.strict:
                msp.add_point((e, nn, z), dxfattribs={"layer": self.L("TOPO-POINTS")})

        for cl in contours or []:
            layer = self.L("TOPO-CONTOUR-MAJOR" if cl["major"] else "TOPO-CONTOUR-MINOR")
            if self.strict:   # 3D polyline with Z on every vertex
                msp.add_polyline3d([(x, y, cl["z"]) for x, y in cl["coords"]], dxfattribs={"layer": layer})
            else:
                msp.add_lwpolyline(cl["coords"], dxfattribs={"layer": layer, "elevation": cl["z"]})
            if cl["major"] and len(cl["coords"]) > 10:
                i = len(cl["coords"]) // 2
                (x1, y1), (x2, y2) = cl["coords"][i - 1], cl["coords"][i + 1]
                ang = math.degrees(math.atan2(y2 - y1, x2 - x1))
                if ang > 90 or ang < -90:
                    ang += 180
                self.label(f"{cl['z']:.1f}", tuple(cl["coords"][i]), self.txt_h * 0.9, self.L("TOPO-CONTOUR-TEXT"),
                           rotation=ang, align=TextEntityAlignment.MIDDLE_CENTER)

        gcp_code = codes.get("GCP", {"layer": "SURV-CONTROL", "block": "SYM_GCP"})
        for ck in checks or []:
            if ck.get("measured") is None:
                continue
            e, nn, z = ck["E"], ck["N"], ck["H"]
            layer = self.L("SURV-CHECK") if ck["kind"] == "check" else gcp_code["layer"]
            self.insert(gcp_code.get("block") or "SYM_GCP", layer, (e, nn, z), sym, elev=z, code=ck["kind"].upper(),
                        name=ck["name"])
            self.label(ck["name"], (e + sym, nn - sym), self.txt_h, layer, align=TextEntityAlignment.TOP_LEFT)

        bounds = self.p.get("bounds")
        if bounds:
            self._grid(bounds)
            self._title_block(bounds)
        return n

    def _grid(self, bounds):
        step = 50 if self.scale <= 500 else 100
        left, bottom, right, top = bounds
        h = self.txt_h
        cross = 1.5 * h
        lay = self.L("SURV-GRID")
        e = math.ceil(left / step) * step
        while e <= right:
            n = math.ceil(bottom / step) * step
            while n <= top:
                self.msp.add_line((e - cross, n), (e + cross, n), dxfattribs={"layer": lay})
                self.msp.add_line((e, n - cross), (e, n + cross), dxfattribs={"layer": lay})
                self.label(f"{int(e)}", (e - h * 0.3, n + cross * 1.2), h * 0.8, lay, rotation=90)
                self.label(f"{int(n)}", (e + cross * 1.2, n + h * 0.3), h * 0.8, lay)
                n += step
            e += step

    def _title_block(self, bounds):
        p, k, h = self.p, self.k, self.txt_h
        meta = p.get("meta", {})
        lay = self.opt["ignore_layer"] if self.strict else self.L("SURV-TITLE")
        left, bottom, right, top = bounds
        w, hh = 120 * k, 60 * k
        x0, y0 = right + 10 * k, bottom
        self.msp.add_lwpolyline([(x0, y0), (x0 + w, y0), (x0 + w, y0 + hh), (x0, y0 + hh)], close=True,
                                dxfattribs={"layer": lay})
        lines = [
            ("פרויקט", p.get("name", "")),
            ("מזמין", meta.get("client", "")),
            ("גוש / חלקה", meta.get("parcel", "")),
            ("מודד מוסמך", meta.get("surveyor", "")),
            ("מס' רישיון", meta.get("license", "")),
            ("תאריך מדידה", meta.get("date") or datetime.date.today().isoformat()),
            ("קנ\"מ", f"1:{int(p.get('scale', 250))}"),
            ("רשת קואורדינטות", "רשת ישראל החדשה (ITM)" if p.get("epsg") == 2039 else f"EPSG:{p.get('epsg')}"),
            ("גובה", meta.get("datum") or "גובה אורתומטרי מעל פני הים"),
            ("שיטה", "פוטוגרמטריה מרחפן"),
        ]
        if self.strict:
            for i, (a, b) in enumerate(lines):
                self.label(f"{a}: {b}", (x0 + w - 2 * k, y0 + hh - (i + 1) * h * 1.8), h * 1.1, lay,
                           align=TextEntityAlignment.BOTTOM_RIGHT)
        else:
            mt = self.msp.add_mtext("\\P".join(f"{a}: {b}" for a, b in lines),
                                    dxfattribs={"layer": lay, "style": self.hstyle, "char_height": h * 1.1,
                                                "width": w - 4 * k})
            mt.set_location((x0 + w - 2 * k, y0 + hh - 2 * k), attachment_point=3)
        ax, ay = x0 + 12 * k, y0 + hh + 15 * k
        self.msp.add_lwpolyline([(ax, ay), (ax - 3 * k, ay - 10 * k), (ax, ay - 7 * k), (ax + 3 * k, ay - 10 * k)],
                                close=True, dxfattribs={"layer": lay})
        self.label("N", (ax, ay + 2 * k), h * 2, lay, align=TextEntityAlignment.BOTTOM_CENTER)

    def save(self, path):
        self.doc.saveas(path, encoding="utf-8")


def build_dxf(project, features, out_path, contours=None, spots=None, ortho_ref=None, checks=None, template=None):
    w = Writer(project, template)
    n = w.write(features, contours, spots, ortho_ref, checks)
    w.save(out_path)
    return n


def read_template(path):
    """Layers and blocks (with attribute tags) available in an office template DXF."""
    doc = ezdxf.readfile(path)
    layers = [{"name": l.dxf.name, "color": l.dxf.color, "description": getattr(l, "description", "") or ""}
              for l in doc.layers if l.dxf.name not in ("Defpoints",)]
    blocks = []
    for b in doc.blocks:
        if b.name.startswith("*") or b.is_any_layout:
            continue
        blocks.append({"name": b.name, "attribs": [a.dxf.tag for a in b.attdefs()]})
    return {"layers": layers, "blocks": blocks, "dxfversion": doc.dxfversion}


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
