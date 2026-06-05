"""Engine-level tests — pure numerics, no I/O beyond loading the YAMLs.

Covers:
  - utilisation-factor edge cases (γ=0, γ=1, γ→∞)
  - envelope geometry (height fallback, perimeter, NGF)
  - monthly balance plausibility against literature benchmark range (spec §4)
  - profile loaders + envelope-class picker
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.engine.balance import (
    _utilisation_factor,
    electricity_demand_kWh_a,
    monthly_heating_demand,
)
from app.engine.climate import load_climate
from app.engine.envelope import estimate_envelope
from app.engine.profiles import (
    load_envelope_defaults,
    load_system_efficiency,
    load_usage_profiles,
    pick_envelope_class,
)
from app.engine.uncertainty import absolute_band, combine_relative
from app.models import Building, Provenance, SourceRef, Value

REPO_ROOT = Path(__file__).resolve().parents[2]
CLIMATE_PATH = REPO_ROOT / "data" / "climate" / "oldenburg_tmyx.yaml"
USAGE_PATH = REPO_ROOT / "data" / "profiles" / "usage_profiles.yaml"
ENV_PATH = REPO_ROOT / "data" / "profiles" / "envelope_defaults.yaml"
SYS_PATH = REPO_ROOT / "data" / "profiles" / "system_efficiency.yaml"


def _value(v: float, unit: str) -> Value:
    return Value(
        value=v,
        unit=unit,
        source=SourceRef(label="test", provenance=Provenance.PROVIDED,
                         retrieved_at=datetime.now(timezone.utc)),
    )


def _building(footprint=1000.0, storeys=3, height=None, build_year=None) -> Building:
    return Building(
        osm_id="test/1",
        name="test",
        footprint_area_m2=_value(footprint, "m2"),
        storeys=_value(storeys, "-") if storeys else None,
        height_m=_value(height, "m") if height else None,
    )


# --- utilisation factor ---------------------------------------------------

class TestUtilisationFactor:
    def test_no_losses(self):
        assert _utilisation_factor(gains=100, losses=0) == 1.0

    def test_no_gains(self):
        # γ = 0 → η = 1
        assert _utilisation_factor(gains=0, losses=100) == 1.0

    def test_gamma_equals_one(self):
        # η = a/(a+1) = 0.5 for a=1
        assert _utilisation_factor(gains=100, losses=100) == pytest.approx(0.5)

    def test_gains_dominate(self):
        # γ >> 1 → η → 1/γ (small)
        eta = _utilisation_factor(gains=1000, losses=100)
        assert 0.0 < eta < 0.15

    def test_losses_dominate(self):
        # γ << 1 → η → 1 (almost all gains usable)
        eta = _utilisation_factor(gains=10, losses=1000)
        assert 0.95 < eta <= 1.0


# --- envelope -------------------------------------------------------------

class TestEnvelope:
    def test_height_from_storeys_when_height_missing(self):
        env_classes, default = load_envelope_defaults(ENV_PATH)
        envelope = estimate_envelope(_building(footprint=900.0, storeys=4),
                                     env_classes[default])
        assert envelope.height_m == 12.0  # 4 storeys × 3 m
        assert envelope.ngf_m2 == 3600.0
        # perimeter of a 30×30 square = 120 m
        assert envelope.perimeter_m == pytest.approx(120.0)
        # wall gross = 120 × 12 = 1440 m²; window 30% of that
        assert envelope.a_window_m2 == pytest.approx(1440.0 * 0.3)
        assert envelope.a_wall_opaque_m2 == pytest.approx(1440.0 * 0.7)

    def test_explicit_height_overrides_storeys(self):
        env_classes, default = load_envelope_defaults(ENV_PATH)
        envelope = estimate_envelope(
            _building(footprint=400.0, storeys=2, height=15.0),
            env_classes[default],
        )
        assert envelope.height_m == 15.0
        # storeys still derived from explicit storey count, not height
        assert envelope.ngf_m2 == 800.0

    def test_missing_both_raises(self):
        env_classes, default = load_envelope_defaults(ENV_PATH)
        with pytest.raises(ValueError, match="neither height nor storeys"):
            estimate_envelope(_building(storeys=None), env_classes[default])


# --- profile loaders ------------------------------------------------------

class TestProfileLoaders:
    def test_usage_profiles_load(self):
        profiles = load_usage_profiles(USAGE_PATH)
        assert "UNIVERSITY" in profiles
        assert "RESTAURANT" in profiles
        assert profiles["RESTAURANT"].q_electricity_kWh_per_m2a > profiles["UNIVERSITY"].q_electricity_kWh_per_m2a

    def test_envelope_classes_load(self):
        classes, default = load_envelope_defaults(ENV_PATH)
        assert default in classes
        # older buildings should have higher U-values
        assert classes["build_year_class_pre_1980"].u_wall > classes["build_year_class_post_2015"].u_wall

    def test_system_efficiency_load(self):
        systems = load_system_efficiency(SYS_PATH)
        fw = systems["fernwaerme"]
        assert fw.eta_total == pytest.approx(1.00 * 0.95 * 0.95, abs=1e-4)

    def test_pick_envelope_class_year_buckets(self):
        assert pick_envelope_class(1950) == "build_year_class_pre_1980"
        assert pick_envelope_class(1990) == "build_year_class_1980_2000"
        assert pick_envelope_class(2008) == "build_year_class_2000_2015"
        assert pick_envelope_class(2020) == "build_year_class_post_2015"
        assert pick_envelope_class(None) == "build_year_class_1980_2000"


# --- monthly balance plausibility ----------------------------------------

class TestMonthlyBalancePlausibility:
    """Sanity-check the balance produces values within a literature
    Teilenergiekennwert range (~50-200 kWh/m²a for institutional buildings
    depending on era + system).
    """

    def test_university_jade_default_class(self):
        env_classes, default = load_envelope_defaults(ENV_PATH)
        profile = load_usage_profiles(USAGE_PATH)["UNIVERSITY"]
        climate = load_climate(CLIMATE_PATH)
        building = _building(footprint=1500.0, storeys=3)
        envelope = estimate_envelope(building, env_classes[default])

        b = monthly_heating_demand(envelope, env_classes[default], profile, climate)
        kwh_per_m2 = b.Q_h_kWh_a / envelope.ngf_m2

        # 1980-2000 class, Oldenburg, 1500 m² × 3 storeys ~ 4500 m² NGF —
        # plausible Q_h around 60–200 kWh/m²a for an unrenovated 80s building
        # at university-style usage. Loose bracket — this is plausibility,
        # not a regression lock.
        assert 40.0 < kwh_per_m2 < 250.0, f"got {kwh_per_m2:.1f} kWh/m²a"
        # losses must exceed gains in winter — otherwise no heating demand at all
        assert b.Q_T_kWh_a > 0
        # max heating in deepest winter, none in July
        m1_jan = b.monthly_Q_h_kWh[0]
        m7_jul = b.monthly_Q_h_kWh[6]
        assert m1_jan > m7_jul

    def test_restaurant_higher_electricity_than_university(self):
        profiles = load_usage_profiles(USAGE_PATH)
        ngf = 1000.0
        e_uni = electricity_demand_kWh_a(profiles["UNIVERSITY"], ngf)
        e_rest = electricity_demand_kWh_a(profiles["RESTAURANT"], ngf)
        assert e_rest > 2 * e_uni  # kitchen loads


# --- uncertainty ----------------------------------------------------------

class TestUncertainty:
    def test_combine_two(self):
        # sqrt(0.1² + 0.1²) ≈ 0.1414
        assert combine_relative(0.1, 0.1) == pytest.approx(0.1414, abs=1e-3)

    def test_combine_ignores_none(self):
        assert combine_relative(0.1, None, 0.0) == pytest.approx(0.1)  # type: ignore[arg-type]

    def test_absolute_band(self):
        assert absolute_band(1000.0, 0.15) == 150.0
