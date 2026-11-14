"""Generate a synthetic test site (ITM, EPSG:2039): orthophoto, DSM, DTM and a control
point file, so the full workflow can be tried without real drone data.

usage: python scripts/make_sample.py OUT_DIR"""
import os
import sys

import numpy as np
import rasterio
from rasterio.transform import from_origin

E0, N0 = 180000.0, 665100.0      # top-left (Tel Aviv area)
W, H = 120.0, 90.0               # metres
ORES, DRES = 0.03, 0.10
BIAS = 0.12                      # simulated vertical bias of the photogrammetry


def terrain(e, n):
    x, y = e - E0, N0 - n
    return 32.0 + 0.04 * x - 0.02 * y + 1.2 * np.exp(-(((x - 30) ** 2 + (y - 65) ** 2) / 300.0))


def main(out):
    os.makedirs(out, exist_ok=True)
    rng = np.random.default_rng(1)
    # ---- DEMs
    w, h = int(W / DRES), int(H / DRES)
    cols, rows = np.meshgrid(np.arange(w) + 0.5, np.arange(h) + 0.5)
    e, n = E0 + cols * DRES, N0 - rows * DRES
    dtm = terrain(e, n)
    x, y = e - E0, N0 - n
    road = (y > 40) & (y < 50)
    curb = ((y > 39.6) & (y <= 40)) | ((y >= 50) & (y < 50.4))
    dtm = np.where(road, dtm - 0.15, dtm)           # road 15cm below curb top
    dsm = dtm.copy()
    bldg = (x > 70) & (x < 85) & (y > 60) & (y < 72)
    dsm[bldg] = dtm[bldg].max() + 6.0
    trees = [(15, 20), (40, 18), (95, 25), (55, 75)]
    for tx, ty in trees:
        d = np.hypot(x - tx, y - ty)
        dsm = np.maximum(dsm, np.where(d < 2.5, terrain(E0 + tx, N0 - ty) + 4.0 - d, dsm))
    noise = rng.normal(0, 0.02, dsm.shape)
    prof = dict(driver="GTiff", dtype="float32", count=1, width=w, height=h, crs="EPSG:2039",
                transform=from_origin(E0, N0, DRES, DRES), nodata=-9999.0, tiled=True, compress="deflate")
    with rasterio.open(os.path.join(out, "dsm.tif"), "w", **prof) as d:
        d.write((dsm + noise + BIAS).astype("float32"), 1)
    with rasterio.open(os.path.join(out, "dtm.tif"), "w", **prof) as d:
        d.write((dtm + BIAS).astype("float32"), 1)

    # ---- ortho
    w, h = int(W / ORES), int(H / ORES)
    cols, rows = np.meshgrid(np.arange(w) + 0.5, np.arange(h) + 0.5)
    x, y = cols * ORES, rows * ORES
    img = np.zeros((h, w, 3), np.float32)
    img[:] = (110, 135, 80)
    img += rng.normal(0, 12, img.shape)
    road = (y > 40) & (y < 50)
    img[road] = (85, 85, 88)
    img[((y > 39.6) & (y <= 40)) | ((y >= 50) & (y < 50.4))] = (215, 215, 210)
    img[((y > 36.5) & (y <= 39.6)) | ((y >= 50.4) & (y < 53.5))] = (175, 170, 160)    # sidewalks
    img[road & (np.abs(y - 45) < 0.07) & ((x % 6) < 3)] = (240, 240, 240)             # lane marking
    img[(x > 70) & (x < 85) & (y > 60) & (y < 72)] = (170, 70, 55)                      # roof
    img[(np.abs(x - 5) < 0.05) & (y > 55)] = (60, 60, 60)                               # fence
    for tx, ty in trees:
        img[np.hypot(x - tx, y - ty) < 2.5] = (40, 80, 35)
    for px, py in [(10, 38), (60, 38), (110, 38)]:                                        # poles + shadow
        img[np.hypot(x - px, y - py) < 0.15] = (30, 30, 30)
    img[np.hypot(x - 35, y - 45) < 0.3] = (50, 50, 55)                                  # manhole
    gcps = [("GCP1", 8, 8, "gcp"), ("GCP2", 110, 10, "gcp"), ("GCP3", 112, 82, "gcp"), ("GCP4", 10, 85, "gcp"),
            ("CHK1", 60, 30, "check"), ("CHK2", 45, 70, "check"), ("CHK3", 95, 55, "check")]
    for _, gx, gy, _k in gcps:                                                           # checkerboard targets
        m = (np.abs(x - gx) < 0.3) & (np.abs(y - gy) < 0.3)
        q = ((x - gx) > 0) ^ ((y - gy) > 0)
        img[m & q] = (250, 250, 250)
        img[m & ~q] = (15, 15, 15)
    img = np.clip(img, 0, 255).astype(np.uint8)
    prof = dict(driver="GTiff", dtype="uint8", count=3, width=w, height=h, crs="EPSG:2039",
                transform=from_origin(E0, N0, ORES, ORES), tiled=True, compress="jpeg", photometric="ycbcr")
    with rasterio.open(os.path.join(out, "ortho.tif"), "w", **prof) as d:
        d.write(np.transpose(img, (2, 0, 1)))

    with open(os.path.join(out, "control.csv"), "w") as f:
        f.write("name,E,N,H,type\n")
        for name, gx, gy, kind in gcps:
            ge, gn = E0 + gx, N0 - gy
            f.write(f"{name},{ge:.3f},{gn:.3f},{terrain(ge, gn) + rng.normal(0, 0.01):.3f},{kind}\n")
    print("sample written to", out)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "sample_data")
