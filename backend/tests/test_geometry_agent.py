"""Unit tests for the pure helpers in the geometry agent.

The OSM fetch itself is not tested here (needs Overpass network access).
What we test is the deterministic parsing + containment logic that
encodes spec §3.2 (exclude clipped buildings) and the tag tolerance.
"""
from __future__ import annotations

import pytest
from shapely.geometry import Polygon

from app.agents.geometry import (
    _METRIC_CRS,
    _WGS84,
    _is_fully_within_aoi,
    _osm_id_from_index,
    _parse_number,
    _to_metric,
)


class TestParseNumber:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("3", 3.0),
            ("12.5", 12.5),
            ("12,5", 12.5),  # German decimal
            ("12 m", 12.0),
            ("12.0 m", 12.0),
            ("ca. 5", 5.0),
            ("3-4", 3.0),  # take the first number
            ("approximately 7.5", 7.5),
            (None, None),
            ("", None),
            ("yes", None),
            ("no", None),
            ("not-a-number", None),
            (3, 3.0),
            (3.5, 3.5),
        ],
    )
    def test_robust_parse(self, raw, expected):
        assert _parse_number(raw) == expected


class TestOsmIdFromIndex:
    def test_tuple_index_flattens(self):
        assert _osm_id_from_index(("way", 12345)) == "way/12345"

    def test_scalar_fallback(self):
        assert _osm_id_from_index(12345) == "12345"


class TestMetricReprojection:
    def test_oldenburg_polygon_has_metric_area(self):
        # ~50m x 50m square near Jade HS Oldenburg (53.1435 N, 8.2147 E).
        # 1 deg lat ~ 111_320 m; 50 m ~ 0.000449 deg lat.
        # 1 deg lon ~ 111_320 * cos(53.14) ~ 66_700 m; 50 m ~ 0.000749 deg lon.
        d_lat = 50.0 / 111_320
        d_lon = 50.0 / (111_320 * 0.6)  # rough cos(53.14)
        lat, lon = 53.1435, 8.2147
        poly_wgs = Polygon(
            [
                (lon, lat),
                (lon + d_lon, lat),
                (lon + d_lon, lat + d_lat),
                (lon, lat + d_lat),
                (lon, lat),
            ]
        )
        poly_metric = _to_metric(poly_wgs)
        # Should be roughly 50x50 = 2500 m² — accept ±15% for the cos approx.
        assert 2125 <= poly_metric.area <= 2875

    def test_crs_constants(self):
        assert _METRIC_CRS == "EPSG:25832"
        assert _WGS84 == "EPSG:4326"


class TestContainmentRule:
    """Spec §3.2: only buildings *fully* within the AOI enter the balance;
    those the boundary cuts through are excluded and logged.

    We exercise the exact predicate the agent uses (`_is_fully_within_aoi`)
    on fixed polygons, in metric coords, to lock in the keep/exclude contract.
    """

    def test_fully_inside_kept(self):
        aoi = Polygon([(0, 0), (100, 0), (100, 100), (0, 100)])
        building = Polygon([(10, 10), (30, 10), (30, 30), (10, 30)])
        assert _is_fully_within_aoi(building, aoi)

    def test_crossing_boundary_excluded(self):
        aoi = Polygon([(0, 0), (100, 0), (100, 100), (0, 100)])
        building = Polygon([(90, 90), (110, 90), (110, 110), (90, 110)])
        assert not _is_fully_within_aoi(building, aoi)

    def test_flush_with_boundary_from_inside_kept(self):
        """Footprint shares two edges with the AOI but its whole body is inside,
        so it is *fully captured*, not clipped — spec §3.2 keeps it. `contains`
        is True because no point of the footprint lies in the AOI's exterior."""
        aoi = Polygon([(0, 0), (100, 0), (100, 100), (0, 100)])
        building = Polygon([(90, 0), (100, 0), (100, 10), (90, 10)])
        assert _is_fully_within_aoi(building, aoi)

    def test_touching_boundary_from_outside_excluded(self):
        """Footprint whose body sits outside the AOI, sharing only the boundary
        edge, has points in the AOI's exterior → clipped/incomplete → excluded."""
        aoi = Polygon([(0, 0), (100, 0), (100, 100), (0, 100)])
        building = Polygon([(100, 0), (110, 0), (110, 10), (100, 10)])
        assert not _is_fully_within_aoi(building, aoi)
