"""Raster helpers: import/reproject GeoTIFFs, serve map tiles, sample elevations,
generate spot heights and contours."""
import io
import math
import shutil

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT
from rasterio.windows import from_bounds
from PIL import Image

TILE = 256


def import_raster(src_path, dst_path, epsg, is_dem=False):
    """Copy a GeoTIFF into the project, reprojecting to the project CRS if needed,
    and build overviews so tiles are fast at every zoom."""
    with rasterio.open(src_path) as src:
        if src.crs is None:
            raise ValueError("לקובץ אין מערכת קואורדינטות (CRS). יש לייצא GeoTIFF עם גיאורפרנס.")
        if src.crs.to_epsg() == epsg:
            shutil.copyfile(src_path, dst_path)
        else:
            resampling = Resampling.bilinear if is_dem else Resampling.cubic
            with WarpedVRT(src, crs=f"EPSG:{epsg}", resampling=resampling) as vrt:
                profile = vrt.profile.copy()
                profile.update(driver="GTiff", tiled=True, blockxsize=512, blockysize=512,
                               compress="deflate", BIGTIFF="IF_SAFER")
                with rasterio.open(dst_path, "w", **profile) as dst:
                    for _, window in vrt.block_windows(1):
                        dst.write(vrt.read(window=window), window=window)
    with rasterio.open(dst_path, "r+") as ds:
        factors = [f for f in (2, 4, 8, 16, 32, 64, 128) if min(ds.width, ds.height) / f >= 64]
        if factors:
            ds.build_overviews(factors, Resampling.average)
            ds.update_tags(ns="rio_overview", resampling="average")
        b = ds.bounds
        info = {"bounds": [b.left, b.bottom, b.right, b.top],
                "res": [abs(ds.res[0]), abs(ds.res[1])],
                "width": ds.width, "height": ds.height, "count": ds.count}
        if is_dem:
            arr = ds.read(1, masked=True, out_shape=(min(ds.height, 1024), min(ds.width, 1024)))
            info["zmin"] = float(arr.min())
            info["zmax"] = float(arr.max())
    return info


def render_tile(path, origin_e, origin_n, z, x, y, dem_range=None):
    """Render a 256px tile for a local CRS where zoom 0 = 1 px per metre and
    tile (0,0) starts at the project origin (top-left)."""
    size = TILE / (2.0 ** z)
    left = origin_e + x * size
    top = origin_n - y * size
    right, bottom = left + size, top - size
    with rasterio.open(path) as ds:
        b = ds.bounds
        if right < b.left or left > b.right or top < b.bottom or bottom > b.top:
            return None
        win = from_bounds(left, bottom, right, top, ds.transform)
        if dem_range is not None:
            arr = ds.read(1, window=win, out_shape=(TILE, TILE), boundless=True,
                          masked=True, resampling=Resampling.bilinear)
            img = _colorize_dem(arr, *dem_range)
        else:
            bands = [1, 2, 3] if ds.count >= 3 else [1, 1, 1]
            rgb = ds.read(bands, window=win, out_shape=(3, TILE, TILE), boundless=True,
                          fill_value=0, resampling=Resampling.bilinear)
            if rgb.dtype != np.uint8:
                rgb = _stretch(rgb)
            if ds.count >= 4:
                alpha = ds.read(4, window=win, out_shape=(TILE, TILE), boundless=True,
                                fill_value=0, resampling=Resampling.nearest)
            else:
                mask = ds.read_masks(1, window=win, out_shape=(TILE, TILE), boundless=True)
                alpha = mask
            img = np.dstack([rgb[0], rgb[1], rgb[2], np.where(alpha > 0, 255, 0).astype(np.uint8)])
    buf = io.BytesIO()
    Image.fromarray(img, "RGBA").save(buf, "PNG", optimize=False)
    return buf.getvalue()


def _stretch(rgb):
    out = np.zeros(rgb.shape, dtype=np.uint8)
    for i in range(rgb.shape[0]):
        band = rgb[i].astype("float64")
        lo, hi = np.percentile(band, (2, 98)) if band.size else (0, 1)
        out[i] = np.clip((band - lo) / max(hi - lo, 1e-9) * 255, 0, 255).astype(np.uint8)
    return out


def _colorize_dem(arr, zmin, zmax):
    import matplotlib
    cmap = matplotlib.colormaps["terrain"]
    norm = np.clip((arr.filled(zmin) - zmin) / max(zmax - zmin, 1e-6), 0, 1)
    rgba = (cmap(norm) * 255).astype(np.uint8)
    rgba[..., 3] = np.where(np.ma.getmaskarray(arr), 0, 200)
    return rgba


def sample_bilinear(path, pts):
    """Bilinear elevation at each (E, N); None where outside/nodata."""
    out = []
    with rasterio.open(path) as ds:
        inv = ~ds.transform
        nodata = ds.nodata
        for e, n in pts:
            col, row = inv @ (e, n)
            c0, r0 = int(math.floor(col - 0.5)), int(math.floor(row - 0.5))
            fx, fy = col - 0.5 - c0, row - 0.5 - r0
            if c0 < 0 or r0 < 0 or c0 + 1 >= ds.width or r0 + 1 >= ds.height:
                out.append(None)
                continue
            w = rasterio.windows.Window(c0, r0, 2, 2)
            a = ds.read(1, window=w).astype("float64")
            bad = ~np.isfinite(a)
            if nodata is not None:
                bad |= a == nodata
            if bad.all():
                out.append(None)
                continue
            if bad.any():  # fall back to mean of valid neighbours
                out.append(float(a[~bad].mean()))
                continue
            v = (a[0, 0] * (1 - fx) * (1 - fy) + a[0, 1] * fx * (1 - fy)
                 + a[1, 0] * (1 - fx) * fy + a[1, 1] * fx * fy)
            out.append(float(v))
    return out


def _read_grid(path, step):
    """Read DEM resampled to ~step metres; returns array, transform."""
    with rasterio.open(path) as ds:
        res = abs(ds.res[0])
        factor = max(1.0, step / res)
        h, w = max(2, int(ds.height / factor)), max(2, int(ds.width / factor))
        arr = ds.read(1, out_shape=(h, w), masked=True, resampling=Resampling.average)
        if ds.nodata is None:
            arr = np.ma.masked_invalid(arr)
        transform = ds.transform @ ds.transform.scale(ds.width / w, ds.height / h)
    return arr, transform


def spot_grid(path, spacing, polygon=None, holes=()):
    """Regular spot heights every `spacing` metres (optionally inside a polygon)."""
    with rasterio.open(path) as ds:
        b = ds.bounds
    e0 = math.ceil(b.left / spacing) * spacing
    n0 = math.ceil(b.bottom / spacing) * spacing
    es = np.arange(e0, b.right, spacing)
    ns = np.arange(n0, b.top, spacing)
    pts = [(float(e), float(n)) for n in ns for e in es]
    if polygon:
        from matplotlib.path import Path
        p = Path(polygon)
        pts = [q for q in pts if p.contains_point(q)]
    if holes:
        from matplotlib.path import Path
        hp = [Path(h) for h in holes]
        pts = [q for q in pts if not any(h.contains_point(q) for h in hp)]
    zs = sample_bilinear(path, pts)
    return [(e, n, z) for (e, n), z in zip(pts, zs) if z is not None]


def contours(path, interval, major_every=5, smooth_m=1.0, polygon=None, holes=()):
    """Contour lines from a DEM. Returns list of dicts {z, major, coords:[(e,n),..]}."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from scipy.ndimage import gaussian_filter

    step = max(smooth_m, 0.25)
    arr, tr = _read_grid(path, step)
    mask = np.ma.getmaskarray(arr)
    data = arr.filled(np.nan).astype("float64")
    if smooth_m > 0 and np.isfinite(data).any():
        fill = np.where(np.isfinite(data), data, np.nanmean(data))
        sm = gaussian_filter(fill, sigma=1.0)
        data = np.where(mask, np.nan, sm)
    if polygon or holes:
        from matplotlib.path import Path
        rows, cols = np.mgrid[0:data.shape[0], 0:data.shape[1]]
        xs, ys = tr.c + tr.a * (cols + 0.5), tr.f + tr.e * (rows + 0.5)
        xy = np.column_stack([xs.ravel(), ys.ravel()])
        if polygon:
            data[~Path(polygon).contains_points(xy).reshape(data.shape)] = np.nan
        for h in holes:  # no contours through buildings
            data[Path(h).contains_points(xy).reshape(data.shape)] = np.nan
    if not np.isfinite(data).any():
        return []
    zmin, zmax = np.nanmin(data), np.nanmax(data)
    levels = np.arange(math.ceil(zmin / interval) * interval, zmax, interval)
    if len(levels) == 0:
        return []
    fig = plt.figure()
    cs = plt.contour(data, levels=levels)
    result = []
    for lvl, segs in zip(cs.levels, cs.allsegs):
        k = round(lvl / interval)
        for seg in segs:
            if len(seg) < 3:
                continue
            coords = [tuple(tr @ (c + 0.5, r + 0.5)) for c, r in seg]
            result.append({"z": float(lvl), "major": k % major_every == 0, "coords": coords})
    plt.close(fig)
    return result


def export_ortho_jpeg(path, out_jpg, max_px=8000):
    """Write a JPEG + world file (.jgw) of the orthophoto for referencing in CAD."""
    with rasterio.open(path) as ds:
        factor = max(1.0, max(ds.width, ds.height) / max_px)
        h, w = int(ds.height / factor), int(ds.width / factor)
        bands = [1, 2, 3] if ds.count >= 3 else [1, 1, 1]
        rgb = ds.read(bands, out_shape=(3, h, w), resampling=Resampling.average)
        if rgb.dtype != np.uint8:
            rgb = _stretch(rgb)
        tr = ds.transform @ ds.transform.scale(ds.width / w, ds.height / h)
    Image.fromarray(np.dstack(rgb), "RGB").save(out_jpg, "JPEG", quality=88)
    with open(out_jpg[:-4] + ".jgw", "w") as f:
        f.write(f"{tr.a}\n{tr.d}\n{tr.b}\n{tr.e}\n{tr.c + tr.a / 2}\n{tr.f + tr.e / 2}\n")
    return w, h, (tr.c, tr.f + tr.e * h), (tr.a * w, -tr.e * h)
