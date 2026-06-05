"""Envelope-area estimator from building footprint + height + storeys.

This is a deliberately simple geometric model:
  - Footprint area is treated as both ground-floor and roof area.
  - Wall area = footprint perimeter × height.
  - Window area = wall area × window/wall ratio (from envelope defaults).

Real 3D geometry from OSM or measured drawings would be more accurate;
this matches the OSM data-availability level (spec §3 step 4) and is
explicitly marked as a simplification in the report.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from ..models import Building
from .profiles import EnvelopeDefaults

# Default storey height when OSM only provides number of storeys.
_DEFAULT_STOREY_HEIGHT_M = 3.0


@dataclass(frozen=True)
class EnvelopeAreas:
    a_wall_opaque_m2: float
    a_window_m2: float
    a_roof_m2: float
    a_floor_m2: float
    height_m: float
    ngf_m2: float        # net floor area derived from footprint × storeys (estimate)
    perimeter_m: float


def estimate_envelope(
    building: Building,
    envelope: EnvelopeDefaults,
    storey_height_m: float = _DEFAULT_STOREY_HEIGHT_M,
) -> EnvelopeAreas:
    """Derive opaque/transparent envelope areas + NGF estimate.

    Priority for height (spec §3 step 4 — research agent later sharpens these):
      1. OSM tag `height`
      2. storeys × default storey height
      3. raise — geometry without any height info cannot be balanced
    """
    footprint = building.footprint_area_m2.value
    if footprint <= 0:
        raise ValueError(f"building {building.osm_id}: non-positive footprint area")

    height = _resolve_height(building, storey_height_m)
    storeys = _resolve_storeys(building, height, storey_height_m)
    perimeter = _square_equivalent_perimeter(footprint)

    a_wall_gross = perimeter * height
    wwr = envelope.window_wall_ratio
    a_window = a_wall_gross * wwr
    a_wall_opaque = a_wall_gross - a_window
    a_roof = footprint
    a_floor = footprint
    ngf_estimate = footprint * storeys

    return EnvelopeAreas(
        a_wall_opaque_m2=a_wall_opaque,
        a_window_m2=a_window,
        a_roof_m2=a_roof,
        a_floor_m2=a_floor,
        height_m=height,
        ngf_m2=ngf_estimate,
        perimeter_m=perimeter,
    )


def _resolve_height(building: Building, storey_height_m: float) -> float:
    if building.height_m and building.height_m.value > 0:
        return building.height_m.value
    if building.storeys and building.storeys.value > 0:
        return building.storeys.value * storey_height_m
    raise ValueError(
        f"building {building.osm_id}: neither height nor storeys available — "
        "research agent must fill OSM gap before energy balance can run (spec §3 step 4)"
    )


def _resolve_storeys(building: Building, height: float, storey_height_m: float) -> float:
    if building.storeys and building.storeys.value > 0:
        return building.storeys.value
    return max(1.0, round(height / storey_height_m))


def _square_equivalent_perimeter(area_m2: float) -> float:
    """Estimate perimeter as that of a square with the same area.

    A real OSM footprint has a known perimeter — wire it through from the
    geometry agent later. Square-equivalent is a defensible neutral default
    (lower bound for a given area; circle would give the true minimum, but
    square is closer to typical institutional buildings than a circle).
    """
    return 4.0 * math.sqrt(area_m2)
