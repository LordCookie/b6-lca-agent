"""Research agent tests — default heuristics, plausibility, pedigree, source classification.

No LLM is invoked here — the research agent only calls the LLM for build_year
research when settings.research_llm_fallback is True AND an LLM client is passed.
These tests pass llm=None, so only the deterministic path (OSM gaps closed by
documented default heuristics) runs.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.agents.research import ResearchAgent
from app.engine.pedigree import classify, for_source
from app.models import Building, Provenance, SourceRef, Value

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_value(v, unit="m2", label="OSM way/123"):
    return Value(
        value=v, unit=unit,
        source=SourceRef(label=label, provenance=Provenance.PROVIDED,
                         retrieved_at=datetime.now(timezone.utc)),
    )


def _basic_building(osm_id="way/123", footprint=1500.0, storeys=None, height=None,
                    ngf=None, use_mix=None, build_year=None):
    return Building(
        osm_id=osm_id,
        name="Test",
        footprint_area_m2=_make_value(footprint, "m2", f"OSM way {osm_id}"),
        storeys=_make_value(storeys, "-", f"OSM tag building:levels (id={osm_id})") if storeys else None,
        height_m=_make_value(height, "m", f"OSM tag height (id={osm_id})") if height else None,
        ngf_m2=_make_value(ngf, "m2", "test ngf") if ngf else None,
        use_mix=use_mix or {},
        build_year=build_year,
    )


@pytest.fixture
def agent(tmp_path):
    run_dir = tmp_path / "run"
    (run_dir / "sources").mkdir(parents=True)
    return ResearchAgent(llm=None, run_dir=run_dir)  # type: ignore[arg-type]


# --- default heuristics ---------------------------------------------------


class TestHeuristics:
    def test_storey_height_cross_fill(self, agent):
        # Only OSM storeys present; height must be derived at 3 m/level.
        b = _basic_building(osm_id="way/999", storeys=3)
        agent.run({"buildings": [b]})
        assert b.height_m.value == 9.0
        assert b.field_sources["height_m"] == "osm_derived"

    def test_ngf_default_from_footprint_times_storeys(self, agent):
        b = _basic_building(osm_id="way/999", storeys=3)
        agent.run({"buildings": [b]})
        # NGF default: footprint × storeys = 1500 × 3 = 4500
        assert b.ngf_m2.value == 4500.0
        assert b.field_sources["ngf_m2"] == "default_heuristic"

    def test_use_mix_default_to_university(self, agent):
        b = _basic_building(osm_id="way/999", storeys=3)
        agent.run({"buildings": [b]})
        assert b.use_mix == {"UNIVERSITY": 1.0}
        assert b.field_sources["use_mix"] == "default_heuristic"

    def test_notfall_default_when_neither_height_nor_storeys(self, agent):
        # Only footprint known — emergency defaults keep the energy balance alive.
        b = _basic_building(osm_id="way/123")
        agent.run({"buildings": [b]})
        assert b.storeys is not None and b.storeys.value == 2.0
        assert b.height_m is not None and b.height_m.value == 6.0
        assert b.field_sources["storeys"] == "default_heuristic"
        assert b.field_sources["height_m"] == "default_heuristic"


# --- plausibility ---------------------------------------------------------


class TestPlausibility:
    def test_flag_missing_build_year(self, agent):
        b = _basic_building(osm_id="way/999", storeys=3)
        out = agent.run({"buildings": [b]})
        assert any(f["field"] == "build_year" for f in out["flagged"])

    def test_flag_use_mix_not_summing_to_one(self, agent):
        b = _basic_building(osm_id="way/999", storeys=3, use_mix={"UNIVERSITY": 0.6, "RESTAURANT": 0.3})
        out = agent.run({"buildings": [b]})
        assert any(f["field"] == "use_mix" for f in out["flagged"])

    def test_flag_height_storeys_mismatch(self, agent):
        # Storeys say 3 (→ 9 m default) but height claims 30 m → ratio 3.33×.
        b = _basic_building(osm_id="way/999", storeys=3, height=30.0)
        out = agent.run({"buildings": [b]})
        assert any(f["field"] == "height_vs_storeys" for f in out["flagged"])

    def test_excluded_buildings_skipped(self, agent):
        b = _basic_building(osm_id="way/EXCL")
        b.excluded = True
        b.exclusion_reason = "clipped"
        out = agent.run({"buildings": [b]})
        # No gap-fill attempted on excluded buildings
        assert b.ngf_m2 is None
        assert b.use_mix == {}


# --- pedigree matrix ------------------------------------------------------


class TestPedigree:
    def test_oekobaudat_source_gets_top_score(self):
        ped = for_source("oekobaudat")
        assert ped.reliability == 1
        assert ped.completeness == 1

    def test_default_class_not_better_than_oekobaudat(self):
        o = for_source("oekobaudat")
        d = for_source("default_class")
        assert d.reliability >= o.reliability

    def test_classify_oekobaudat_label(self):
        assert classify("Fernwärme Mix Deutschland (ÖKOBAUDAT 2024-I)") == "oekobaudat"

    def test_classify_osm_label(self):
        assert classify("OSM way/12345") == "osm_tag"

    def test_classify_falls_back_to_default(self):
        assert classify("some unknown label nobody mentioned") == "default_heuristic"

    def test_pedigree_matrix_built(self, agent):
        b = _basic_building(osm_id="way/999", storeys=3)
        out = agent.run({"buildings": [b]})
        pedigree = out["pedigree"]
        assert pedigree, "pedigree matrix must not be empty"
        # all rows must have the five DQI dimensions
        for row in pedigree.values():
            for dim in ("reliability", "completeness", "temporal_correlation",
                        "geographical_correlation", "technological_correlation"):
                assert dim in row


# --- written artefacts ----------------------------------------------------


class TestWrittenArtefacts:
    def test_pedigree_json_written(self, agent):
        b = _basic_building(osm_id="way/999", storeys=3)
        agent.run({"buildings": [b]})
        out = agent.run_dir / "sources" / "pedigree.json"
        assert out.is_file()
        import json
        data = json.loads(out.read_text(encoding="utf-8"))
        assert "scoring_convention" in data
        assert "default_scores" in data
        assert "pedigree_per_source" in data
