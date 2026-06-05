"""Material agent integration tests.

Uses the real ÖKOBAUDAT snapshot (no network). Skips when the snapshot
is not mounted (e.g. CI without the data volume).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from app.agents.base import AgentError
from app.agents.material import (
    _SWO_FERNWAERME_DEFAULT_KG_CO2EQ_PER_KWH,
    MaterialAgent,
)
from app.config import settings
from app.models import Provenance

REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = REPO_ROOT / "data" / "oekobaudat" / "oekobaudat_2024-I_2026-05-25.csv"
CARRIER_MAP = REPO_ROOT / "data" / "oekobaudat" / "carrier_map.yaml"


@pytest.fixture
def patched_paths(tmp_path, monkeypatch):
    """Point the agent at the real snapshot files, run_dir at tmp."""
    if not SNAPSHOT.is_file() or not CARRIER_MAP.is_file():
        pytest.skip("OBD snapshot or carrier_map.yaml missing")
    monkeypatch.setattr(settings, "oekobaudat_csv_path", SNAPSHOT)
    monkeypatch.setattr(settings, "carrier_map_path", CARRIER_MAP)
    # Force the "no central lookup" path so OBD is used.
    monkeypatch.setattr(settings, "lookup_table_path", tmp_path / "nope.xlsx")
    return tmp_path


class TestResolutionChain:
    def test_swo_falls_back_to_documented_default(self, patched_paths):
        agent = MaterialAgent(llm=None, run_dir=patched_paths)  # type: ignore[arg-type]
        out = agent.run({"energy_carriers": ["heat_fernwaerme_swo"]})
        v = out["gwp_factors"]["heat_fernwaerme_swo"]
        assert v.value == pytest.approx(_SWO_FERNWAERME_DEFAULT_KG_CO2EQ_PER_KWH)
        # Critical: the default must be flagged as ASSUMPTION, not PROVIDED.
        assert v.source.provenance == Provenance.ASSUMPTION
        assert "AGFW" in v.source.label

    def test_electricity_resolves_from_oekobaudat(self, patched_paths):
        agent = MaterialAgent(llm=None, run_dir=patched_paths)  # type: ignore[arg-type]
        out = agent.run({"energy_carriers": ["electricity_grid"]})
        v = out["gwp_factors"]["electricity_grid"]
        assert v.value == pytest.approx(0.4102, abs=1e-3)
        assert v.source.provenance == Provenance.PROVIDED
        assert v.source.uuid == "669c531b-aa84-4d04-b96b-aa0f075952b3"
        assert "OBD" in (v.source.version or "")

    def test_erdgas_resolves_from_oekobaudat(self, patched_paths):
        agent = MaterialAgent(llm=None, run_dir=patched_paths)  # type: ignore[arg-type]
        out = agent.run({"energy_carriers": ["heat_erdgas"]})
        v = out["gwp_factors"]["heat_erdgas"]
        assert v.value == pytest.approx(0.2511, abs=1e-3)
        assert v.source.provenance == Provenance.PROVIDED

    def test_unknown_carrier_stops(self, patched_paths):
        agent = MaterialAgent(llm=None, run_dir=patched_paths)  # type: ignore[arg-type]
        with pytest.raises(AgentError, match="STOP"):
            agent.run({"energy_carriers": ["completely_unknown_carrier"]})
