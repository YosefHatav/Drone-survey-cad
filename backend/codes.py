"""Feature-code library: maps survey codes to CAD layers, colors, geometry type and
which surface the elevation is sampled from.

surface: "ground" -> DTM if available, else DSM (curbs, road edges, spot heights)
         "top"    -> DSM (roof edges, top of wall)
Colors are AutoCAD Color Index (ACI).
Offices can override this table with a JSON file (see load_codes)."""
import json
import os

DEFAULT_CODES = [
    # code,        Hebrew label,           layer,              geom,      aci, surface, block
    ("BLDG",       "מבנה",                 "TOPO-BUILDING",    "polygon", 1,   "ground", None),
    ("ROOF",       "קו גג",                "TOPO-ROOF",        "polygon", 6,   "top",    None),
    ("CURB",       "אבן שפה",              "TOPO-CURB",        "line",    3,   "ground", None),
    ("ROAD",       "שולי כביש / אספלט",    "TOPO-ROAD-EDGE",   "line",    8,   "ground", None),
    ("SIDEWALK",   "מדרכה",                "TOPO-SIDEWALK",    "line",    9,   "ground", None),
    ("FENCE",      "גדר",                  "TOPO-FENCE",       "line",    30,  "ground", None),
    ("WALL",       "קיר / קיר תומך",       "TOPO-WALL",        "line",    5,   "top",    None),
    ("WALL_BASE",  "בסיס קיר",             "TOPO-WALL-BASE",   "line",    5,   "ground", None),
    ("TOB",        "ראש מדרון",            "TOPO-TOP-BANK",    "line",    2,   "ground", None),
    ("TOE",        "רגל מדרון",            "TOPO-TOE-BANK",    "line",    2,   "ground", None),
    ("BRK",        "קו שבר",               "TOPO-BREAKLINE",   "line",    4,   "ground", None),
    ("PAINT",      "סימון כביש",           "TOPO-ROAD-MARKING","line",    7,   "ground", None),
    ("DITCH",      "תעלה",                 "TOPO-DITCH",       "line",    150, "ground", None),
    ("VEG",        "גבול צמחייה",          "TOPO-VEGETATION",  "polygon", 82,  "ground", None),
    ("POLE",       "עמוד חשמל/תאורה",      "TOPO-POLE",        "point",   1,   "ground", "SYM_POLE"),
    ("TREE",       "עץ",                   "TOPO-TREE",        "point",   94,  "ground", "SYM_TREE"),
    ("MH",         "שוחה / תא ביקורת",     "TOPO-MANHOLE",     "point",   140, "ground", "SYM_MH"),
    ("HYD",        "ברז כיבוי",            "TOPO-HYDRANT",     "point",   1,   "ground", "SYM_HYD"),
    ("SIGN",       "תמרור",                "TOPO-SIGN",        "point",   7,   "ground", "SYM_SIGN"),
    ("SPOT",       "רום (נקודת גובה)",     "TOPO-SPOT",        "point",   7,   "ground", "SYM_SPOT"),
    ("AREA",       "גבול אזור עבודה",      "SURV-WORK-AREA",   "polygon", 6,   "ground", None),
    ("GCP",        "נקודת בקרה",           "SURV-CONTROL",     "point",   1,   "ground", "SYM_GCP"),
]

FIELDS = ("code", "label", "layer", "geom", "color", "surface", "block")


def load_codes(path=None):
    path = path or os.environ.get("DSC_CODES_FILE")
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return [dict(zip(FIELDS, row)) for row in DEFAULT_CODES]


def codes_by_key(codes):
    return {c["code"]: c for c in codes}
