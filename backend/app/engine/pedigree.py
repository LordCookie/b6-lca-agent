"""Pedigree-Matrix (DQI) per data source (spec §3.4, §7, §9).

Standard Weidema/Ciroth scoring on five dimensions, integer 1-5
(1 = best, 5 = worst). Each source class has a default score sheet
that the research agent applies. Outliers can override per-field.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

# Dimension keys must match validation.PEDIGREE_DIMENSIONS.
PEDIGREE_DIMS = (
    "reliability",
    "completeness",
    "temporal_correlation",
    "geographical_correlation",
    "technological_correlation",
)


@dataclass(frozen=True)
class Pedigree:
    reliability: int
    completeness: int
    temporal_correlation: int
    geographical_correlation: int
    technological_correlation: int
    note: str = ""

    def as_dict(self) -> dict[str, int | str]:
        return asdict(self)


# Default DQI per source class. These are defensible mid-of-road values;
# the research agent can override per data point if justified.
DEFAULTS: dict[str, Pedigree] = {
    "osm_tag": Pedigree(
        reliability=2, completeness=2, temporal_correlation=3,
        geographical_correlation=1, technological_correlation=3,
        note="Direkter OSM-Tag, Community-erfasst, Datenfrische variabel",
    ),
    "osm_derived": Pedigree(
        reliability=3, completeness=3, temporal_correlation=3,
        geographical_correlation=1, technological_correlation=3,
        note="Aus OSM-Tags abgeleitet (z. B. height ≈ storeys × 3 m)",
    ),
    "default_heuristic": Pedigree(
        reliability=3, completeness=3, temporal_correlation=3,
        geographical_correlation=3, technological_correlation=3,
        note="Defensiver Default ohne objekt-spezifische Quelle",
    ),
    "default_class": Pedigree(
        reliability=3, completeness=4, temporal_correlation=3,
        geographical_correlation=2, technological_correlation=3,
        note="Klassen-Default (z. B. TABULA-Hüllenklasse, IPCC-Allometrie)",
    ),
    "oekobaudat": Pedigree(
        reliability=1, completeness=1, temporal_correlation=2,
        geographical_correlation=1, technological_correlation=2,
        note="ÖKOBAUDAT 2024-I, EN 15804+A2 (EF 3.1) konform, UUID-pinned",
    ),
    "llm_research_unsourced": Pedigree(
        reliability=5, completeness=5, temporal_correlation=5,
        geographical_correlation=5, technological_correlation=5,
        note="LLM-Antwort ohne verifizierte Quelle — sollte nicht verwendet werden",
    ),
    "llm_research_with_source": Pedigree(
        reliability=3, completeness=2, temporal_correlation=2,
        geographical_correlation=2, technological_correlation=3,
        note="LLM-Recherche mit zitierter Sekundärquelle, nicht primär-verifiziert",
    ),
    "geg_anlage_4": Pedigree(
        reliability=1, completeness=1, temporal_correlation=1,
        geographical_correlation=1, technological_correlation=1,
        note="GEG 2024 Anlage 4 — gesetzliche Festlegung",
    ),
}


def for_source(source_class: str) -> Pedigree:
    return DEFAULTS.get(source_class, DEFAULTS["default_heuristic"])


def classify(source_label: str) -> str:
    """Best-effort mapping from a Value.source.label to a DQI class.

    Falls back to default_heuristic — the research agent should set the
    field_source explicitly so we don't depend on label fuzzy-matching.
    """
    s = source_label.lower()
    if "ökobaudat" in s or "oekobaudat" in s or "obd" in s:
        return "oekobaudat"
    if "geg" in s and "anlage" in s:
        return "geg_anlage_4"
    if "osm tag" in s or "osm way" in s or "osm relation" in s:
        return "osm_tag"
    if "osm" in s:
        return "osm_derived"
    if "tabula" in s or "din v 18599-10" in s or "agfw" in s or "ipcc" in s:
        return "default_class"
    if "llm" in s:
        return "llm_research_with_source"
    return "default_heuristic"
