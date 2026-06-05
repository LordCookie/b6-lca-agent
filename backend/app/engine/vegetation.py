"""Vegetation engine (spec §8) — DEEPNESS ONNX inference + allometric chain.

Pipeline:
    raster (GeoTIFF) ─┐
                      ├─► clip_to_aoi  ─► (array, transform, crs)
    aoi polygon ──────┘
                            │
                            ▼
                tiled_inference (ONNX, headless)
                            │
                            ▼
                  binary canopy mask
                            │
                            ▼
    extract_trees (connected components + stem-foot filter, spec §8:
                   "Stammfuß innerhalb der Grenze, nicht Kronenüberhang")
                            │
                            ▼
       allometric_co2_stock (CD → DBH → AGB → C → CO₂)
                            │
                            ▼
                   VegetationStats

Each step is a pure function so it can be tested without an ONNX model
or a raster file.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Sequence

import numpy as np
import onnxruntime as ort
import rasterio
import yaml
from rasterio.mask import mask as rio_mask
from rasterio.warp import Resampling, reproject
from scipy import ndimage as ndi
from shapely.geometry import Point, mapping
from shapely.geometry.base import BaseGeometry

log = logging.getLogger("lca.engine.vegetation")

# --- data types ------------------------------------------------------------


@dataclass(frozen=True)
class Tree:
    crown_area_m2: float
    crown_diameter_m: float
    centroid_xy_metric: tuple[float, float]   # stem-foot proxy (spec §8)
    flagged_oversize: bool = False            # cap applied (likely merged crowns)
    osm_only: bool = False                    # added via OSM (ONNX missed it)


@dataclass(frozen=True)
class AllometryParams:
    source: str
    cd_to_dbh_a: float
    cd_to_dbh_b: float
    cd_to_dbh_sigma_rel: float
    dbh_to_agb_a: float
    dbh_to_agb_b: float
    dbh_to_agb_sigma_rel: float
    root_shoot_ratio: float
    carbon_fraction: float
    co2_per_carbon: float
    min_crown_area_m2: float
    max_crown_area_m2: float


@dataclass(frozen=True)
class VegetationStats:
    n_trees: int
    n_flagged: int
    total_canopy_area_m2: float
    mean_crown_area_m2: float
    co2_stock_kg: float
    co2_stock_sigma_kg: float
    raster_pixel_area_m2: float


# --- allometry config loader ----------------------------------------------


@lru_cache(maxsize=2)
def load_allometry(path: Path) -> AllometryParams:
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    cd = raw["crown_diameter_to_dbh"]
    db = raw["dbh_to_aboveground_biomass"]
    return AllometryParams(
        source=str(raw["source"]),
        cd_to_dbh_a=float(cd["a"]),
        cd_to_dbh_b=float(cd["b"]),
        cd_to_dbh_sigma_rel=float(cd.get("uncertainty_rel", 0.30)),
        dbh_to_agb_a=float(db["a"]),
        dbh_to_agb_b=float(db["b"]),
        dbh_to_agb_sigma_rel=float(db.get("uncertainty_rel", 0.40)),
        root_shoot_ratio=float(raw["belowground_root_shoot_ratio"]),
        carbon_fraction=float(raw["carbon_fraction"]),
        co2_per_carbon=float(raw["co2_per_carbon"]),
        min_crown_area_m2=float(raw.get("min_crown_area_m2", 4.0)),
        max_crown_area_m2=float(raw.get("max_crown_area_m2", 250.0)),
    )


# Known species-group keys (must match allometry.yaml `species_groups`).
SPECIES_GROUPS = ("laubbaum", "nadelbaum", "mischbestand")


def augment_with_osm_trees(
    trees: list[Tree],
    canopy_mask: np.ndarray,
    transform: "rasterio.Affine",
    aoi_metric: BaseGeometry,
    osm_trees: "Sequence",
    *,
    default_crown_diameter_m: float = 5.0,
) -> tuple[list[Tree], int]:
    """Add OSM ``natural=tree`` nodes the ONNX segmentation missed.

    A node is added when (a) it lies inside the AOI polygon, (b) its raster
    pixel is OUTSIDE the canopy mask. The added :class:`Tree` gets a documented
    default crown diameter and ``osm_only=True`` so the report can distinguish
    it from pixel-detected trees.

    Returns ``(augmented_trees, n_added)``.
    """
    if not osm_trees or canopy_mask.size == 0:
        return list(trees), 0
    h, w = canopy_mask.shape[:2]
    crown_area = math.pi * (default_crown_diameter_m / 2.0) ** 2
    inv = ~transform  # map → pixel
    out = list(trees)
    added = 0
    for pt in osm_trees:
        try:
            if not aoi_metric.contains(pt):
                continue
            col_f, row_f = inv * (pt.x, pt.y)
        except Exception:
            continue
        col, row = int(col_f), int(row_f)
        if not (0 <= row < h and 0 <= col < w):
            continue
        if bool(canopy_mask[row, col]):
            continue  # ONNX already detected this tree
        out.append(Tree(
            crown_area_m2=crown_area,
            crown_diameter_m=default_crown_diameter_m,
            centroid_xy_metric=(float(pt.x), float(pt.y)),
            flagged_oversize=False,
            osm_only=True,
        ))
        added += 1
    return out, added


def apply_species_group(
    base: AllometryParams,
    path: Path,
    group_key: str,
    *,
    source_note: str | None = None,
) -> AllometryParams:
    """Return `base` with the literature coefficients of one species group.

    The coefficients come from allometry.yaml `species_groups` (NOT from the
    LLM — the research only *classifies* the dominant group, the numbers stay
    literature-based). Unknown group → `base` returned unchanged.
    """
    if group_key not in SPECIES_GROUPS:
        return base
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    g = (raw.get("species_groups") or {}).get(group_key)
    if not g:
        return base
    cd = g.get("crown_diameter_to_dbh", {})
    db = g.get("dbh_to_aboveground_biomass", {})
    return replace(
        base,
        cd_to_dbh_a=float(cd.get("a", base.cd_to_dbh_a)),
        cd_to_dbh_b=float(cd.get("b", base.cd_to_dbh_b)),
        cd_to_dbh_sigma_rel=float(cd.get("uncertainty_rel", base.cd_to_dbh_sigma_rel)),
        dbh_to_agb_a=float(db.get("a", base.dbh_to_agb_a)),
        dbh_to_agb_b=float(db.get("b", base.dbh_to_agb_b)),
        dbh_to_agb_sigma_rel=float(db.get("uncertainty_rel", base.dbh_to_agb_sigma_rel)),
        source=source_note or str(g.get("note", base.source)),
    )


# --- raster clipping -------------------------------------------------------


def clip_raster_to_aoi(
    raster_path: Path,
    aoi_polygon_metric: BaseGeometry,
    aoi_crs: str = "EPSG:25832",
) -> tuple[np.ndarray, "rasterio.Affine", str]:
    """Load and clip the raster to the AOI polygon.

    Reprojects the raster on-the-fly to `aoi_crs` if it does not already match.
    Returns (array shaped (C, H, W) uint8/float, transform, crs).
    """
    with rasterio.open(raster_path) as src:
        if src.crs is None:
            raise ValueError(f"raster {raster_path.name} has no CRS — cannot clip safely")

        if str(src.crs).upper() == aoi_crs.upper():
            geoms = [mapping(aoi_polygon_metric)]
            out_image, out_transform = rio_mask(src, geoms, crop=True, filled=True)
            crs_out = aoi_crs
        else:
            # Reproject the source into aoi_crs, then mask.
            dst_arr, dst_transform = _reproject_to_crs(src, aoi_crs)
            # Wrap reprojected array in an in-memory dataset for masking.
            from rasterio.io import MemoryFile

            with MemoryFile() as mf:
                with mf.open(
                    driver="GTiff",
                    height=dst_arr.shape[1],
                    width=dst_arr.shape[2],
                    count=dst_arr.shape[0],
                    dtype=dst_arr.dtype,
                    crs=aoi_crs,
                    transform=dst_transform,
                ) as tmp:
                    tmp.write(dst_arr)
                    geoms = [mapping(aoi_polygon_metric)]
                    out_image, out_transform = rio_mask(tmp, geoms, crop=True, filled=True)
            crs_out = aoi_crs

    return out_image, out_transform, crs_out


def render_raster_preview(
    array_chw: np.ndarray,
    out_path: Path,
    max_dim_px: int = 1024,
) -> Path:
    """Render a clipped raster (C, H, W) to an 8-bit RGB PNG preview.

    Spec §3 step 3: the AOI-clipped raster must be displayed in the UI.
    This produces the artefact that the UI fetches; the original GeoTIFF
    stays in /app/data/imagery and is not exposed to the browser.
    """
    from PIL import Image  # imported lazily so the engine can be tested without PIL

    out_path.parent.mkdir(parents=True, exist_ok=True)
    arr = array_chw

    # Take the first three channels (RGB). 4-band RGBN orthophotos drop NIR.
    if arr.shape[0] >= 3:
        rgb = arr[:3]
    else:
        # 1-band: repeat to grayscale RGB
        rgb = np.repeat(arr[:1], 3, axis=0)

    # Normalise to 8-bit. Most GeoTIFFs are already uint8; if not, stretch
    # via percentile to avoid washed-out previews.
    if rgb.dtype != np.uint8:
        lo = np.percentile(rgb, 2.0)
        hi = np.percentile(rgb, 98.0)
        if hi <= lo:
            hi = lo + 1.0
        rgb = np.clip((rgb - lo) * (255.0 / (hi - lo)), 0, 255).astype(np.uint8)

    # (C, H, W) → (H, W, C) for PIL.
    img = Image.fromarray(np.transpose(rgb, (1, 2, 0)))

    # Cap the longer edge so the browser preview stays small.
    if max(img.size) > max_dim_px:
        img.thumbnail((max_dim_px, max_dim_px))

    img.save(out_path, format="PNG", optimize=True)
    return out_path


def _reproject_to_crs(src, dst_crs: str):
    """Reproject a rasterio source dataset into dst_crs and return (array, transform)."""
    from rasterio.warp import calculate_default_transform

    transform, width, height = calculate_default_transform(
        src.crs, dst_crs, src.width, src.height, *src.bounds
    )
    dst = np.zeros((src.count, height, width), dtype=src.read(1).dtype)
    for i in range(1, src.count + 1):
        reproject(
            source=rasterio.band(src, i),
            destination=dst[i - 1],
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=transform,
            dst_crs=dst_crs,
            resampling=Resampling.bilinear,
        )
    return dst, transform


# --- ONNX inference --------------------------------------------------------


def tiled_inference(
    session: ort.InferenceSession,
    array_chw: np.ndarray,
    tile_size: int = 512,
    overlap: int = 64,
    threshold: float = 0.5,
    target_class: int | None = None,
) -> np.ndarray:
    """Sliding-window ONNX inference, stitched by maximum.

    Defensive about input shape:
      - Auto-detects whether the model wants 3 or 4 channels.
      - Pads with zeros at the right/bottom edges so the last tile is full-size.
      - Normalises pixel values to [0, 1] (DEEPNESS convention).

    Output handling:
      - target_class=None  (binary segmentation, single-class output):
          take channel 0 as probability, stitch by max, threshold.
      - target_class=<int> (multi-class segmentation, e.g. LandCover.ai):
          argmax over the class axis per tile, then mask = (argmax == target_class).
          This is what the DEEPNESS Land-Cover-Segmentation model needs
          (Woodland = class 2).
    """
    in_meta = session.get_inputs()[0]
    want_channels = in_meta.shape[1] if isinstance(in_meta.shape[1], int) else array_chw.shape[0]
    in_name = in_meta.name

    arr = array_chw
    if arr.shape[0] > want_channels:
        arr = arr[:want_channels]
    elif arr.shape[0] < want_channels:
        # Repeat the first band to fill — better than failing outright on a 1-band DOP.
        pad = np.repeat(arr[:1], want_channels - arr.shape[0], axis=0)
        arr = np.vstack([arr, pad])

    if arr.dtype != np.float32:
        arr = arr.astype(np.float32) / 255.0

    _, h, w = arr.shape
    stride = tile_size - overlap
    n_tiles_y = math.ceil((h - overlap) / stride)
    n_tiles_x = math.ceil((w - overlap) / stride)

    out_mask = np.zeros((h, w), dtype=np.float32)

    for ty in range(n_tiles_y):
        for tx in range(n_tiles_x):
            y0 = ty * stride
            x0 = tx * stride
            y1 = min(y0 + tile_size, h)
            x1 = min(x0 + tile_size, w)
            tile = arr[:, y0:y1, x0:x1]

            # Right/bottom edge: pad to full tile size for the model.
            if tile.shape[1] != tile_size or tile.shape[2] != tile_size:
                padded = np.zeros((tile.shape[0], tile_size, tile_size), dtype=np.float32)
                padded[:, : tile.shape[1], : tile.shape[2]] = tile
                tile = padded
                pad_y, pad_x = y1 - y0, x1 - x0
            else:
                pad_y, pad_x = tile_size, tile_size

            inp = tile[np.newaxis, :, :, :]  # (1, C, H, W)
            out = session.run(None, {in_name: inp})[0]
            # Output may be (1, K, H, W), (1, 1, H, W), (1, H, W) or (H, W).
            prob = np.squeeze(out)
            if prob.ndim == 3:
                if target_class is None:
                    # binary: take channel 0 as the foreground probability
                    prob = prob[0].astype(np.float32)
                else:
                    # multi-class: hard-mask via argmax (1.0 for the target class)
                    argmax = prob.argmax(axis=0)
                    prob = (argmax == target_class).astype(np.float32)
            elif prob.ndim == 2:
                prob = prob.astype(np.float32)
            else:
                raise ValueError(f"unexpected ONNX output shape: {out.shape}")
            prob_tile = prob[:pad_y, :pad_x]
            # Stitch by maximum so overlapping inferences reinforce each other.
            np.maximum(out_mask[y0:y1, x0:x1], prob_tile, out=out_mask[y0:y1, x0:x1])

    return (out_mask >= threshold).astype(np.uint8)


# --- tree extraction (spec §8 stem-foot rule) ------------------------------


def extract_trees(
    mask: np.ndarray,
    transform: "rasterio.Affine",
    aoi_polygon_metric: BaseGeometry,
    allometry: AllometryParams,
) -> list[Tree]:
    """Connected-component analysis on the mask, filtered by stem-foot rule.

    A crown's centroid is used as a stem-foot proxy (the segmentation model
    sees crowns, not stems). A tree is *kept* iff its centroid lies inside
    the AOI polygon — that's the strict interpretation of spec §8.
    """
    labelled, n_labels = ndi.label(mask)
    if n_labels == 0:
        return []

    px_w = abs(transform.a)
    px_h = abs(transform.e)
    px_area = px_w * px_h

    # centroids in pixel space (row, col)
    centroids_rc = ndi.center_of_mass(mask, labelled, range(1, n_labels + 1))
    areas_px = ndi.sum(mask, labelled, range(1, n_labels + 1))

    trees: list[Tree] = []
    for (rc, area_px) in zip(centroids_rc, areas_px):
        area_m2 = float(area_px) * px_area
        if area_m2 < allometry.min_crown_area_m2:
            continue

        # rasterio Affine: world(x, y) = transform * (col + 0.5, row + 0.5)
        col, row = float(rc[1]) + 0.5, float(rc[0]) + 0.5
        x_world, y_world = transform * (col, row)

        if not aoi_polygon_metric.contains(Point(x_world, y_world)):
            # spec §8: Kronenüberhang außerhalb der Grenze zählt nicht.
            continue

        flagged = False
        if area_m2 > allometry.max_crown_area_m2:
            area_m2 = allometry.max_crown_area_m2
            flagged = True

        crown_diameter_m = math.sqrt(4.0 * area_m2 / math.pi)
        trees.append(
            Tree(
                crown_area_m2=area_m2,
                crown_diameter_m=crown_diameter_m,
                centroid_xy_metric=(x_world, y_world),
                flagged_oversize=flagged,
            )
        )
    return trees


# --- allometric chain ------------------------------------------------------


def allometric_co2_stock(
    trees: Sequence[Tree],
    allometry: AllometryParams,
    raster_pixel_area_m2: float = 0.0,
) -> VegetationStats:
    """CD → DBH → AGB → BGB → C → CO₂, with Gauss-propagated relative σ."""
    if not trees:
        return VegetationStats(
            n_trees=0,
            n_flagged=0,
            total_canopy_area_m2=0.0,
            mean_crown_area_m2=0.0,
            co2_stock_kg=0.0,
            co2_stock_sigma_kg=0.0,
            raster_pixel_area_m2=raster_pixel_area_m2,
        )

    total_co2 = 0.0
    total_area = 0.0
    for t in trees:
        # DBH [cm] = a · CD[m]^b
        dbh_cm = allometry.cd_to_dbh_a * (t.crown_diameter_m ** allometry.cd_to_dbh_b)
        # AGB [kg dry] = a · DBH[cm]^b
        agb_kg = allometry.dbh_to_agb_a * (dbh_cm ** allometry.dbh_to_agb_b)
        # Total biomass incl. roots
        bgb_kg = agb_kg * allometry.root_shoot_ratio
        total_biomass_kg = agb_kg + bgb_kg
        carbon_kg = total_biomass_kg * allometry.carbon_fraction
        co2_kg = carbon_kg * allometry.co2_per_carbon
        total_co2 += co2_kg
        total_area += t.crown_area_m2

    # Combine the two dominant relative σ in quadrature on the multiplicative chain.
    rel_sigma = math.sqrt(
        allometry.cd_to_dbh_sigma_rel ** 2 + allometry.dbh_to_agb_sigma_rel ** 2
    )
    return VegetationStats(
        n_trees=len(trees),
        n_flagged=sum(1 for t in trees if t.flagged_oversize),
        total_canopy_area_m2=total_area,
        mean_crown_area_m2=total_area / len(trees),
        co2_stock_kg=total_co2,
        co2_stock_sigma_kg=total_co2 * rel_sigma,
        raster_pixel_area_m2=raster_pixel_area_m2,
    )
