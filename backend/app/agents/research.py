"""Research / validation agent (spec §7, §9, §13).

Does three things, in order:

  1. Fill OSM weaknesses (height, NGF, build_year, use_mix). OSM is good for
     footprint but typically missing the rest. build_year is researched via
     the LLM web-search tool (strict source citation, spec §10); the other
     gaps are closed with documented default heuristics.

  2. Plausibility-check every value against the validation layer (spec §9):
     plausibility ranges + cross-field consistency (e.g. storeys × 3 m ≈ height).
     Outliers are *flagged*, never silently dropped.

  3. Build the pedigree matrix per source class:
     DQI scores from engine.pedigree.DEFAULTS, one entry per (source_label,
     source_class) actually used.

The LLM build_year research uses the openrouter:web_search server-tool and
only accepts values backed by a real url_citation (spec §10: no fabricated
values). Disable via settings.research_llm_fallback=False.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from ..config import settings
from ..llm.web_search import WebSearchError, web_search
from ..engine.pedigree import DEFAULTS as PEDIGREE_DEFAULTS
from ..engine.pedigree import classify as classify_source
from ..engine.pedigree import for_source as pedigree_for_source
from ..models import (
    Building,
    LLMResearchResult,
    OutlierComment,
    OutlierCommentBatch,
    Provenance,
    SourceRef,
    Value,
)
from ..validation import plausibility_check
from .base import Agent

log = logging.getLogger("lca.agent.research")

# Default storey height when only one of height/storeys is known.
_DEFAULT_STOREY_HEIGHT_M = 3.0


class ResearchAgent(Agent):
    name = "research"

    def run(self, context: dict[str, Any]) -> dict[str, Any]:
        buildings: list[Building] = context.get("buildings", [])

        # Location hint for web-search queries (e.g. AOI name "Jade HS Oldenburg").
        aoi = context.get("aoi")
        self._location_hint = getattr(aoi, "name", "") or ""

        gap_fill_log: list[dict[str, Any]] = []
        flagged: list[dict[str, Any]] = []
        sources_used: dict[str, str] = {}  # source_label -> source_class

        llm_calls_remaining = settings.research_llm_max_calls_per_run if settings.research_llm_fallback else 0

        for b in buildings:
            if b.excluded:
                continue
            # Spec §13 + §10: LLM-Recherche mit openrouter:web_search Tool.
            # Schließt Lücken die OSM offen ließ. Strikte Quellen-Pflicht.
            if llm_calls_remaining > 0 and b.build_year is None:
                if self._try_llm_fill_build_year(b, gap_fill_log, sources_used, flagged):
                    llm_calls_remaining -= 1
                else:
                    llm_calls_remaining -= 1  # count the attempt too
            self._apply_default_heuristics(b, gap_fill_log, sources_used)
            self._plausibility(b, flagged)
            self._track_existing_sources(b, sources_used)

        # Spec §1+§13: Outlier-Kommentare via LLM, ergibt fachliche Erklärung
        # für jeden flagged-Eintrag. Einzel-Call mit Batch-Output (günstig).
        outlier_comments: dict[str, OutlierComment] = {}
        if (settings.validation_llm_enabled and self.llm is not None
                and flagged):
            outlier_comments = self._llm_comment_outliers(flagged)

        pedigree = self._build_pedigree_matrix(sources_used)
        self._write_pedigree_log(pedigree, gap_fill_log, flagged)

        self._log_step(
            "research/validation complete",
            n_buildings=len(buildings),
            n_gap_fills=len(gap_fill_log),
            n_flagged=len(flagged),
            n_sources_rated=len(pedigree),
        )
        # Outlier-Kommentare an die flagged-Einträge anheften, damit
        # Frontend + Report sie zusammen anzeigen können.
        for f in flagged:
            key = f"{f.get('osm_id', '?')}::{f.get('field', '?')}"
            comment = outlier_comments.get(key)
            if comment:
                f["llm_erklaerung"] = comment.fachliche_erklaerung
                if comment.empfohlene_aktion:
                    f["llm_aktion"] = comment.empfohlene_aktion

        return {
            "buildings": buildings,
            "pedigree": pedigree,
            "flagged": flagged,
            "gap_fill_log": gap_fill_log,
        }

    # ------------------------------------------------------------------
    # 1) Default heuristics for the gaps OSM didn't close
    # ------------------------------------------------------------------

    def _apply_default_heuristics(
        self,
        b: Building,
        gap_fill_log: list[dict[str, Any]],
        sources_used: dict[str, str],
    ) -> None:
        """Defensive defaults when OSM provided no value.

        Each default raises the pedigree (worse DQI) so the reader of the
        report sees which fields are weakest.
        """
        retrieved = datetime.now(timezone.utc)

        # Height ↔ storeys cross-fill: if exactly one is present, derive the other.
        if b.height_m is None and b.storeys is not None and b.storeys.value > 0:
            derived = b.storeys.value * _DEFAULT_STOREY_HEIGHT_M
            label = "Default-Heuristik: height = storeys x 3 m"
            b.height_m = Value(
                value=derived, unit="m",
                source=SourceRef(label=label, provenance=Provenance.ASSUMPTION,
                                 retrieved_at=retrieved),
            )
            b.field_sources["height_m"] = "osm_derived"
            sources_used[label] = "osm_derived"
            gap_fill_log.append({"osm_id": b.osm_id, "field": "height_m",
                                  "value": derived, "from": "osm_derived"})

        if b.storeys is None and b.height_m is not None and b.height_m.value > 0:
            derived = max(1.0, round(b.height_m.value / _DEFAULT_STOREY_HEIGHT_M))
            label = "Default-Heuristik: storeys = round(height / 3 m)"
            b.storeys = Value(
                value=derived, unit="-",
                source=SourceRef(label=label, provenance=Provenance.ASSUMPTION,
                                 retrieved_at=retrieved),
            )
            b.field_sources["storeys"] = "osm_derived"
            sources_used[label] = "osm_derived"
            gap_fill_log.append({"osm_id": b.osm_id, "field": "storeys",
                                  "value": derived, "from": "osm_derived"})

        # Last-resort default: weder OSM noch Cross-Fill liefern Höhe oder
        # Geschosse. Setze defensive Mindest-Werte (2 Geschosse / 6 m),
        # damit die Energie-Bilanz nicht abbricht. Wird klar geflaggt.
        if b.storeys is None and b.height_m is None:
            label_storeys = "Notfall-Default: storeys = 2 (OSM ohne Höhe/Geschosse)"
            label_height = "Notfall-Default: height = 6 m (OSM ohne Höhe/Geschosse)"
            b.storeys = Value(
                value=2.0, unit="-",
                source=SourceRef(label=label_storeys, provenance=Provenance.ASSUMPTION,
                                 retrieved_at=retrieved),
            )
            b.height_m = Value(
                value=6.0, unit="m",
                source=SourceRef(label=label_height, provenance=Provenance.ASSUMPTION,
                                 retrieved_at=retrieved),
            )
            b.field_sources["storeys"] = "default_heuristic"
            b.field_sources["height_m"] = "default_heuristic"
            sources_used[label_storeys] = "default_heuristic"
            sources_used[label_height] = "default_heuristic"
            gap_fill_log.append({"osm_id": b.osm_id, "field": "storeys",
                                  "value": 2, "from": "notfall_default"})
            gap_fill_log.append({"osm_id": b.osm_id, "field": "height_m",
                                  "value": 6, "from": "notfall_default"})

        # NGF default: footprint × storeys when OSM provides no area.
        if b.ngf_m2 is None and b.storeys is not None and b.storeys.value > 0:
            derived = b.footprint_area_m2.value * b.storeys.value
            label = "Default-Heuristik: NGF = Grundflaeche x Geschosse (DIN 277 nicht verifiziert)"
            b.ngf_m2 = Value(
                value=derived, unit="m2",
                source=SourceRef(label=label, provenance=Provenance.ASSUMPTION,
                                 retrieved_at=retrieved),
            )
            b.field_sources["ngf_m2"] = "default_heuristic"
            sources_used[label] = "default_heuristic"
            gap_fill_log.append({"osm_id": b.osm_id, "field": "ngf_m2",
                                  "value": round(derived, 1), "from": "default_heuristic"})

        # use_mix default: full UNIVERSITY for the Jade-HS cluster context.
        # This is a Jade-HS-specific assumption; explicit and tracked.
        if not b.use_mix:
            b.use_mix = {"UNIVERSITY": 1.0}
            label = "Default-Annahme: UNIVERSITY 100% (Cluster-Kontext Jade HS)"
            b.field_sources["use_mix"] = "default_heuristic"
            sources_used[label] = "default_heuristic"
            gap_fill_log.append({"osm_id": b.osm_id, "field": "use_mix",
                                  "value": "UNIVERSITY:1.0", "from": "default_heuristic"})

        # build_year default: stays None — the energy agent then uses the
        # configured default envelope class and we flag the gap below.
        if b.build_year is None:
            b.field_sources["build_year"] = "default_class"
            sources_used["Default-Hüllenklasse build_year_class_1980_2000"] = "default_class"

    # ------------------------------------------------------------------
    # 3) Plausibility checks
    # ------------------------------------------------------------------

    def _plausibility(self, b: Building, flagged: list[dict[str, Any]]) -> None:
        for field_name, value in (
            ("footprint_area_m2", b.footprint_area_m2),
            ("ngf_m2", b.ngf_m2),
            ("height_m", b.height_m),
            ("storeys", b.storeys),
        ):
            if value is None:
                continue
            check = plausibility_check(field_name, value.value, value.unit)
            if not check.ok:
                flagged.append({"osm_id": b.osm_id, "field": field_name,
                                 "issue": check.message})

        # Cross-field: height vs. storeys × 3 m should be within ±50 %.
        if b.height_m and b.storeys and b.storeys.value > 0:
            ratio = b.height_m.value / (b.storeys.value * _DEFAULT_STOREY_HEIGHT_M)
            if not 0.5 <= ratio <= 1.5:
                flagged.append({
                    "osm_id": b.osm_id, "field": "height_vs_storeys",
                    "issue": f"height={b.height_m.value:.1f} m vs. {b.storeys.value:g} storeys "
                             f"(ratio {ratio:.2f}× expected 3-m default)",
                })

        # use_mix must sum to 1.0 within tolerance.
        if b.use_mix:
            total = sum(b.use_mix.values())
            if abs(total - 1.0) > 1e-3:
                flagged.append({"osm_id": b.osm_id, "field": "use_mix",
                                 "issue": f"sum={total:.3f}, expected 1.0"})

        # Spec §10: missing build_year is acceptable but worth flagging so the
        # reviewer of the report knows which envelope class is a default.
        if b.build_year is None:
            flagged.append({"osm_id": b.osm_id, "field": "build_year",
                             "issue": "no build_year — default envelope class used"})

    # ------------------------------------------------------------------
    # 4) Source tracking + pedigree matrix
    # ------------------------------------------------------------------

    def _track_existing_sources(
        self,
        b: Building,
        sources_used: dict[str, str],
    ) -> None:
        for value in (b.footprint_area_m2, b.ngf_m2, b.height_m, b.storeys):
            if value is None:
                continue
            label = value.source.label
            if label not in sources_used:
                sources_used[label] = classify_source(label)

    @staticmethod
    def _build_pedigree_matrix(
        sources_used: dict[str, str],
    ) -> dict[str, dict[str, int | str]]:
        out: dict[str, dict[str, int | str]] = {}
        for label, source_class in sources_used.items():
            ped = pedigree_for_source(source_class)
            row = ped.as_dict()
            row["source_class"] = source_class
            out[label] = row
        return out

    # ------------------------------------------------------------------
    # I/O helpers
    # ------------------------------------------------------------------

    def _write_pedigree_log(
        self,
        pedigree: dict[str, dict[str, int | str]],
        gap_fill_log: list[dict[str, Any]],
        flagged: list[dict[str, Any]],
    ) -> None:
        out = self.run_dir / "sources" / "pedigree.json"
        out.write_text(
            json.dumps(
                {
                    "scoring_convention": (
                        "DQI 1 (best) - 5 (worst) on 5 Weidema/Ciroth dimensions; "
                        "defaults from engine.pedigree.DEFAULTS"
                    ),
                    "default_scores": {k: asdict(v) for k, v in PEDIGREE_DEFAULTS.items()},
                    "pedigree_per_source": pedigree,
                    "gap_fills": gap_fill_log,
                    "flagged": flagged,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self._log_step("pedigree matrix written", path=str(out))

    # ------------------------------------------------------------------
    # 5) LLM research path — self-hosted web search (spec §10 + §13)
    #    We run the search ourselves (llm/web_search.py, keyless DuckDuckGo)
    #    and let the LLM only EXTRACT the build_year from the REAL results,
    #    citing one of the returned URLs. No reliance on any model-side tool,
    #    so it works regardless of which model is routed to the agent.
    # ------------------------------------------------------------------

    _SYSTEM_PROMPT = (
        "Du bist ein Recherche-Assistent für eine wissenschaftliche Bachelor-Thesis "
        "(B6-Bilanzierung nach DIN EN 15978). Dir werden ECHTE Web-Suchtreffer "
        "(Titel, URL, Auszug) vorgelegt. Deine Aufgabe: das Baujahr eines konkreten "
        "Gebäudes NUR dann angeben, wenn es durch einen der Treffer belegt ist.\n\n"
        "STRIKTE REGELN (Spec §10):\n"
        "- Stütze den Wert AUSSCHLIESSLICH auf die vorgelegten Treffer, nutze KEIN "
        "Vorwissen.\n"
        "- source_url MUSS exakt eine der vorgelegten URLs sein.\n"
        "- Wenn kein Treffer das Baujahr eindeutig belegt: confidence='not_found', "
        "value=null, source_url=null. Erfinde niemals einen Wert oder eine URL.\n"
        "- Bevorzuge offizielle/seriöse Treffer (Hochschule, Stadt, Wikipedia, "
        "Architektur-/Denkmal-Register).\n\n"
        "Antworte ausschließlich als JSON nach diesem Schema:\n"
        '{"field": "build_year", "value": int|null, '
        '"source_url": string|null, "source_title": string|null, '
        '"confidence": "found"|"not_found", "reasoning": string|null}'
    )

    def _try_llm_fill_build_year(
        self,
        b: Building,
        gap_fill_log: list[dict[str, Any]],
        sources_used: dict[str, str],
        flagged: list[dict[str, Any]],
    ) -> bool:
        """Fill `build_year` via self-hosted web search + LLM extraction.

        Spec §10: we perform the search ourselves (keyless DuckDuckGo) so every
        accepted value is backed by a real, citable URL — independent of any
        model-side tool. Returns True on success.
        """
        if self.llm is None:
            return False

        name = b.name or "(unbenanntes Gebäude)"
        location = (self._location_hint or "").strip()
        query = " ".join(p for p in [name, location, "Baujahr Gebäude"] if p).strip()

        # 1) Run the search ourselves (keyless DuckDuckGo).
        try:
            results = web_search(
                query,
                max_results=settings.web_search_max_results,
                region=settings.web_search_region,
            )
        except WebSearchError as exc:
            flagged.append({
                "osm_id": b.osm_id, "field": "build_year",
                "issue": f"Web-Suche nicht erreichbar: {exc}",
            })
            return False
        # Count the search for the report's KI-Nutzung table (keyless → 0 USD).
        self.llm.record_web_search("research", n=1)

        if not results:
            flagged.append({
                "osm_id": b.osm_id, "field": "build_year",
                "issue": f"Keine Web-Treffer für Query {query!r} — kein Baujahr übernommen.",
            })
            return False

        # 2) Let the LLM extract the year from the REAL results only.
        results_block = "\n".join(
            f"[{i}] {r.title}\n    URL: {r.url}\n    Auszug: {r.snippet}"
            for i, r in enumerate(results, 1)
        )
        valid_urls = {r.url for r in results}
        user_prompt = (
            f"Gebäude: {name}\n"
            f"OSM-ID: {b.osm_id}\n"
            + (f"Standort/Kontext: {location}\n" if location else "")
            + "\nWeb-Suchtreffer:\n"
            + results_block
            + "\n\nGib das Baujahr (Fertigstellung) als JSON zurück. "
            "source_url MUSS exakt eine der oben gelisteten URLs sein, "
            "sonst confidence='not_found'."
        )

        try:
            resp = self.llm.chat(
                agent="research",
                messages=[
                    {"role": "system", "content": self._SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.0,
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            log.warning("LLM extraction failed for %s: %s", b.osm_id, exc)
            flagged.append({
                "osm_id": b.osm_id, "field": "build_year",
                "issue": f"LLM-Extraktion fehlgeschlagen: {exc}",
            })
            return False

        result = self._parse_llm_research_result(resp.get("content"), "build_year")
        if result is None:
            flagged.append({
                "osm_id": b.osm_id, "field": "build_year",
                "issue": "LLM-Antwort nicht parsebar — kein Wert übernommen.",
            })
            return False

        if result.confidence != "found" or result.value is None or not result.source_url:
            flagged.append({
                "osm_id": b.osm_id, "field": "build_year",
                "issue": f"Kein belegtes Baujahr in den Treffern ({result.confidence}).",
            })
            return False

        # 3) The cited URL MUST be one of the real result URLs (anti-fabrication).
        if not any(
            result.source_url == u or result.source_url in u or u in result.source_url
            for u in valid_urls
        ):
            flagged.append({
                "osm_id": b.osm_id, "field": "build_year",
                "issue": (
                    f"LLM zitierte {result.source_url!r}, das nicht unter den echten "
                    "Suchtreffern ist — Wert abgelehnt (Spec §10)."
                ),
            })
            return False

        try:
            build_year = int(result.value)
        except (TypeError, ValueError):
            flagged.append({
                "osm_id": b.osm_id, "field": "build_year",
                "issue": f"Baujahr nicht ganzzahlig: {result.value!r}",
            })
            return False
        if not (1700 <= build_year <= 2030):
            flagged.append({
                "osm_id": b.osm_id, "field": "build_year",
                "issue": f"Baujahr {build_year} außerhalb des Plausibilitätsbereichs.",
            })
            return False

        b.build_year = build_year
        b.field_sources["build_year"] = "llm_research_with_source"
        label = f"Web-Recherche: {result.source_title or result.source_url}"
        sources_used[label] = "llm_research_with_source"
        gap_fill_log.append({
            "osm_id": b.osm_id, "field": "build_year",
            "value": build_year, "from": "llm_research_with_source",
            "source_url": result.source_url,
            "source_title": result.source_title,
        })
        self._log_step(
            "Baujahr per Web-Recherche gefüllt",
            osm_id=b.osm_id, build_year=build_year, url=result.source_url,
        )
        return True

    @staticmethod
    def _parse_llm_research_result(
        content: str | None, expected_field: str,
    ) -> LLMResearchResult | None:
        if not content:
            return None
        try:
            data = json.loads(content)
            result = LLMResearchResult.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as exc:
            log.warning("LLM research output failed parse/validate: %s", exc)
            return None
        if result.field != expected_field:
            log.warning("LLM returned field=%s, expected %s",
                        result.field, expected_field)
            return None
        return result

    # ------------------------------------------------------------------
    # 6) Outlier-Kommentare via LLM (spec §9 + §1)
    # ------------------------------------------------------------------

    _OUTLIER_SYSTEM_PROMPT = (
        "Du bist ein Validierungs-Assistent für Gebäude-Energiebilanzen. "
        "Eingangsdaten sind eine Liste markierter Ausreißer aus einer B6-Bilanz "
        "(z.B. unplausible Höhen, fehlende Baujahre, ungewöhnliche Verbrauchswerte). "
        "Deine Aufgabe: erkläre jeden Outlier knapp und sachlich (1-2 Sätze), und "
        "gib eine konkrete Handlungsempfehlung für den Nutzer.\n\n"
        "STRIKTE REGELN:\n"
        "- Du erfindest KEINE Werte und keine Bestätigung der Daten.\n"
        "- Wenn die Ursache unklar ist, schreibe das explizit.\n"
        "- Antworte ausschließlich als JSON nach diesem Schema:\n"
        '{"comments": [{"issue_key": string, "fachliche_erklaerung": string, '
        '"empfohlene_aktion": string|null}]}'
    )

    def _llm_comment_outliers(
        self, flagged: list[dict[str, Any]],
    ) -> dict[str, OutlierComment]:
        if self.llm is None:
            return {}
        # issue_key = osm_id::field, eindeutig
        items = []
        for f in flagged:
            key = f"{f.get('osm_id', '?')}::{f.get('field', '?')}"
            items.append({"issue_key": key,
                          "osm_id": f.get("osm_id"),
                          "field": f.get("field"),
                          "issue": f.get("issue")})
        try:
            resp = self.llm.chat(
                agent="research",
                messages=[
                    {"role": "system", "content": self._OUTLIER_SYSTEM_PROMPT},
                    {"role": "user", "content": (
                        "Markierte Ausreißer dieses Laufs:\n```json\n"
                        + json.dumps(items, ensure_ascii=False, indent=2)
                        + "\n```\n\nKommentiere alle Einträge."
                    )},
                ],
                temperature=0.0,
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            log.warning("outlier LLM call failed: %s", exc)
            return {}

        content = resp.get("content")
        if not content:
            return {}
        try:
            data = json.loads(content)
            batch = OutlierCommentBatch.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as exc:
            log.warning("outlier LLM output failed validation: %s", exc)
            return {}
        return {c.issue_key: c for c in batch.comments}
