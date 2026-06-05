"""Usage profile, envelope-default, and system-efficiency loaders.

All three configs are mounted as read-only YAML so they can be revised
without rebuilding the image — methodologically important: anyone reviewing
the thesis can re-run with corrected DIN V 18599-10 table values.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from ..config import settings


@dataclass(frozen=True)
class UsageProfile:
    key: str
    source: str
    theta_i_set_heating_C: float
    q_internal_W_per_m2: float
    q_electricity_kWh_per_m2a: float
    air_change_rate_1_per_h: float
    operation_hours_per_day: int
    operation_days_per_year: int
    uncertainty_rel: float


@dataclass(frozen=True)
class EnvelopeDefaults:
    key: str
    source: str
    u_wall: float
    u_roof: float
    u_floor: float
    u_window: float
    window_wall_ratio: float
    g_value: float
    shading_factor_F_S: float
    frame_factor_F_F: float
    uncertainty_rel: float


@dataclass(frozen=True)
class SystemEfficiency:
    key: str
    source: str
    eta_total: float
    uncertainty_rel: float


@lru_cache(maxsize=4)
def load_usage_profiles(path: Path | None = None) -> dict[str, UsageProfile]:
    p = Path(path) if path else _profiles_path("usage_profiles.yaml")
    with p.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return {
        key: UsageProfile(
            key=key,
            source=str(v["source"]),
            theta_i_set_heating_C=float(v["theta_i_set_heating_C"]),
            q_internal_W_per_m2=float(v["q_internal_W_per_m2"]),
            q_electricity_kWh_per_m2a=float(v["q_electricity_kWh_per_m2a"]),
            air_change_rate_1_per_h=float(v["air_change_rate_1_per_h"]),
            operation_hours_per_day=int(v["operation_hours_per_day"]),
            operation_days_per_year=int(v["operation_days_per_year"]),
            uncertainty_rel=float(v.get("uncertainty_rel", 0.15)),
        )
        for key, v in raw.items()
    }


@lru_cache(maxsize=4)
def load_envelope_defaults(
    path: Path | None = None,
) -> tuple[dict[str, EnvelopeDefaults], str]:
    """Return (classes by key, default_class_key)."""
    p = Path(path) if path else _profiles_path("envelope_defaults.yaml")
    with p.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    default_class_key = str(raw.pop("default_class"))
    by_key = {
        key: EnvelopeDefaults(
            key=key,
            source=str(v["source"]),
            u_wall=float(v["u_wall"]),
            u_roof=float(v["u_roof"]),
            u_floor=float(v["u_floor"]),
            u_window=float(v["u_window"]),
            window_wall_ratio=float(v["window_wall_ratio"]),
            g_value=float(v["g_value"]),
            shading_factor_F_S=float(v["shading_factor_F_S"]),
            frame_factor_F_F=float(v["frame_factor_F_F"]),
            uncertainty_rel=float(v.get("uncertainty_rel", 0.20)),
        )
        for key, v in raw.items()
    }
    if default_class_key not in by_key:
        raise ValueError(
            f"envelope_defaults.default_class={default_class_key} not found in classes"
        )
    return by_key, default_class_key


@lru_cache(maxsize=4)
def load_system_efficiency(path: Path | None = None) -> dict[str, SystemEfficiency]:
    p = Path(path) if path else _profiles_path("system_efficiency.yaml")
    with p.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    out: dict[str, SystemEfficiency] = {}
    for key, v in raw.items():
        eta_total = v.get("eta_total")
        if eta_total is None:
            eta_total = (
                float(v["eta_generation"])
                * float(v["eta_distribution"])
                * float(v["eta_transfer"])
            )
        out[key] = SystemEfficiency(
            key=key,
            source=str(v["source"]),
            eta_total=float(eta_total),
            uncertainty_rel=float(v.get("uncertainty_rel", 0.10)),
        )
    return out


def _profiles_path(name: str) -> Path:
    base = getattr(settings, "profiles_dir", None)
    if base:
        return Path(base) / name
    return Path("/app/data/profiles") / name


def pick_envelope_class(build_year: int | None) -> str:
    """Map a build year to one of the four envelope classes."""
    if build_year is None:
        return "build_year_class_1980_2000"  # mirrors envelope_defaults.default_class
    if build_year < 1980:
        return "build_year_class_pre_1980"
    if build_year < 2000:
        return "build_year_class_1980_2000"
    if build_year < 2015:
        return "build_year_class_2000_2015"
    return "build_year_class_post_2015"
