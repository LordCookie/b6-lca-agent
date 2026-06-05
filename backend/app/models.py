"""Pydantic schemas exchanged between UI, orchestrator, and agents.

These shapes are the contract — the report.md / b6_results.json fields
ultimately serialize from these models.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Provenance(str, Enum):
    PROVIDED = "provided"        # spec §10: *bereitgestellt*
    BENCHMARK = "benchmark"      # *Benchmark*
    ASSUMPTION = "assumption"    # *Annahme*


class LLMResearchResult(BaseModel):
    """Structured output the research-agent LLM is required to produce.

    Spec §10: a value is only accepted when ``source_url`` is present.
    ``confidence='not_found'`` is the explicit "no value" signal — never
    fabricate to satisfy the schema.
    """
    field: str                       # e.g. "build_year"
    value: float | int | str | None  # null when not found
    source_url: str | None           # MUST be present to accept the value
    source_title: str | None = None
    confidence: str                  # "found" | "not_found"
    reasoning: str | None = None     # short rationale; for the audit trail


class SpeciesResearchResult(BaseModel):
    """Structured output for the vegetation species/allometry web research.

    The LLM only *classifies* the dominant tree group (the allometry numbers
    stay literature-based, picked from allometry.yaml). Spec §10: ``group`` is
    only honoured when backed by a cited ``source_url``.
    """
    group: str                       # "laubbaum" | "nadelbaum" | "mischbestand"
    species_examples: list[str] = Field(default_factory=list)
    source_url: str | None
    source_title: str | None = None
    confidence: str                  # "found" | "not_found"
    reasoning: str | None = None


# --- KI-Empfehlungen (Reporting-Agent) ---

class SanierungsMassnahme(BaseModel):
    """Eine konkrete Sanierungs-/Optimierungs-Empfehlung."""
    massnahme: str                          # z.B. "Fassadendämmung WLG 035 mit 14 cm"
    erwartete_einsparung: str | None = None  # z.B. "~15 % thermisch"
    investitions_kategorie: str | None = None  # "klein" | "mittel" | "groß"


class Hotspot(BaseModel):
    """Ein Gebäude mit überproportionalem Beitrag zur Cluster-Bilanz."""
    osm_id: str
    name: str | None = None
    prioritaet: str                          # "hoch" | "mittel" | "niedrig"
    begruendung: str                          # Warum dieses Gebäude Hotspot ist
    massnahmen: list[SanierungsMassnahme] = Field(default_factory=list)


class AuffaelligerBefund(BaseModel):
    """Ein methodischer Befund (kein Sanierungs-Vorschlag, sondern eine Beobachtung)."""
    kennzahl: str                            # z.B. "Strombedarf"
    befund: str                              # was auffällig ist
    vermutete_ursache: str | None = None
    empfohlene_pruefung: str | None = None


class ReportingAdvice(BaseModel):
    """KI-Empfehlungs-Block für den Bericht. Spec §10: kein Wert wird erfunden,
    nur die bereits berechneten Werte werden interpretiert."""
    executive_summary: str                   # 2-3 Sätze für Laien
    cluster_assessment: str                  # Bewertung der Cluster-Bilanz vs. Benchmark
    hotspots: list[Hotspot] = Field(default_factory=list)
    auffaellige_befunde: list[AuffaelligerBefund] = Field(default_factory=list)


# --- Outlier-Kommentare (Research-Agent) ---

class OutlierComment(BaseModel):
    """Fachliche Erklärung eines markierten Outliers."""
    issue_key: str                           # eindeutiger Index in flagged-Liste
    fachliche_erklaerung: str                # was bedeutet das in der Praxis
    empfohlene_aktion: str | None = None     # was sollte der User tun


class OutlierCommentBatch(BaseModel):
    """Sammelantwort des LLM für mehrere Outliers in einem Call."""
    comments: list[OutlierComment]


# --- Material-Agent UUID-Vorschlag ---

class MaterialCarrierSuggestion(BaseModel):
    """LLM-Vorschlag für eine ÖKOBAUDAT-UUID + Begründung."""
    carrier_key: str
    suggested_uuid: str | None
    suggested_name: str | None = None
    modul: str = "B6"
    reasoning: str                            # warum dieser Datensatz passt
    confidence: str                           # "found" | "no_good_match"


class SourceRef(BaseModel):
    """Reference to the origin of a value. Spec §10/§11/§14."""
    label: str
    provenance: Provenance
    uuid: str | None = None              # e.g. ÖKOBAUDAT UUID
    citation: str | None = None          # GEG fundstelle, DOI, etc.
    retrieved_at: datetime | None = None
    version: str | None = None


class Value(BaseModel):
    """A single physical quantity with unit, source, and uncertainty."""
    value: float
    unit: str
    uncertainty: float | None = None     # +/- band (gauss propagation, §6)
    source: SourceRef
    pedigree: dict[str, int] | None = None  # data-quality matrix (§4 step 4)


class PolygonGeoJSON(BaseModel):
    type: str = "Polygon"
    coordinates: list[list[list[float]]]


class AOI(BaseModel):
    """Area of interest selected on the map."""
    name: str
    polygon: PolygonGeoJSON
    crs: str = "EPSG:4326"


class Building(BaseModel):
    osm_id: str
    name: str | None = None
    footprint_area_m2: Value
    storeys: Value | None = None
    height_m: Value | None = None
    ngf_m2: Value | None = None          # DIN 277 — spec §5
    use_mix: dict[str, float] = Field(default_factory=dict)  # e.g. {"UNIVERSITY": 0.7, "RESTAURANT": 0.3}
    build_year: int | None = None        # filled by the research agent (web research / heuristic)
    field_sources: dict[str, str] = Field(default_factory=dict)
    # per-field source tag for the pedigree matrix:
    #   "osm" | "llm_research" | "default_heuristic" | "default_class"
    excluded: bool = False
    exclusion_reason: str | None = None  # randständig geschnitten -> dokumentiert


class EnergyResult(BaseModel):
    """Spec §4: *strict* split heat vs. electricity, no netting of exports."""
    end_energy_heat_kwh: Value
    end_energy_electricity_kwh: Value
    penrt_kwh: Value
    gwp_kg_co2eq: Value
    gwp_per_ngf_kg_co2eq_per_m2a: Value


class BuildingResult(BaseModel):
    building: Building
    energy: EnergyResult


class VegetationResult(BaseModel):
    """Spec §8 — separate from B6."""
    canopy_area_m2: Value
    estimated_trees: int
    co2_stock_kg: Value
    # OSM-augmented details (spec §8 cross-validation with semantic OSM tags)
    osm_only_trees: int = 0                 # OSM tree nodes that ONNX missed
    osm_green_area_m2: float = 0.0          # union(woodland + parks + grass)
    imagery_source: str = "DOP20"           # "DOP20" or "ESRI World Imagery"


class ClusterResult(BaseModel):
    aoi: AOI
    buildings: list[BuildingResult]
    cluster_energy: EnergyResult
    vegetation: VegetationResult | None = None


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class StepStatus(BaseModel):
    name: str
    status: RunStatus
    started_at: datetime | None = None
    finished_at: datetime | None = None
    message: str | None = None


class RunState(BaseModel):
    run_id: str
    started_at: datetime
    status: RunStatus
    steps: list[StepStatus]
    aoi: AOI | None = None
    result: ClusterResult | None = None
    error: str | None = None
