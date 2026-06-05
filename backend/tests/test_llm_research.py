"""Tests for the self-hosted web-search research path (spec §13 + §10).

The web search (llm/web_search.py) is mocked so tests are deterministic and
offline; the LLM is replaced by a stub returning canned JSON. We verify:
  - valid response citing a real result URL → value accepted
  - confidence='not_found' → no value taken, flagged
  - missing source_url → refused even if value is set
  - cited URL NOT among the real search results → refused (anti-fabrication)
  - no search results at all → no value taken
  - unparseable response → refused
  - cost cap respected
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

import app.agents.research as research_mod
from app.agents.research import ResearchAgent
from app.config import settings
from app.llm.web_search import SearchResult
from app.models import Building, Provenance, SourceRef, Value


def _val(v, unit, label="OSM"):
    return Value(
        value=v, unit=unit,
        source=SourceRef(label=label, provenance=Provenance.PROVIDED,
                         retrieved_at=datetime.now(timezone.utc)),
    )


def _building(osm_id="way/123", name="Test", storeys=3):
    return Building(
        osm_id=osm_id, name=name,
        footprint_area_m2=_val(1500.0, "m2"),
        storeys=_val(storeys, "-"),
    )


class _LLMStub:
    """Replaces LLMClient.chat with a scripted response + counts calls."""
    def __init__(self, response_content: str):
        self.response_content = response_content
        self.calls: list[dict] = []
        self.web_searches = 0

    def chat(self, agent, messages, **kwargs):
        self.calls.append({"agent": agent, "messages": messages, **kwargs})
        return {
            "model": "stub",
            "content": self.response_content,
            "annotations": [],
            "tool_calls": None,
            "usage": None,
            "cost_usd": 0.0,
            "web_search_requests": 0,
        }

    def record_web_search(self, agent, n=1, cost_usd=0.0):
        self.web_searches += n


def _install_search(monkeypatch, urls):
    """Patch research.web_search to return canned results with the given URLs."""
    results = [
        SearchResult(title=f"Treffer {i}", url=u, snippet="… Baujahr …")
        for i, u in enumerate(urls, 1)
    ]
    monkeypatch.setattr(research_mod, "web_search", lambda *a, **k: list(results))
    return results


@pytest.fixture
def patched_paths(tmp_path, monkeypatch):
    # Web search is the only source of build_year (no reference-file gap-fill).
    monkeypatch.setattr(settings, "research_llm_fallback", True)
    monkeypatch.setattr(settings, "research_llm_max_calls_per_run", 10)
    run_dir = tmp_path / "run"
    (run_dir / "sources").mkdir(parents=True)
    return run_dir


class TestLLMHappyPath:
    def test_valid_response_with_cited_url(self, patched_paths, monkeypatch):
        url = "https://jade-hs.de/gebaeude/ha"
        _install_search(monkeypatch, [url])
        llm = _LLMStub(
            response_content=json.dumps({
                "field": "build_year",
                "value": 1985,
                "source_url": url,
                "source_title": "Jade HS Gebäudechronik",
                "confidence": "found",
                "reasoning": "Treffer nennt Fertigstellung 1985",
            }),
        )
        agent = ResearchAgent(llm=llm, run_dir=patched_paths)  # type: ignore[arg-type]
        b = _building()
        out = agent.run({"buildings": [b]})

        assert b.build_year == 1985
        assert b.field_sources["build_year"] == "llm_research_with_source"
        assert any(e["field"] == "build_year" and e["from"] == "llm_research_with_source"
                   for e in out["gap_fill_log"])
        assert llm.web_searches >= 1  # we recorded the search


class TestLLMRefusals:
    def test_not_found_response(self, patched_paths, monkeypatch):
        _install_search(monkeypatch, ["https://example.org/x"])
        llm = _LLMStub(
            response_content=json.dumps({
                "field": "build_year", "value": None,
                "source_url": None, "source_title": None,
                "confidence": "not_found", "reasoning": "Keine Quelle gefunden",
            }),
        )
        agent = ResearchAgent(llm=llm, run_dir=patched_paths)  # type: ignore[arg-type]
        b = _building()
        out = agent.run({"buildings": [b]})

        assert b.build_year is None
        assert any(f["field"] == "build_year" and "not_found" in f["issue"]
                   for f in out["flagged"])

    def test_value_without_source_url_rejected(self, patched_paths, monkeypatch):
        _install_search(monkeypatch, ["https://example.org/x"])
        llm = _LLMStub(
            response_content=json.dumps({
                "field": "build_year", "value": 1985,
                "source_url": None, "source_title": None,
                "confidence": "found",
            }),
        )
        agent = ResearchAgent(llm=llm, run_dir=patched_paths)  # type: ignore[arg-type]
        b = _building()
        agent.run({"buildings": [b]})
        assert b.build_year is None

    def test_url_not_in_results_rejected(self, patched_paths, monkeypatch):
        # Model cites a URL that is NOT among the real search results —
        # likely hallucinated → must be refused.
        _install_search(monkeypatch, ["https://different-site.example/x"])
        llm = _LLMStub(
            response_content=json.dumps({
                "field": "build_year", "value": 1985,
                "source_url": "https://made-up-site.example/building",
                "source_title": "Fake",
                "confidence": "found",
            }),
        )
        agent = ResearchAgent(llm=llm, run_dir=patched_paths)  # type: ignore[arg-type]
        b = _building()
        out = agent.run({"buildings": [b]})
        assert b.build_year is None
        assert any("Suchtreffern" in f["issue"] for f in out["flagged"])

    def test_no_search_results(self, patched_paths, monkeypatch):
        # isolate: no outlier-comment LLM call should be counted as "extraction".
        monkeypatch.setattr(settings, "validation_llm_enabled", False)
        _install_search(monkeypatch, [])  # search found nothing
        llm = _LLMStub(response_content=json.dumps({
            "field": "build_year", "value": 1985,
            "source_url": "https://x.example", "confidence": "found",
        }))
        agent = ResearchAgent(llm=llm, run_dir=patched_paths)  # type: ignore[arg-type]
        b = _building()
        agent.run({"buildings": [b]})
        assert b.build_year is None
        # LLM extraction must NOT even be called when there are no results
        assert len(llm.calls) == 0

    def test_unparseable_response_rejected(self, patched_paths, monkeypatch):
        _install_search(monkeypatch, ["https://example.org/x"])
        llm = _LLMStub(response_content="this is not json at all")
        agent = ResearchAgent(llm=llm, run_dir=patched_paths)  # type: ignore[arg-type]
        b = _building()
        agent.run({"buildings": [b]})
        assert b.build_year is None

    def test_implausible_year_rejected(self, patched_paths, monkeypatch):
        url = "https://x.example"
        _install_search(monkeypatch, [url])
        llm = _LLMStub(
            response_content=json.dumps({
                "field": "build_year", "value": 1500,  # out of range
                "source_url": url, "source_title": "X",
                "confidence": "found",
            }),
        )
        agent = ResearchAgent(llm=llm, run_dir=patched_paths)  # type: ignore[arg-type]
        b = _building()
        agent.run({"buildings": [b]})
        assert b.build_year is None


class TestCostCap:
    def test_max_calls_respected(self, patched_paths, monkeypatch):
        monkeypatch.setattr(settings, "research_llm_max_calls_per_run", 2)
        # isolate the build_year cost-cap: don't let the outlier-comment LLM
        # call (fired for flagged buildings) inflate the call count.
        monkeypatch.setattr(settings, "validation_llm_enabled", False)
        url = "https://x.example"
        _install_search(monkeypatch, [url])
        llm = _LLMStub(
            response_content=json.dumps({
                "field": "build_year", "value": 1985,
                "source_url": url, "source_title": "X",
                "confidence": "found",
            }),
        )
        agent = ResearchAgent(llm=llm, run_dir=patched_paths)  # type: ignore[arg-type]
        buildings = [_building(osm_id=f"way/{i}") for i in range(5)]
        agent.run({"buildings": buildings})
        # Only the first 2 buildings should have triggered an LLM extraction.
        assert len(llm.calls) == 2
