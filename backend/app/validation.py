"""Validation layer (spec §9).

Every value entering the pipeline crosses this layer.
Outliers are flagged and reasoned about — never silently accepted (spec §10).
"""
from __future__ import annotations

from dataclasses import dataclass

# Standard pedigree matrix dimensions (DQI) — Weidema 1996 / Ciroth et al.
PEDIGREE_DIMENSIONS = (
    "reliability",
    "completeness",
    "temporal_correlation",
    "geographical_correlation",
    "technological_correlation",
)


@dataclass
class CheckResult:
    ok: bool
    message: str


# Plausibility ranges (spec §9 examples). Extend as needed.
_RANGES: dict[str, tuple[float, float, str]] = {
    "u_value": (0.1, 3.0, "W/m2K"),
    "footprint_area_m2": (10.0, 100_000.0, "m2"),
    "ngf_m2": (10.0, 200_000.0, "m2"),
    "height_m": (2.0, 200.0, "m"),
    "storeys": (1.0, 60.0, "-"),
    "end_energy_kwh_per_m2a": (10.0, 800.0, "kWh/m2a"),
}


def plausibility_check(name: str, value: float, unit: str | None = None) -> CheckResult:
    if name not in _RANGES:
        return CheckResult(True, f"{name}: no range registered, skipped")
    lo, hi, expected_unit = _RANGES[name]
    if unit and unit != expected_unit:
        return CheckResult(False, f"{name}: unit mismatch (got {unit}, expected {expected_unit})")
    if not (lo <= value <= hi):
        return CheckResult(False, f"{name}={value} outside plausibility range [{lo}, {hi}] {expected_unit}")
    return CheckResult(True, f"{name}={value} {expected_unit} OK")
