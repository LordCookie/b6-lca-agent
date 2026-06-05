"""ESRI World Imagery tile fetcher → georeferenced GeoTIFF mosaic.

Keyless satellite imagery for the AOI when no local high-res raster (DOP20)
is available. Tiles come from ArcGIS Online World Imagery (~0.3-1 m/px in
European urban areas, JPEG XYZ). Output is an RGB GeoTIFF in EPSG:3857
(Web Mercator); the existing :func:`clip_raster_to_aoi` reprojects to
EPSG:25832 on the fly.

Tiles are cached on disk so the same AOI doesn't re-download. Persistent
fetch failures raise — the agent then falls back to "no raster" (vegetation
step is skipped gracefully).
"""
from __future__ import annotations

import io
import logging
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import rasterio
import requests
from PIL import Image
from rasterio.transform import from_bounds
from shapely.geometry.base import BaseGeometry
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger("lca.engine.satellite_tiles")

ESRI_WORLD_IMAGERY_URL = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/"
    "World_Imagery/MapServer/tile/{z}/{y}/{x}"
)
_WEB_MERCATOR_EXTENT = 20037508.342789244  # half the world in metres
TILE_SIZE = 256
_MAX_TILES = 1500  # ~ 90 MB at z=18 — safety cap


def _lat_lon_to_tile(lat: float, lon: float, zoom: int) -> tuple[int, int]:
    """WGS84 lat/lon → XYZ tile indices (x, y) at the given zoom."""
    n = 2 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int(
        (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi)
        / 2.0 * n
    )
    return x, y


def _tile_to_web_mercator_bbox(
    x: int, y: int, zoom: int,
) -> tuple[float, float, float, float]:
    """(minx, miny, maxx, maxy) of tile (x, y, zoom) in EPSG:3857 metres."""
    n = 2 ** zoom
    minx = x / n * 2 * _WEB_MERCATOR_EXTENT - _WEB_MERCATOR_EXTENT
    maxx = (x + 1) / n * 2 * _WEB_MERCATOR_EXTENT - _WEB_MERCATOR_EXTENT
    maxy = _WEB_MERCATOR_EXTENT - y / n * 2 * _WEB_MERCATOR_EXTENT
    miny = _WEB_MERCATOR_EXTENT - (y + 1) / n * 2 * _WEB_MERCATOR_EXTENT
    return minx, miny, maxx, maxy


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
)
def _fetch_tile(z: int, y: int, x: int, cache_dir: Path) -> np.ndarray:
    """Fetch one ESRI tile (cached) and return an HxWx3 uint8 array."""
    cache_path = cache_dir / f"z{z}_y{y}_x{x}.jpg"
    if cache_path.is_file():
        with Image.open(cache_path) as im:
            return np.array(im.convert("RGB"))
    url = ESRI_WORLD_IMAGERY_URL.format(z=z, y=y, x=x)
    headers = {"User-Agent": "LCA-Agent/1.0 (B6-Bilanzierung, thesis tool)"}
    r = requests.get(url, headers=headers, timeout=20)
    r.raise_for_status()
    data = r.content
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(data)
    with Image.open(io.BytesIO(data)) as im:
        return np.array(im.convert("RGB"))


def fetch_aoi_imagery(
    aoi_polygon_wgs84: BaseGeometry,
    out_path: Path,
    *,
    zoom: int = 18,
    cache_dir: Path,
    max_workers: int = 8,
) -> Path:
    """Download ESRI tiles covering the AOI bbox, stitch a GeoTIFF in EPSG:3857.

    Raises :class:`RuntimeError` if the AOI is too large for the chosen zoom
    (more than 1500 tiles) or if any tile fetch keeps failing.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    min_lon, min_lat, max_lon, max_lat = aoi_polygon_wgs84.bounds

    # NB: tile y increases southward, so the southwest corner gives y_max.
    x_sw, y_sw = _lat_lon_to_tile(min_lat, min_lon, zoom)
    x_ne, y_ne = _lat_lon_to_tile(max_lat, max_lon, zoom)
    x_min, x_max = (x_sw, x_ne) if x_sw <= x_ne else (x_ne, x_sw)
    y_min, y_max = (y_ne, y_sw) if y_ne <= y_sw else (y_sw, y_ne)

    n_tiles = (x_max - x_min + 1) * (y_max - y_min + 1)
    if n_tiles > _MAX_TILES:
        raise RuntimeError(
            f"AOI requires {n_tiles} tiles at z={zoom} — over the safety cap "
            f"({_MAX_TILES}). Lower the zoom or shrink the AOI."
        )
    log.info(
        "Fetching %d ESRI World Imagery tiles at z=%d for AOI bbox %s",
        n_tiles, zoom, aoi_polygon_wgs84.bounds,
    )

    rows = y_max - y_min + 1
    cols = x_max - x_min + 1
    mosaic = np.zeros((rows * TILE_SIZE, cols * TILE_SIZE, 3), dtype=np.uint8)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_fetch_tile, zoom, ty, tx, cache_dir): (tx, ty)
            for tx in range(x_min, x_max + 1)
            for ty in range(y_min, y_max + 1)
        }
        for fut in as_completed(futures):
            tx, ty = futures[fut]
            try:
                tile = fut.result()
            except Exception as exc:
                raise RuntimeError(
                    f"ESRI tile {zoom}/{ty}/{tx} failed: {exc}"
                ) from exc
            r0 = (ty - y_min) * TILE_SIZE
            c0 = (tx - x_min) * TILE_SIZE
            mosaic[r0:r0 + TILE_SIZE, c0:c0 + TILE_SIZE, :] = tile

    # Web-Mercator bbox of the assembled mosaic (north-west to south-east).
    nw_minx, _, _, nw_maxy = _tile_to_web_mercator_bbox(x_min, y_min, zoom)
    _, se_miny, se_maxx, _ = _tile_to_web_mercator_bbox(x_max, y_max, zoom)
    transform = from_bounds(
        nw_minx, se_miny, se_maxx, nw_maxy,
        cols * TILE_SIZE, rows * TILE_SIZE,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        out_path, "w",
        driver="GTiff",
        height=mosaic.shape[0],
        width=mosaic.shape[1],
        count=3,
        dtype=mosaic.dtype,
        crs="EPSG:3857",
        transform=transform,
        compress="deflate",
        photometric="rgb",
    ) as dst:
        for band in range(3):
            dst.write(mosaic[:, :, band], band + 1)

    log.info(
        "Satellite mosaic written: %s (%d×%d px, %d tiles)",
        out_path, mosaic.shape[1], mosaic.shape[0], n_tiles,
    )
    return out_path


__all__ = ["fetch_aoi_imagery", "ESRI_WORLD_IMAGERY_URL"]
