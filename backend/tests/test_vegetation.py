"""Vegetation engine tests.

Pure-function coverage — no ONNX model, no real GeoTIFF needed.
We synthesise binary masks + a known Affine transform and verify:
  - tree extraction respects min/max crown area
  - stem-foot rule (centroid-in-AOI) is enforced (spec §8)
  - allometric chain produces expected magnitudes
  - σ propagation combines crown→DBH and DBH→AGB in quadrature
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest
from rasterio.transform import from_origin
from shapely.geometry import Polygon

from app.engine.vegetation import (
    AllometryParams,
    Tree,
    allometric_co2_stock,
    extract_trees,
    load_allometry,
    tiled_inference,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
ALLOMETRY_PATH = REPO_ROOT / "data" / "profiles" / "allometry.yaml"


# --- fixtures -------------------------------------------------------------


@pytest.fixture
def allometry() -> AllometryParams:
    return load_allometry(ALLOMETRY_PATH)


def _square_mask_with_trees(
    shape_px=(200, 200),
    pixel_size_m=0.5,
    tree_centers_px=((50, 50), (100, 100), (180, 180)),
    tree_radius_px=8,
):
    """Build a binary mask with circular tree crowns + a metric-projected Affine."""
    h, w = shape_px
    mask = np.zeros((h, w), dtype=np.uint8)
    yy, xx = np.ogrid[:h, :w]
    for (cy, cx) in tree_centers_px:
        circle = (yy - cy) ** 2 + (xx - cx) ** 2 <= tree_radius_px ** 2
        mask[circle] = 1
    # Affine: world origin (0, 100), pixel size pixel_size_m, y axis flipped.
    transform = from_origin(0.0, h * pixel_size_m, pixel_size_m, pixel_size_m)
    return mask, transform


# --- tree extraction ------------------------------------------------------


class TestExtractTrees:
    def test_keeps_trees_inside_aoi(self, allometry):
        mask, transform = _square_mask_with_trees(pixel_size_m=0.5)
        # AOI covers the full 100×100 m image.
        aoi = Polygon([(0, 0), (100, 0), (100, 100), (0, 100)])
        trees = extract_trees(mask, transform, aoi, allometry)
        assert len(trees) == 3
        # Crown area ≈ π·r² · px²  = π · 4² m² ≈ 50 m² each
        for t in trees:
            assert 40.0 < t.crown_area_m2 < 60.0
            assert 6.5 < t.crown_diameter_m < 8.5

    def test_drops_trees_outside_aoi(self, allometry):
        # rasterio's Affine has y pointing DOWN: pixel (row, col) → world
        # (col·px, (h-row)·px). With shape 200×200 @ 0.5 m/px (image 100×100 m):
        #   pixel (160, 40) → world ~(20, 20)  — inside the lower-left AOI quadrant
        #   pixel (40, 160) → world ~(80, 80)  — outside the lower-left AOI quadrant
        from shapely.geometry import Point
        mask, transform = _square_mask_with_trees(
            tree_centers_px=((160, 40), (40, 160)), pixel_size_m=0.5
        )
        aoi = Polygon([(0, 0), (50, 0), (50, 50), (0, 50)])
        trees = extract_trees(mask, transform, aoi, allometry)
        assert len(trees) == 1
        x, y = trees[0].centroid_xy_metric
        assert aoi.contains(Point(x, y))

    def test_drops_below_min_area(self, allometry):
        # A 2×2-pixel blob → 1 m² < min 4 m² → discarded.
        mask = np.zeros((20, 20), dtype=np.uint8)
        mask[5:7, 5:7] = 1
        transform = from_origin(0.0, 20 * 0.5, 0.5, 0.5)
        aoi = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
        assert extract_trees(mask, transform, aoi, allometry) == []

    def test_flags_oversize_crowns(self, allometry):
        # One huge blob > max_crown_area_m2 (=250) → flagged and capped.
        h, w = 100, 100
        mask = np.zeros((h, w), dtype=np.uint8)
        mask[10:90, 10:90] = 1  # 80×80 px @ 1 m/px = 6400 m²
        transform = from_origin(0.0, h * 1.0, 1.0, 1.0)
        aoi = Polygon([(0, 0), (100, 0), (100, 100), (0, 100)])
        trees = extract_trees(mask, transform, aoi, allometry)
        assert len(trees) == 1
        assert trees[0].flagged_oversize is True
        assert trees[0].crown_area_m2 == pytest.approx(allometry.max_crown_area_m2)


# --- allometric chain -----------------------------------------------------


class TestAllometricChain:
    def test_empty_input(self, allometry):
        stats = allometric_co2_stock([], allometry)
        assert stats.n_trees == 0
        assert stats.co2_stock_kg == 0.0
        assert stats.co2_stock_sigma_kg == 0.0

    def test_single_tree_known_chain(self, allometry):
        """Manual cross-check for a 6 m crown diameter (≈28 m² crown).

        DBH  = 6.5 · 6 = 39 cm
        AGB  = 0.0509 · 39^2.5 ≈ 484 kg
        Total = AGB · (1 + 0.27) = 615 kg
        C     = 615 · 0.5 = 307 kg
        CO₂   = 307 · 3.667 ≈ 1127 kg
        """
        tree = Tree(crown_area_m2=28.27, crown_diameter_m=6.0, centroid_xy_metric=(0, 0))
        stats = allometric_co2_stock([tree], allometry)
        assert stats.n_trees == 1
        assert stats.co2_stock_kg == pytest.approx(1127.0, rel=0.05)

    def test_uncertainty_combines_in_quadrature(self, allometry):
        tree = Tree(crown_area_m2=28.27, crown_diameter_m=6.0, centroid_xy_metric=(0, 0))
        stats = allometric_co2_stock([tree], allometry)
        expected_rel = math.sqrt(
            allometry.cd_to_dbh_sigma_rel ** 2 + allometry.dbh_to_agb_sigma_rel ** 2
        )
        assert stats.co2_stock_sigma_kg / stats.co2_stock_kg == pytest.approx(expected_rel, abs=1e-6)

    def test_scaling_with_tree_count(self, allometry):
        tree = Tree(crown_area_m2=28.27, crown_diameter_m=6.0, centroid_xy_metric=(0, 0))
        single = allometric_co2_stock([tree], allometry)
        ten = allometric_co2_stock([tree] * 10, allometry)
        # CO₂ stock is linear in tree count; σ is too (perfectly correlated allometry).
        assert ten.co2_stock_kg == pytest.approx(10 * single.co2_stock_kg)


# --- tiled inference (no ONNX needed — use a dummy session) --------------


class _IdentitySession:
    """Stand-in for an ort.InferenceSession that returns the mean of the first
    channel >= 0.5 as the canopy probability. Lets us unit-test the stitching."""

    class _Input:
        name = "input"
        shape = (1, 3, 512, 512)

    def get_inputs(self):
        return [self._Input()]

    def run(self, _, feeds):
        arr = feeds["input"]  # (1, 3, 512, 512), float32 in [0, 1]
        # Probability = first channel value (already in [0,1]).
        return [arr[:, :1, :, :]]


class TestTiledStitching:
    def test_tiles_cover_full_image(self):
        h, w = 600, 800
        # Channel 0 = constant 1.0 inside a rectangle, 0 elsewhere.
        arr = np.zeros((3, h, w), dtype=np.float32)
        arr[0, 100:500, 200:600] = 1.0
        mask = tiled_inference(_IdentitySession(), arr, tile_size=512, overlap=64, threshold=0.5)
        assert mask.shape == (h, w)
        # All pixels of the input rectangle should be detected.
        assert mask[100:500, 200:600].all()
        # Pixels outside should remain 0.
        assert not mask[:50, :50].any()
        assert not mask[550:, 700:].any()


class _LandCoverSession:
    """Stand-in for the DEEPNESS LandCover.ai model output (6 classes).

    Inside the input's channel-0 > 0.5 region: class 2 (Woodland) wins.
    Elsewhere: class 0 (background) wins.
    """

    class _Input:
        name = "input"
        shape = (1, 3, 512, 512)

    def get_inputs(self):
        return [self._Input()]

    def run(self, _, feeds):
        arr = feeds["input"]
        b, _, h, w = arr.shape
        out = np.zeros((b, 6, h, w), dtype=np.float32)
        woodland_mask = arr[:, 0] > 0.5
        out[:, 0] = np.where(woodland_mask, 0.05, 0.6)
        out[:, 2] = np.where(woodland_mask, 0.85, 0.05)
        return [out]


class TestMultiClassMode:
    def test_target_class_picks_woodland(self):
        h, w = 600, 800
        arr = np.zeros((3, h, w), dtype=np.float32)
        arr[0, 100:500, 200:600] = 1.0
        mask = tiled_inference(_LandCoverSession(), arr,
                               tile_size=512, overlap=64, target_class=2)
        # Exactly the woodland rectangle should be 1.
        assert mask[100:500, 200:600].all()
        assert not mask[:50, :50].any()
        assert mask.sum() == 400 * 400

    def test_binary_mode_takes_channel_zero(self):
        h, w = 600, 800
        arr = np.zeros((3, h, w), dtype=np.float32)
        arr[0, 100:500, 200:600] = 1.0
        mask = tiled_inference(_LandCoverSession(), arr,
                               tile_size=512, overlap=64, target_class=None)
        # Channel 0 in this stub is the *background* class — it dominates
        # OUTSIDE the rectangle. Confirms the two modes pick different signals.
        assert mask[:50, :50].all()
        assert not mask[200:400, 300:500].any()
