"""Integration tests against the real ÖKOBAUDAT snapshot.

These hit the 32 MB CSV in data/oekobaudat/ — they're fast enough
(<1 s) thanks to the lru_cache on load_snapshot.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.data.oekobaudat_loader import (
    OekobaudatRecord,
    _parse_decimal,
    _ref_to_kwh,
    load_snapshot,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = REPO_ROOT / "data" / "oekobaudat" / "oekobaudat_2024-I_2026-05-25.csv"


class TestPureHelpers:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("0,254", 0.254),
            ("3,6", 3.6),
            ("1", 1.0),
            ("", None),
            (None, None),
            ("not-a-number", None),
        ],
    )
    def test_parse_decimal(self, raw, expected):
        assert _parse_decimal(raw) == expected

    def test_ref_to_kwh_from_mj(self):
        # 3.6 MJ = 1 kWh
        assert _ref_to_kwh(3.6, "MJ") == pytest.approx(1.0)

    def test_ref_to_kwh_from_kwh(self):
        assert _ref_to_kwh(1.0, "kWh") == 1.0

    def test_ref_to_kwh_rejects_kg(self):
        with pytest.raises(ValueError, match="not supported"):
            _ref_to_kwh(1.0, "kg")


@pytest.mark.skipif(not SNAPSHOT.is_file(), reason="OBD snapshot not mounted")
class TestRealSnapshot:
    def test_snapshot_loads_and_indexes(self):
        snap = load_snapshot(SNAPSHOT)
        assert snap.n_rows > 1000
        assert snap.snapshot_date == date(2026, 5, 25)
        assert snap.version_label == "2024-I"

    def test_fernwaerme_mix_de(self):
        """Spec sanity check: Fernwärme Mix DE B6 ~ 0.254 kg CO2eq/kWh."""
        snap = load_snapshot(SNAPSHOT)
        rec = snap.get("bff1909a-5383-49bb-a450-aa9543e7a9ee", "B6")
        assert isinstance(rec, OekobaudatRecord)
        assert "Fernw" in rec.name_de
        # Bezugsgröße is 3,6 MJ = 1 kWh, so the per-kWh factor equals
        # the raw GWPtotal value.
        factor = rec.to_factor_kg_co2eq_per_kwh()
        assert factor == pytest.approx(0.2537, abs=1e-3)

    def test_strom_gebaeudebetrieb_2021(self):
        snap = load_snapshot(SNAPSHOT)
        rec = snap.get("669c531b-aa84-4d04-b96b-aa0f075952b3", "B6")
        assert (factor := rec.to_factor_kg_co2eq_per_kwh())
        assert factor == pytest.approx(0.4102, abs=1e-3)

    def test_thermische_energie_erdgas(self):
        snap = load_snapshot(SNAPSHOT)
        rec = snap.get("2561c686-875a-44e5-8abe-47ec392d16ff", "B6")
        assert rec.to_factor_kg_co2eq_per_kwh() == pytest.approx(0.2511, abs=1e-3)

    def test_unknown_uuid_raises(self):
        snap = load_snapshot(SNAPSHOT)
        with pytest.raises(KeyError, match="has no row"):
            snap.get("does-not-exist", "B6")

    def test_unknown_module_for_known_uuid_raises(self):
        snap = load_snapshot(SNAPSHOT)
        # Fernwärme Mix DE only has Modul B6 — A1-A3 does not exist.
        with pytest.raises(KeyError):
            snap.get("bff1909a-5383-49bb-a450-aa9543e7a9ee", "A1-A3")
