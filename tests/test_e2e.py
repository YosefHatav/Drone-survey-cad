"""End-to-end test of the whole workflow on the synthetic sample site.
run: python -m pytest tests -q"""
import io
import os
import subprocess
import sys
import zipfile

import ezdxf
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    data = tmp_path_factory.mktemp("data")
    sample = tmp_path_factory.mktemp("sample")
    subprocess.check_call([sys.executable, os.path.join(ROOT, "scripts", "make_sample.py"), str(sample)])
    os.environ["DSC_DATA"] = str(data)
    from fastapi.testclient import TestClient
    import app as appmod
    appmod.DATA = str(data)
    c = TestClient(appmod.app)
    c.sample = sample
    return c


def test_full_workflow(client, tmp_path):
    c, s = client, client.sample
    p = c.post("/api/projects", json={"name": "בדיקה", "scale": 250, "meta": {"surveyor": "ישראל ישראלי"}}).json()
    pid = p["id"]
    for kind in ("ortho", "dsm", "dtm"):
        with open(s / f"{kind}.tif", "rb") as f:
            r = c.post(f"/api/projects/{pid}/upload/{kind}", files={"file": (f"{kind}.tif", f)})
        assert r.status_code == 200, r.text

    # tiles render
    r = c.get(f"/api/projects/{pid}/tiles/ortho/0/0/0.png")
    assert r.status_code == 200 and r.content[:4] == b"\x89PNG"
    r = c.get(f"/api/projects/{pid}/tiles/ortho/-3/0/0.png")
    assert r.status_code == 200

    # elevation sampling: ground at road vs top of building
    z = c.get(f"/api/projects/{pid}/elev", params={"e": 180077.0, "n": 665100 - 66.0}).json()
    assert z["top"] - z["ground"] > 5.5

    # control points: vertical bias of 0.12 must be detected and correctable
    with open(s / "control.csv", "rb") as f:
        c.post(f"/api/projects/{pid}/control", files={"file": ("control.csv", f)})
    acc = c.get(f"/api/projects/{pid}/accuracy").json()
    assert acc["gcp"]["count"] == 4 and acc["check"]["count"] == 3
    assert abs(acc["gcp"]["mean_dh"] - 0.12) < 0.03
    c.patch(f"/api/projects/{pid}", json={"vertical_offset": -acc["gcp"]["mean_dh"]})
    acc = c.get(f"/api/projects/{pid}/accuracy").json()
    assert acc["check"]["rmse_h"] < 0.03
    # horizontal measurement of a check point clicked 2cm off
    acc = c.post(f"/api/projects/{pid}/control/measure", json={"name": "CHK1", "e": 180060.02, "n": 665070.0}).json()
    assert abs(acc["check"]["rmse_xy"] - 0.02) < 1e-6 or acc["check"]["rmse_xy"] < 0.03

    # draw features
    curb = c.post(f"/api/projects/{pid}/features", json={"code": "CURB", "geom": "line",
                  "coords": [[180002, 665060.2], [180060, 665060.2], [180118, 665060.2]]}).json()
    assert all(pt[2] is not None for pt in curb["coords"])
    c.post(f"/api/projects/{pid}/features", json={"code": "BLDG", "geom": "polygon",
           "coords": [[180070, 665040], [180085, 665040], [180085, 665028], [180070, 665028]]})
    c.post(f"/api/projects/{pid}/features", json={"code": "ROOF", "geom": "polygon",
           "coords": [[180071, 665039], [180084, 665039], [180084, 665029], [180071, 665029]]})
    c.post(f"/api/projects/{pid}/features", json={"code": "POLE", "geom": "point", "coords": [[180010, 665062]]})
    c.post(f"/api/projects/{pid}/features", json={"code": "TREE", "geom": "point", "coords": [[180015, 665080]],
                                                   "text": "אורן"})
    area = c.post(f"/api/projects/{pid}/features", json={"code": "AREA", "geom": "polygon",
                  "coords": [[180001, 665099], [180119, 665099], [180119, 665011], [180001, 665011]]}).json()
    proj = c.get(f"/api/projects/{pid}").json()
    roof = next(f for f in proj["features"] if f["code"] == "ROOF")
    bldg = next(f for f in proj["features"] if f["code"] == "BLDG")
    assert roof["coords"][0][2] - bldg["coords"][0][2] > 5   # roof sampled from DSM, footprint from DTM

    # spot heights & contours inside work area
    r = c.post(f"/api/projects/{pid}/generate", json={"spot_spacing": 10, "contour_interval": 0.5,
                                                      "boundary_id": area["id"]}).json()
    assert r["spots"] > 50 and r["contours"] > 5

    # DXF export opens and has expected layers/entities
    r = c.get(f"/api/projects/{pid}/export.dxf")
    path = tmp_path / "out.dxf"
    path.write_bytes(r.content)
    doc = ezdxf.readfile(path)
    layers = {l.dxf.name for l in doc.layers}
    for lay in ("TOPO-CURB", "TOPO-BUILDING", "TOPO-CONTOUR-MAJOR", "TOPO-SPOT", "SURV-TITLE", "SURV-GRID"):
        assert lay in layers
    msp = doc.modelspace()
    assert len(msp.query('POLYLINE[layer=="TOPO-CURB"]')) == 1
    ins = msp.query('INSERT[layer=="TOPO-POLE"]')[0]
    assert ins.get_attrib_text("ELEV")
    assert len(msp.query('LWPOLYLINE[layer=="TOPO-CONTOUR-MINOR"]')) > 0
    audit = doc.audit()
    assert not audit.has_errors

    # full ZIP package
    r = c.get(f"/api/projects/{pid}/export.zip")
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    assert any(n.endswith(".dxf") for n in names) and any(n.endswith(".jgw") for n in names)
    assert any(n.endswith("_PNEZD.csv") for n in names) and any(n.endswith("_accuracy.txt") for n in names)
