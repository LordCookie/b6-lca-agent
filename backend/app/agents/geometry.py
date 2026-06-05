"""Geometry agent (spec §7).

Pulls OSM buildings inside the AOI polygon, counts them, builds the
per-building table. Buildings whose footprint is not *fully* contained
in the AOI are excluded and the exclusion is documented (spec §3 step 2).

Workflow
--------
1. AOI polygon arrives in WGS84 (EPSG:4326) from the browser.
2. Fetch building features via osmnx (Overpass). Cache enabled.
3. Reproject to a *metric* CRS (ETRS89 / UTM 32N, EPSG:25832 — covers
   Oldenburg) for valid area and containment computation. WGS84-area
   would be meaningless.
4. Deduplicate (osmnx returns extra rows for building:part / relations).
5. Parse OSM tags `building:levels`, `height`, `name`, `building` —
   robust to strings like "3", "12 m", "3-4".
6. Containment via `_is_fully_within_aoi` — *full* containment (`contains`),
   not intersects. A footprint flush with the boundary from the inside is
   kept (fully captured); one the boundary cuts through is excluded
   (spec §3 step 2).
7. Write `sources/buildings.csv` as an input snapshot.

Missing height/storeys are *not* invented here — the research agent
fills those gaps separately (spec §3 step 4).
"""
from __future__ import annotations

import csv
import logging
import re
from datetime import datetime, timezone
from typing import Any

import geopandas as gpd
import osmnx as ox
from pyproj import Transformer
from shapely.geometry import MultiPolygon, Polygon, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shp_transform

from ..config import settings
from ..models import AOI, Building, Provenance, SourceRef, Value
from ..validation import plausibility_check
from .base import Agent, AgentError

log = logging.getLogger("lca.agent.geometry")

# Oldenburg lies in UTM zone 32N — ETRS89 / UTM 32N = EPSG:25832.
# This is the official metric CRS for the area; use it for area, length,
# and containment so the geometry-clipping rule (spec §3.2) is exact.
_METRIC_CRS = "EPSG:25832"
_WGS84 = "EPSG:4326"

# Skip OSM noise (sheds, mistagged points polygonalised, single-pixel artefacts).
_MIN_FOOTPRINT_M2 = 5.0

_NUMBER_RE = re.compile(r"(-?\d+(?:[.,]\d+)?)")


def _to_metric(geom: BaseGeometry) -> BaseGeometry:
    transformer = Transformer.from_crs(_WGS84, _METRIC_CRS, always_xy=True)
    return shp_transform(transformer.transform, geom)


def _is_fully_within_aoi(footprint: BaseGeometry, aoi: BaseGeometry) -> bool:
    """Spec §3.2: a footprint enters the balance only if it lies *fully* within
    the AOI; one the boundary cuts through (part outside) is excluded.

    Uses ``contains`` deliberately, not ``contains_properly``: a footprint flush
    against the AOI boundary from the inside (it shares an edge but its whole
    body is inside) is *fully captured* and must be kept — ``contains_properly``
    would wrongly drop such complete buildings just for touching the line. A
    footprint that crosses the boundary, or sits outside sharing only an edge,
    has points in the AOI's exterior, so ``contains`` is False → excluded.
    """
    return aoi.contains(footprint)


def _parse_number(raw: Any) -> float | None:
    """Tolerant numeric parse for OSM tag strings like '12', '12.5', '12 m', '3-4', 'ca. 5'."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s.lower() in {"yes", "no"}:
        return None
    m = _NUMBER_RE.search(s.replace(",", "."))
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _osm_id_from_index(idx: Any) -> str:
    """osmnx returns a MultiIndex of (element_type, osmid) — flatten to 'way/123'."""
    if isinstance(idx, tuple) and len(idx) == 2:
        return f"{idx[0]}/{idx[1]}"
    return str(idx)


class GeometryAgent(Agent):
    name = "geometry"

    def run(self, context: dict[str, Any]) -> dict[str, Any]:
        aoi: AOI | None = context.get("aoi")
        if aoi is None:
            raise AgentError("geometry: no AOI in context")

        aoi_wgs = shape(aoi.polygon.model_dump())
        if not aoi_wgs.is_valid or aoi_wgs.is_empty:
            raise AgentError("geometry: AOI polygon is invalid or empty")

        self._log_step("fetching OSM buildings", aoi=aoi.name, bounds=aoi_wgs.bounds)
        gdf = self._fetch_buildings(aoi_wgs)
        if gdf.empty:
            raise AgentError("geometry: OSM returned zero buildings inside AOI — STOP")

        # Reproject AOI once; reuse for all containment checks.
        aoi_metric = _to_metric(aoi_wgs).buffer(0)  # heal self-touch edges
        retrieved_at = datetime.now(timezone.utc)

        kept: list[Building] = []
        excluded: list[Building] = []
        seen: set[str] = set()
        building_types: dict[str, int] = {}

        for idx, row in gdf.iterrows():
            osm_id = _osm_id_from_index(idx)
            if osm_id in seen:
                continue
            seen.add(osm_id)

            geom_wgs = row.geometry
            if geom_wgs is None or geom_wgs.is_empty:
                continue
            if not isinstance(geom_wgs, (Polygon, MultiPolygon)):
                continue  # points/lines aren't building footprints

            geom_metric = _to_metric(geom_wgs)
            area_m2 = float(geom_metric.area)
            if area_m2 < _MIN_FOOTPRINT_M2:
                continue

            tag_name = row.get("name") if "name" in row.index else None
            building_type = row.get("building") if "building" in row.index else None
            levels = _parse_number(row.get("building:levels") if "building:levels" in row.index else None)
            height_m = _parse_number(row.get("height") if "height" in row.index else None)

            footprint_check = plausibility_check("footprint_area_m2", area_m2, "m2")
            if not footprint_check.ok:
                log.warning("footprint plausibility: %s (osm_id=%s)", footprint_check.message, osm_id)

            building = Building(
                osm_id=osm_id,
                name=str(tag_name) if tag_name else None,
                footprint_area_m2=Value(
                    value=area_m2,
                    unit="m2",
                    source=SourceRef(
                        label=f"OSM way/relation {osm_id}",
                        provenance=Provenance.PROVIDED,
                        retrieved_at=retrieved_at,
                        version="osmnx Overpass snapshot",
                    ),
                ),
                storeys=self._value(
                    levels, "-", f"OSM tag building:levels (id={osm_id})", retrieved_at
                ),
                height_m=self._value(
                    height_m, "m", f"OSM tag height (id={osm_id})", retrieved_at
                ),
            )

            if not _is_fully_within_aoi(geom_metric, aoi_metric):
                building.excluded = True
                building.exclusion_reason = (
                    "footprint not fully contained in AOI (clipped by boundary)"
                )
                excluded.append(building)
            else:
                kept.append(building)
                if building_type:
                    building_types[str(building_type)] = building_types.get(str(building_type), 0) + 1

        if not kept:
            raise AgentError(
                "geometry: all OSM buildings were excluded as boundary-clipped — STOP. "
                "Check that the AOI fully contains the intended buildings."
            )

        self._log_step(
            "OSM extraction complete",
            kept=len(kept),
            excluded=len(excluded),
            building_types=building_types,
        )
        self._write_per_building_table(kept, excluded)
        return {
            "buildings": kept,
            "buildings_excluded": excluded,
        }

    # --- helpers ---------------------------------------------------------

    def _fetch_buildings(self, aoi_wgs: BaseGeometry) -> gpd.GeoDataFrame:
        ox.settings.use_cache = True
        ox.settings.requests_timeout = settings.timeout_osm_fetch
        ox.settings.log_console = False
        try:
            gdf = ox.features_from_polygon(aoi_wgs, tags={"building": True})
        except Exception as exc:
            raise AgentError(f"geometry: OSM fetch failed: {exc}") from exc

        if "building" in gdf.columns:
            gdf = gdf[gdf["building"].notna()]
        return gdf

    @staticmethod
    def _value(
        raw: float | None, unit: str, source_label: str, retrieved_at: datetime
    ) -> Value | None:
        if raw is None:
            return None
        return Value(
            value=raw,
            unit=unit,
            source=SourceRef(
                label=source_label,
                provenance=Provenance.PROVIDED,
                retrieved_at=retrieved_at,
            ),
        )

    def _write_per_building_table(
        self, kept: list[Building], excluded: list[Building]
    ) -> None:
        path = self.run_dir / "sources" / "buildings.csv"
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(
                [
                    "osm_id",
                    "name",
                    "excluded",
                    "exclusion_reason",
                    "footprint_area_m2",
                    "storeys",
                    "height_m",
                ]
            )
            for b in kept + excluded:
                w.writerow(
                    [
                        b.osm_id,
                        b.name or "",
                        b.excluded,
                        b.exclusion_reason or "",
                        f"{b.footprint_area_m2.value:.2f}",
                        f"{b.storeys.value:g}" if b.storeys else "",
                        f"{b.height_m.value:g}" if b.height_m else "",
                    ]
                )
        self._log_step("per-building table written", path=str(path))
