"""Drone Survey CAD — web server.

Run:  uvicorn app:app --app-dir backend --port 8000
Data: $DSC_DATA (default ./data)"""
import csv
import io
import json
import math
import os
import shutil
import tempfile
import threading
import time
import uuid
import zipfile

from fastapi import FastAPI, File, HTTPException, UploadFile, Form
from fastapi.responses import FileResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import raster
from codes import load_codes, codes_by_key
from dxf_export import build_dxf, write_pnezd_csv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("DSC_DATA", os.path.join(ROOT, "data"))
NODEODM_URL = os.environ.get("NODEODM_URL", "http://localhost:3000")
os.makedirs(DATA, exist_ok=True)

app = FastAPI(title="Drone Survey CAD")
_lock = threading.Lock()


# ---------------------------------------------------------------- storage
def pdir(pid):
    d = os.path.join(DATA, os.path.basename(pid))
    if not os.path.isdir(d):
        raise HTTPException(404, "פרויקט לא נמצא")
    return d


def load(pid):
    with open(os.path.join(pdir(pid), "project.json"), encoding="utf-8") as f:
        return json.load(f)


def save(pid, proj):
    path = os.path.join(pdir(pid), "project.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(proj, f, ensure_ascii=False)
    os.replace(tmp, path)


def raster_path(pid, kind):
    p = os.path.join(pdir(pid), f"{kind}.tif")
    return p if os.path.exists(p) else None


def ground_dem(pid):
    return raster_path(pid, "dtm") or raster_path(pid, "dsm")


def elev(pid, proj, pts, surface="ground"):
    path = raster_path(pid, "dsm") if surface == "top" else ground_dem(pid)
    if not path:
        return [None] * len(pts)
    off = proj.get("vertical_offset", 0.0)
    return [None if z is None else round(z + off, 3) for z in raster.sample_bilinear(path, pts)]


# ---------------------------------------------------------------- projects
class ProjectIn(BaseModel):
    name: str
    epsg: int = 2039
    scale: int = 250
    meta: dict = {}


@app.get("/api/codes")
def get_codes():
    return load_codes()


@app.get("/api/projects")
def list_projects():
    out = []
    for d in sorted(os.listdir(DATA)):
        f = os.path.join(DATA, d, "project.json")
        if os.path.exists(f):
            with open(f, encoding="utf-8") as fh:
                p = json.load(fh)
            out.append({"id": d, "name": p["name"], "created": p.get("created")})
    return out


@app.post("/api/projects")
def create_project(body: ProjectIn):
    pid = uuid.uuid4().hex[:10]
    os.makedirs(os.path.join(DATA, pid))
    proj = {"id": pid, "name": body.name, "epsg": body.epsg, "scale": body.scale, "meta": body.meta,
            "created": time.strftime("%Y-%m-%d %H:%M"), "codes": load_codes(), "features": [],
            "control": [], "rasters": {}, "contours": [], "spots": [], "vertical_offset": 0.0,
            "generate": {}}
    save(pid, proj)
    return proj


@app.get("/api/projects/{pid}")
def get_project(pid: str):
    return load(pid)


class ProjectPatch(BaseModel):
    name: str | None = None
    scale: int | None = None
    meta: dict | None = None
    vertical_offset: float | None = None


@app.patch("/api/projects/{pid}")
def patch_project(pid: str, body: ProjectPatch):
    with _lock:
        proj = load(pid)
        for k, v in body.model_dump(exclude_none=True).items():
            proj[k] = v
        if body.vertical_offset is not None:
            _resample_all(pid, proj)
            _residuals(pid, proj)
        save(pid, proj)
    return proj


@app.post("/api/projects/{pid}/upload/{kind}")
def upload_raster(pid: str, kind: str, file: UploadFile = File(...)):
    if kind not in ("ortho", "dsm", "dtm"):
        raise HTTPException(400, "סוג קובץ לא נתמך")
    proj = load(pid)
    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False, dir=pdir(pid)) as tmp:
        shutil.copyfileobj(file.file, tmp)
    try:
        info = _import(pid, proj, kind, tmp.name)
    finally:
        os.unlink(tmp.name)
    return info


def _import(pid, proj, kind, src):
    try:
        info = raster.import_raster(src, os.path.join(pdir(pid), f"{kind}.tif"), proj["epsg"], is_dem=kind != "ortho")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"שגיאה בקריאת הקובץ: {e}")
    with _lock:
        proj = load(pid)
        proj["rasters"][kind] = info
        proj["bounds"] = proj["rasters"].get("ortho", info)["bounds"]
        if kind != "ortho":
            _resample_all(pid, proj)
            _residuals(pid, proj)
        save(pid, proj)
    return info


@app.get("/api/projects/{pid}/tiles/{kind}/{z}/{x}/{y}.png")
def tile(pid: str, kind: str, z: int, x: int, y: int):
    proj = load(pid)
    path = raster_path(pid, kind)
    if not path or "bounds" not in proj:
        raise HTTPException(404)
    left, _, _, top = proj["bounds"]
    rng = None
    if kind in ("dsm", "dtm"):
        r = proj["rasters"][kind]
        rng = (r["zmin"], r["zmax"])
    png = raster.render_tile(path, left, top, z, x, y, rng)
    if png is None:
        return Response(status_code=204)
    return Response(png, media_type="image/png", headers={"Cache-Control": "max-age=3600"})


@app.get("/api/projects/{pid}/elev")
def get_elev(pid: str, e: float, n: float):
    proj = load(pid)
    return {"ground": elev(pid, proj, [(e, n)], "ground")[0], "top": elev(pid, proj, [(e, n)], "top")[0]}


# ---------------------------------------------------------------- features
class Feature(BaseModel):
    id: str | None = None
    code: str
    geom: str
    coords: list[list[float | None]]
    text: str | None = None
    zlock: bool = False   # keep user-entered Z instead of sampling


def _sample_feature(pid, proj, f, codes):
    if f.get("zlock"):
        return f
    c = codes.get(f["code"], {})
    zs = elev(pid, proj, [(p[0], p[1]) for p in f["coords"]], c.get("surface", "ground"))
    f["coords"] = [[p[0], p[1], z] for p, z in zip(f["coords"], zs)]
    return f


def _resample_all(pid, proj):
    codes = codes_by_key(proj["codes"])
    for f in proj["features"]:
        _sample_feature(pid, proj, f, codes)


@app.post("/api/projects/{pid}/features")
def upsert_feature(pid: str, feat: Feature):
    with _lock:
        proj = load(pid)
        codes = codes_by_key(proj["codes"])
        if feat.code not in codes:
            raise HTTPException(400, "קוד לא מוכר")
        f = feat.model_dump()
        f["id"] = f["id"] or uuid.uuid4().hex[:12]
        _sample_feature(pid, proj, f, codes)
        proj["features"] = [x for x in proj["features"] if x["id"] != f["id"]] + [f]
        save(pid, proj)
    return f


@app.delete("/api/projects/{pid}/features/{fid}")
def delete_feature(pid: str, fid: str):
    with _lock:
        proj = load(pid)
        proj["features"] = [x for x in proj["features"] if x["id"] != fid]
        save(pid, proj)
    return {"ok": True}


# ---------------------------------------------------------------- control / accuracy
def _parse_points_csv(text):
    rows = []
    sample = text[:2000]
    delim = "\t" if "\t" in sample else ("," if "," in sample else None)
    for line in text.splitlines():
        parts = [p.strip() for p in (line.split(delim) if delim else line.split())]
        if len(parts) < 4:
            continue
        try:
            e, n, h = float(parts[1]), float(parts[2]), float(parts[3])
        except ValueError:
            continue  # header row
        kind = "check" if len(parts) > 4 and parts[4].lower() in ("check", "cp", "ביקורת", "chk") else "gcp"
        rows.append({"name": parts[0], "E": e, "N": n, "H": h, "kind": kind, "measured": None})
    return rows


@app.post("/api/projects/{pid}/control")
async def upload_control(pid: str, file: UploadFile = File(...), swap_en: bool = Form(False)):
    text = (await file.read()).decode("utf-8-sig", errors="replace")
    rows = _parse_points_csv(text)
    if swap_en:
        for r in rows:
            r["E"], r["N"] = r["N"], r["E"]
    if not rows:
        raise HTTPException(400, "לא נמצאו נקודות. פורמט: שם,E,N,H[,gcp|check]")
    with _lock:
        proj = load(pid)
        proj["control"] = rows
        _residuals(pid, proj)
        save(pid, proj)
    return proj["control"]


class Measure(BaseModel):
    name: str
    e: float | None = None
    n: float | None = None
    kind: str | None = None


@app.post("/api/projects/{pid}/control/measure")
def measure_control(pid: str, m: Measure):
    """User clicked the target centre on the orthophoto."""
    with _lock:
        proj = load(pid)
        for r in proj["control"]:
            if r["name"] == m.name:
                if m.kind:
                    r["kind"] = m.kind
                r["measured"] = None if m.e is None else [m.e, m.n]
        _residuals(pid, proj)
        save(pid, proj)
    return accuracy(pid)


def _residuals(pid, proj):
    for r in proj["control"]:
        z = elev(pid, proj, [(r["E"], r["N"])], "ground")[0]
        r["dH"] = None if z is None else round(z - r["H"], 3)
        if r.get("measured"):
            r["dE"] = round(r["measured"][0] - r["E"], 3)
            r["dN"] = round(r["measured"][1] - r["N"], 3)
        else:
            r["dE"] = r["dN"] = None


def _rmse(vals):
    vals = [v for v in vals if v is not None]
    return round(math.sqrt(sum(v * v for v in vals) / len(vals)), 3) if vals else None


@app.get("/api/projects/{pid}/accuracy")
def accuracy(pid: str):
    proj = load(pid)
    out = {}
    for kind in ("gcp", "check"):
        pts = [r for r in proj["control"] if r["kind"] == kind]
        dh = [r.get("dH") for r in pts]
        dxy = [math.hypot(r["dE"], r["dN"]) if r.get("dE") is not None else None for r in pts]
        valid_dh = [v for v in dh if v is not None]
        out[kind] = {"count": len(pts), "rmse_h": _rmse(dh), "rmse_xy": _rmse(dxy),
                     "mean_dh": round(sum(valid_dh) / len(valid_dh), 3) if valid_dh else None,
                     "max_dh": max((abs(v) for v in valid_dh), default=None)}
    out["points"] = proj["control"]
    out["vertical_offset"] = proj.get("vertical_offset", 0.0)
    return out


# ---------------------------------------------------------------- auto generation
class GenIn(BaseModel):
    spot_spacing: float | None = 10.0
    contour_interval: float | None = 0.5
    major_every: int = 5
    smooth: float = 1.0
    boundary_id: str | None = None   # polygon feature limiting the work area


@app.post("/api/projects/{pid}/generate")
def generate(pid: str, g: GenIn):
    proj = load(pid)
    dem = ground_dem(pid)
    if not dem:
        raise HTTPException(400, "יש להעלות DSM או DTM קודם")
    poly = None
    if g.boundary_id:
        f = next((x for x in proj["features"] if x["id"] == g.boundary_id), None)
        if f:
            poly = [(p[0], p[1]) for p in f["coords"]]
    holes = [[(p[0], p[1]) for p in f["coords"]] for f in proj["features"]
             if f["geom"] == "polygon" and f["code"] in ("BLDG", "ROOF")]
    off = proj.get("vertical_offset", 0.0)
    spots = []
    if g.spot_spacing:
        spots = [(e, n, round(z + off, 3)) for e, n, z in raster.spot_grid(dem, g.spot_spacing, poly, holes)]
    cl = []
    if g.contour_interval:
        cl = raster.contours(dem, g.contour_interval, g.major_every, g.smooth, poly, holes)
        for c in cl:
            c["z"] = round(c["z"] + off, 3)
            c["coords"] = [[round(x, 3), round(y, 3)] for x, y in c["coords"]]
    with _lock:
        proj = load(pid)
        proj["spots"] = spots
        proj["contours"] = cl
        proj["generate"] = g.model_dump()
        save(pid, proj)
    return {"spots": len(spots), "contours": len(cl)}


@app.get("/api/projects/{pid}/generated")
def generated(pid: str):
    proj = load(pid)
    return {"spots": proj["spots"], "contours": proj["contours"]}


# ---------------------------------------------------------------- export
@app.get("/api/projects/{pid}/export.dxf")
def export_dxf(pid: str):
    proj = load(pid)
    out = os.path.join(pdir(pid), "drawing.dxf")
    build_dxf(proj, proj["features"], out, proj["contours"], proj["spots"], None, proj["control"])
    return FileResponse(out, filename=f"{_safe(proj['name'])}.dxf", media_type="application/dxf")


@app.get("/api/projects/{pid}/export.zip")
def export_zip(pid: str):
    proj = load(pid)
    d = pdir(pid)
    name = _safe(proj["name"])
    work = tempfile.mkdtemp(dir=d)
    try:
        ortho_ref = None
        if raster_path(pid, "ortho"):
            jpg = os.path.join(work, f"{name}_ortho.jpg")
            w, h, ins, size = raster.export_ortho_jpeg(raster_path(pid, "ortho"), jpg)
            ortho_ref = (os.path.basename(jpg), (w, h), ins, size)
        build_dxf(proj, proj["features"], os.path.join(work, f"{name}.dxf"), proj["contours"], proj["spots"],
                  ortho_ref, proj["control"])
        write_pnezd_csv(proj["features"], proj["spots"], os.path.join(work, f"{name}_points_PNEZD.csv"))
        with open(os.path.join(work, f"{name}_accuracy.txt"), "w", encoding="utf-8") as f:
            f.write(_accuracy_report(proj, accuracy(pid)))
        zpath = os.path.join(d, "export.zip")
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for fn in os.listdir(work):
                z.write(os.path.join(work, fn), fn)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return FileResponse(zpath, filename=f"{name}.zip", media_type="application/zip")


def _safe(s):
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in s) or "project"


def _accuracy_report(proj, acc):
    lines = [f"דוח דיוק - {proj['name']}", f"מערכת קואורדינטות: EPSG:{proj['epsg']}",
             f"תיקון גובה שהוחל: {acc['vertical_offset']:+.3f} מ'", ""]
    for kind, title in (("gcp", "נקודות בקרה (GCP)"), ("check", "נקודות ביקורת (Check)")):
        a = acc[kind]
        lines.append(f"{title}: {a['count']}  RMSE מישורי={a['rmse_xy']}  RMSE גובה={a['rmse_h']}  "
                     f"ממוצע dH={a['mean_dh']}  מקס |dH|={a['max_dh']}")
    lines += ["", "name,kind,E,N,H,dE,dN,dH"]
    for r in acc["points"]:
        lines.append(f"{r['name']},{r['kind']},{r['E']},{r['N']},{r['H']},{r.get('dE')},{r.get('dN')},{r.get('dH')}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- photogrammetry (NodeODM)
_odm_jobs = {}


@app.post("/api/projects/{pid}/odm")
async def start_odm(pid: str, images: list[UploadFile] = File(...), gcp: UploadFile | None = File(None),
                    quality: str = Form("high")):
    """Send raw drone photos to a NodeODM server (OpenDroneMap) and import the results."""
    import requests
    d = pdir(pid)
    up = os.path.join(d, "odm_upload")
    shutil.rmtree(up, ignore_errors=True)
    os.makedirs(up)
    files = []
    for im in images:
        p = os.path.join(up, os.path.basename(im.filename))
        with open(p, "wb") as f:
            shutil.copyfileobj(im.file, f)
        files.append(p)
    if gcp is not None:
        p = os.path.join(up, "gcp_list.txt")
        with open(p, "wb") as f:
            shutil.copyfileobj(gcp.file, f)
        files.append(p)
    opts = [{"name": "dsm", "value": True}, {"name": "dtm", "value": True},
            {"name": "orthophoto-resolution", "value": 2.0 if quality == "high" else 5.0},
            {"name": "dem-resolution", "value": 5.0 if quality == "high" else 10.0},
            {"name": "pc-quality", "value": "high" if quality == "high" else "medium"},
            {"name": "feature-quality", "value": "high" if quality == "high" else "medium"}]
    try:
        handles = [("images", (os.path.basename(p), open(p, "rb"))) for p in files]
        r = requests.post(f"{NODEODM_URL}/task/new", files=handles,
                          data={"options": json.dumps(opts), "name": pid}, timeout=600)
        for _, (_, fh) in handles:
            fh.close()
        r.raise_for_status()
        task = r.json()["uuid"]
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"שרת העיבוד (NodeODM) לא זמין בכתובת {NODEODM_URL}: {e}")
    _odm_jobs[pid] = {"uuid": task, "status": "running", "progress": 0}
    threading.Thread(target=_poll_odm, args=(pid, task), daemon=True).start()
    return _odm_jobs[pid]


def _poll_odm(pid, task):
    import requests
    job = _odm_jobs[pid]
    try:
        while True:
            info = requests.get(f"{NODEODM_URL}/task/{task}/info", timeout=60).json()
            job["progress"] = info.get("progress", 0)
            code = info.get("status", {}).get("code")
            if code == 40:
                break
            if code in (30, 50):
                job["status"] = "failed"
                job["error"] = info.get("status", {}).get("errorMessage", "")
                return
            time.sleep(10)
        job["status"] = "downloading"
        zpath = os.path.join(pdir(pid), "odm_all.zip")
        with requests.get(f"{NODEODM_URL}/task/{task}/download/all.zip", stream=True, timeout=600) as r:
            r.raise_for_status()
            with open(zpath, "wb") as f:
                shutil.copyfileobj(r.raw, f)
        ext = os.path.join(pdir(pid), "odm_out")
        with zipfile.ZipFile(zpath) as z:
            z.extractall(ext)
        proj = load(pid)
        for kind, rel in (("ortho", "odm_orthophoto/odm_orthophoto.tif"), ("dsm", "odm_dem/dsm.tif"),
                          ("dtm", "odm_dem/dtm.tif")):
            src = os.path.join(ext, rel)
            if os.path.exists(src):
                job["status"] = f"importing {kind}"
                _import(pid, proj, kind, src)
        job["status"] = "done"
    except Exception as e:  # noqa: BLE001
        job["status"] = "failed"
        job["error"] = str(e)


@app.get("/api/projects/{pid}/odm")
def odm_status(pid: str):
    return _odm_jobs.get(pid, {"status": "none"})


app.mount("/", StaticFiles(directory=os.path.join(ROOT, "frontend"), html=True), name="ui")
