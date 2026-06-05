"""OSM vegetation features for the AOI (spec §8 cross-validation).

Fetches OSM-tagged vegetation (individual trees, woodland, parks, grass
areas) via osmnx so the vegetation agent can combine the semantic OSM data
with the pixel-level ONNX segmentation:

  - OSM ``natural=tree`` nodes outside the ONNX canopy → added as
    ONNX-missed trees (with a documented default crown size).
  - OSM ``natural=wood`` / ``landuse=forest`` polygons → expected
    woodland regions (cross-check against ONNX coverage).
  - OSM ``leisure=park`` / ``garden`` polygons → green-space context.

All geometries are returned in EPSG:25832 (metric) so they line up with
the existing building/raster pipeline.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import osmnx as ox
from pyproj import Transformer
from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shp_transform

log = logging.getLogger("lca.engine.osm_vegetation")

# Local WGS84 → ETRS89/UTM-32 transformer — avoids a cross-layer import on
# agents.geometry; same target CRS as the rest of the pipeline.
_TRANSFORM_WGS84_TO_METRIC = Transformer.from_crs(
    "EPSG:4326", "EPSG:25832", always_xy=True
).transform


@dataclass(frozen=True)
class OSMVegetation:
    """OSM-tagged vegetation features in the AOI, metric (EPSG:25832)."""
    trees: list[Point] = field(default_factory=list)            # natural=tree (point/centroid)
    woodland: list[BaseGeometry] = field(default_factory=list)  # natural=wood, landuse=forest
    parks: list[BaseGeometry] = field(default_factory=list)     # leisure=park / garden
    grass: list[BaseGeometry] = field(default_factory=list)     # landuse=grass / meadow / recreation_ground

    @property
    def n_trees(self) -> int: return len(self.trees)

    @property
    def total_woodland_m2(self) -> float:
        return float(sum(g.area for g in self.woodland))

    @property
    def total_parks_m2(self) -> float:
        return float(sum(g.area for g in self.parks))

    @property
    def total_grass_m2(self) -> float:
        return float(sum(g.area for g in self.grass))

    @property
    def total_green_m2(self) -> float:
        """Union area of woodland + parks + grass (overlapping counted once)."""
        from shapely.ops import unary_union
        polys = list(self.woodland) + list(self.parks) + list(self.grass)
        if not polys:
            return 0.0
        return float(unary_union(polys).area)


def fetch_osm_vegetation(aoi_polygon_wgs84: BaseGeometry) -> OSMVegetation:
    """Fetch OSM-tagged vegetation features inside the AOI (WGS84 input).

    Returns metric (EPSG:25832) geometries categorised into trees / woodland /
    parks / grass. Network failure or no-features → empty :class:`OSMVegetation`
    (non-fatal, vegetation step continues with ONNX-only data).
    """
    tags: dict[str, Any] = {
        "natural": ["tree", "wood"],
        "landuse": ["forest", "grass", "meadow", "recreation_ground"],
        "leisure": ["park", "garden"],
    }
    try:
        gdf = ox.features_from_polygon(aoi_polygon_wgs84, tags=tags)
    except Exception as exc:
        log.warning("osmnx vegetation fetch failed: %s", exc)
        return OSMVegetation()

    if gdf is None or gdf.empty:
        log.info("OSM vegetation: no features matched in AOI")
        return OSMVegetation()

    trees: list[Point] = []
    woodland: list[BaseGeometry] = []
    parks: list[BaseGeometry] = []
    grass: list[BaseGeometry] = []

    for _, row in gdf.iterrows():
        geom = row.get("geometry")
        if geom is None or getattr(geom, "is_empty", True):
            continue
        try:
            geom_metric = shp_transform(_TRANSFORM_WGS84_TO_METRIC, geom)
        except Exception as exc:
            log.debug("skipping unprojectable feature: %s", exc)
            continue

        natural = str(row.get("natural") or "").lower()
        landuse = str(row.get("landuse") or "").lower()
        leisure = str(row.get("leisure") or "").lower()

        if natural == "tree":
            # tree-node geometries are usually points; coerce via centroid
            # for the rare case of a small polygon tagged as a tree.
            pt = geom_metric if geom_metric.geom_type == "Point" else geom_metric.centroid
            trees.append(pt)
        elif natural == "wood" or landuse == "forest":
            woodland.append(geom_metric)
        elif leisure in ("park", "garden"):
            parks.append(geom_metric)
        elif landuse in ("grass", "meadow", "recreation_ground"):
            grass.append(geom_metric)

    log.info(
        "OSM vegetation: %d trees · %d woodland · %d parks · %d grass",
        len(trees), len(woodland), len(parks), len(grass),
    )
    return OSMVegetation(trees=trees, woodland=woodland, parks=parks, grass=grass)


__all__ = ["fetch_osm_vegetation", "OSMVegetation"]
