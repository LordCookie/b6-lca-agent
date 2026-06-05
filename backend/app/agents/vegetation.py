"""Vegetation agent (spec §8).

Runs DEEPNESS ONNX inference *locally* (no QGIS GUI, no cloud) on a clipped
aerial-imagery raster, extracts tree crowns, applies the stem-foot filter
("Stammfuß innerhalb der Grenze, nicht Kronenüberhang"), and runs the
allometric chain crown-diameter → DBH → AGB → C → CO₂.

Output is reported SEPARATELY from B6 — spec §8: "fließt nicht in B6 ein".
The orchestrator treats a failing vegetation step as non-fatal so the B6
pipeline still completes when the raster or ONNX model is missing.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import onnxruntime as ort
from pydantic import ValidationError
from shapely.geometry import shape
from shapely.ops import transform as shp_transform

from ..agents.geometry import _to_metric  # reuse the WGS84 → EPSG:25832 transformer
from ..config import settings
from ..engine.osm_vegetation import OSMVegetation, fetch_osm_vegetation
from ..engine.satellite_tiles import fetch_aoi_imagery
from ..engine.vegetation import (
    SPECIES_GROUPS,
    AllometryParams,
    Tree,
    VegetationStats,
    allometric_co2_stock,
    apply_species_group,
    augment_with_osm_trees,
    clip_raster_to_aoi,
    extract_trees,
    load_allometry,
    render_raster_preview,
    tiled_inference,
)
from ..llm.web_search import WebSearchError, web_search
from ..models import (
    AOI,
    Provenance,
    SourceRef,
    SpeciesResearchResult,
    Value,
    VegetationResult,
)
from .base import Agent, AgentError

log = logging.getLogger("lca.agent.vegetation")

_SLUG_RE = re.compile(r"[^a-zA-Z0-9_\-]+")


class VegetationAgent(Agent):
    name = "vegetation"

    def run(self, context: dict[str, Any]) -> dict[str, Any]:
        aoi: AOI | None = context.get("aoi")
        if aoi is None:
            raise AgentError("vegetation: no AOI in context")

        # Spec §8: Vegetation ist *separat* von B6 und darf den Lauf nicht
        # blockieren. Wir versuchen erst das lokale DOP20-Raster (höchste
        # Auflösung, ~0,2 m/px) und fallen auf ESRI-World-Imagery zurück
        # (~0,3-1 m/px), damit das Tool für jede AOI funktioniert.
        if not settings.deepness_onnx_path.is_file():
            return self._skipped(
                f"ONNX-Modell fehlt unter {settings.deepness_onnx_path}. "
                "Siehe data/deepness/README.md für Download-Hinweise."
            )

        # AOI in beiden CRS — WGS84 für OSM/Tile-Fetch, EPSG:25832 für Raster.
        aoi_wgs = shape(aoi.polygon.model_dump())
        aoi_metric = _to_metric(aoi_wgs).buffer(0)

        local_path = self._resolve_raster_path(aoi, context)
        if local_path.is_file():
            raster_path = local_path
            imagery_source = "DOP20 (lokal)"
            self._log_step("Vegetation: lokales DOP20 gefunden", path=str(raster_path))
        else:
            try:
                raster_path = self._fetch_satellite(aoi_wgs)
            except Exception as exc:
                return self._skipped(
                    f"Kein lokales DOP20 ({local_path.name}) und ESRI-Satellit-"
                    f"Download fehlgeschlagen: {exc}"
                )
            imagery_source = "ESRI World Imagery"
            self._log_step("Vegetation: ESRI-Satellit gezogen", path=str(raster_path))

        allometry = load_allometry(self._allometry_path())
        # Spec §8 refinement: research the dominant tree group at the location
        # (web search, cited) and pick literature group-allometry instead of the
        # generic default. Falls back to the generic default if research fails.
        allometry = self._research_species_group(aoi, allometry)

        # 1) Clip raster to AOI (in metric CRS).
        self._log_step("clipping raster to AOI", raster=str(raster_path))
        array_chw, transform, crs = clip_raster_to_aoi(raster_path, aoi_metric)
        px_w, px_h = abs(transform.a), abs(transform.e)
        self._log_step(
            "raster clipped",
            shape=tuple(array_chw.shape),
            crs=crs,
            ground_resolution_m=f"{px_w:.3f} x {px_h:.3f}",
        )

        # Spec §3 step 3: the clipped raster must be visible in the UI.
        preview_path = self.run_dir / "results" / "aoi_raster_preview.png"
        try:
            render_raster_preview(array_chw, preview_path)
            self._log_step("AOI raster preview written", path=str(preview_path))
        except Exception as exc:
            # Non-fatal: vegetation step continues even if preview fails.
            log.warning("failed to render AOI raster preview: %s", exc)

        # 2) ONNX tiled inference.
        session = ort.InferenceSession(
            str(settings.deepness_onnx_path),
            providers=["CPUExecutionProvider"],
        )
        self._log_step(
            "running DEEPNESS inference",
            model=str(settings.deepness_onnx_path),
            tile_size=settings.deepness_tile_size,
            overlap=settings.deepness_tile_overlap,
        )
        mask = tiled_inference(
            session,
            array_chw,
            tile_size=settings.deepness_tile_size,
            overlap=settings.deepness_tile_overlap,
            threshold=settings.deepness_threshold,
            target_class=settings.deepness_target_class,
        )

        # 3) Stem-foot filter + CD → CO₂.
        trees = extract_trees(mask, transform, aoi_metric, allometry)

        # 3a) OSM cross-validation — merge OSM natural=tree / parks / forest
        # with the ONNX detection. Trees the ONNX missed get added; park /
        # forest polygons are surfaced separately (informational).
        n_osm_added = 0
        osm_green_m2 = 0.0
        osm = OSMVegetation()
        if settings.osm_vegetation_enabled:
            osm = fetch_osm_vegetation(aoi_wgs)
            osm_green_m2 = osm.total_green_m2
            if osm.trees:
                trees, n_osm_added = augment_with_osm_trees(
                    trees, mask, transform, aoi_metric, osm.trees,
                    default_crown_diameter_m=settings.osm_tree_default_crown_m,
                )
                self._log_step(
                    "OSM cross-validation",
                    osm_trees_total=osm.n_trees,
                    osm_trees_added=n_osm_added,
                    osm_woodland_m2=round(osm.total_woodland_m2, 1),
                    osm_parks_m2=round(osm.total_parks_m2, 1),
                )

        stats = allometric_co2_stock(trees, allometry, raster_pixel_area_m2=px_w * px_h)
        self._log_step(
            "vegetation extraction complete",
            n_trees=stats.n_trees,
            n_flagged=stats.n_flagged,
            n_osm_only=n_osm_added,
            canopy_m2=round(stats.total_canopy_area_m2, 1),
            co2_kg=round(stats.co2_stock_kg, 1),
            imagery_source=imagery_source,
        )

        # 4) Write per-tree dump for the report.
        self._write_tree_log(trees, stats, raster_path, allometry.source)

        veg_result = self._to_model(
            stats, raster_path, allometry.source,
            osm_only_trees=n_osm_added,
            osm_green_area_m2=osm_green_m2,
            imagery_source=imagery_source,
        )
        return {"vegetation": veg_result}

    # --- helpers ----------------------------------------------------------

    def _skipped(self, reason: str) -> dict[str, Any]:
        """Gracefully skip the vegetation step (spec §8: non-fatal)."""
        self._log_step("vegetation skipped", reason=reason)
        return {
            "vegetation": None,
            "vegetation_skipped": True,
            "vegetation_skip_reason": reason,
        }

    def _fetch_satellite(self, aoi_wgs) -> Path:
        """Download (and cache) an ESRI World Imagery mosaic for the AOI bbox.

        Cache key is the bbox + zoom level, so re-running the same AOI does
        not re-download. Raises on persistent fetch failure — caller skips
        gracefully.
        """
        tiles_dir = settings.imagery_cache_dir / "tiles"
        mosaics_dir = settings.imagery_cache_dir / "mosaics"
        bb = aoi_wgs.bounds
        key = (
            f"esri_z{settings.tile_zoom}_"
            f"{bb[0]:.5f}_{bb[1]:.5f}_{bb[2]:.5f}_{bb[3]:.5f}.tif"
        )
        out_path = mosaics_dir / key
        if out_path.is_file():
            self._log_step("ESRI-Mosaic aus Cache", path=str(out_path))
            return out_path
        return fetch_aoi_imagery(
            aoi_wgs, out_path,
            zoom=settings.tile_zoom, cache_dir=tiles_dir,
        )

    # ------------------------------------------------------------------
    # Species / allometry research (spec §8 refinement, spec §10 sourcing)
    # ------------------------------------------------------------------

    _SPECIES_SYSTEM_PROMPT = (
        "Du bist ein Recherche-Assistent (Stadtökologie). Dir werden ECHTE "
        "Web-Suchtreffer zum Baumbestand eines Standorts vorgelegt. Klassifiziere "
        "die DOMINANTE Baumgruppe für die Allometrie.\n\n"
        "REGELN (Spec §10):\n"
        "- group ∈ {'laubbaum','nadelbaum','mischbestand'}, anhand der Treffer.\n"
        "- source_url MUSS exakt eine der vorgelegten URLs sein.\n"
        "- Erlauben die Treffer keine belastbare Aussage: confidence='not_found', "
        "group='mischbestand', source_url=null. Erfinde nichts.\n\n"
        "Antworte ausschließlich als JSON:\n"
        '{"group": "laubbaum"|"nadelbaum"|"mischbestand", '
        '"species_examples": [string], "source_url": string|null, '
        '"source_title": string|null, "confidence": "found"|"not_found", '
        '"reasoning": string|null}'
    )

    def _research_species_group(
        self, aoi: AOI, allometry: AllometryParams,
    ) -> AllometryParams:
        """Refine allometry by researching the dominant tree group (cited).

        Returns the (possibly overridden) allometry. Any failure → generic
        default unchanged (non-fatal, spec §8).
        """
        if self.llm is None or not settings.vegetation_species_research:
            return allometry

        location = (getattr(aoi, "name", "") or "").strip()
        query = " ".join(
            p for p in [location, "Baumbestand Baumarten Park Campus"] if p
        ).strip()
        try:
            results = web_search(
                query,
                max_results=settings.web_search_max_results,
                region=settings.web_search_region,
            )
        except WebSearchError as exc:
            log.warning("species web_search unavailable: %s", exc)
            return allometry
        self.llm.record_web_search("vegetation", n=1)
        if not results:
            self._log_step("Baumart-Recherche: keine Web-Treffer — generischer Default")
            return allometry

        results_block = "\n".join(
            f"[{i}] {r.title}\n    URL: {r.url}\n    Auszug: {r.snippet}"
            for i, r in enumerate(results, 1)
        )
        valid_urls = {r.url for r in results}
        user_prompt = (
            (f"Standort: {location}\n\n" if location else "")
            + "Web-Suchtreffer:\n"
            + results_block
            + "\n\nKlassifiziere die dominante Baumgruppe als JSON. "
            "source_url MUSS exakt eine der oben gelisteten URLs sein."
        )
        try:
            resp = self.llm.chat(
                agent="vegetation",
                messages=[
                    {"role": "system", "content": self._SPECIES_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.0,
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            log.warning("species LLM call failed: %s", exc)
            return allometry

        content = resp.get("content")
        if not content:
            return allometry
        try:
            sr = SpeciesResearchResult.model_validate(json.loads(content))
        except (json.JSONDecodeError, ValidationError) as exc:
            log.warning("species LLM output invalid: %s", exc)
            return allometry

        if sr.group not in SPECIES_GROUPS:
            return allometry
        url_ok = bool(sr.source_url) and any(
            sr.source_url == u or sr.source_url in u or u in sr.source_url
            for u in valid_urls
        )
        # Spec §10: only override with a source-backed, in-results citation.
        if sr.confidence != "found" or not url_ok:
            self._log_step(
                "Baumart-Recherche ohne belastbare Quelle — generischer Default",
                group=sr.group,
            )
            return allometry

        species = ", ".join(sr.species_examples[:5]) if sr.species_examples else sr.group
        note = (
            f"Allometrie-Gruppe '{sr.group}' (Web-Recherche: {species}; "
            f"Quelle: {sr.source_title or sr.source_url})"
        )
        refined = apply_species_group(
            allometry, self._allometry_path(), sr.group, source_note=note,
        )
        self._log_step(
            "Baumart/Allometrie per Web-Recherche gesetzt",
            group=sr.group, species=species, url=sr.source_url,
        )
        return refined

    @staticmethod
    def _resolve_raster_path(aoi: AOI, context: dict[str, Any]) -> Path:
        # Caller can override per-run via context["aoi_raster"]; otherwise
        # convention is <imagery_dir>/<slug(aoi.name)>.tif.
        override = context.get("aoi_raster")
        if override:
            return Path(override)
        slug = _SLUG_RE.sub("_", aoi.name).strip("_") or "AOI"
        return settings.imagery_dir / f"{slug}.tif"

    @staticmethod
    def _allometry_path() -> Path:
        return settings.profiles_dir / "allometry.yaml"

    def _to_model(
        self,
        stats: VegetationStats,
        raster_path: Path,
        allometry_source: str,
        *,
        osm_only_trees: int = 0,
        osm_green_area_m2: float = 0.0,
        imagery_source: str = "DOP20 (lokal)",
    ) -> VegetationResult:
        retrieved_at = datetime.now(timezone.utc)
        deepness_source = SourceRef(
            label=f"DEEPNESS ONNX tree-crown segmentation auf {imagery_source}",
            provenance=Provenance.PROVIDED,
            citation=(
                f"model={settings.deepness_onnx_path.name}; "
                f"raster={raster_path.name}; "
                f"osm_augmented_trees={osm_only_trees}"
            ),
            retrieved_at=retrieved_at,
        )
        allometry_source_ref = SourceRef(
            label=allometry_source,
            provenance=Provenance.ASSUMPTION,
            citation="data/profiles/allometry.yaml; Stadtbaum-Allometrie (Gruppenmittel)",
            retrieved_at=retrieved_at,
        )
        return VegetationResult(
            canopy_area_m2=Value(
                value=stats.total_canopy_area_m2,
                unit="m2",
                source=deepness_source,
            ),
            estimated_trees=stats.n_trees,
            co2_stock_kg=Value(
                value=stats.co2_stock_kg,
                unit="kg",
                uncertainty=stats.co2_stock_sigma_kg,
                source=allometry_source_ref,
            ),
            osm_only_trees=osm_only_trees,
            osm_green_area_m2=osm_green_area_m2,
            imagery_source=imagery_source,
        )

    def _write_tree_log(
        self,
        trees: list[Tree],
        stats: VegetationStats,
        raster_path: Path,
        allometry_source: str,
    ) -> None:
        out = self.run_dir / "results" / "vegetation_trees.json"
        out.write_text(
            json.dumps(
                {
                    "raster": str(raster_path),
                    "ground_resolution_m2_per_pixel": stats.raster_pixel_area_m2,
                    "allometry_source": allometry_source,
                    "stem_foot_rule": "centroid in AOI (spec §8)",
                    "summary": {
                        "n_trees": stats.n_trees,
                        "n_flagged_oversize": stats.n_flagged,
                        "total_canopy_area_m2": stats.total_canopy_area_m2,
                        "mean_crown_area_m2": stats.mean_crown_area_m2,
                        "co2_stock_kg": stats.co2_stock_kg,
                        "co2_stock_sigma_kg": stats.co2_stock_sigma_kg,
                    },
                    "trees": [
                        {
                            "x_m": round(t.centroid_xy_metric[0], 2),
                            "y_m": round(t.centroid_xy_metric[1], 2),
                            "crown_area_m2": round(t.crown_area_m2, 1),
                            "crown_diameter_m": round(t.crown_diameter_m, 2),
                            "flagged_oversize": t.flagged_oversize,
                        }
                        for t in trees
                    ],
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self._log_step("per-tree dump written", path=str(out))
