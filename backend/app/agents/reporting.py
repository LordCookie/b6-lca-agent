"""Reporting agent (spec §11).

Writes the output contract:
  - results/b6_results.json / .csv  (machine-readable, heat/electricity split)
  - results/report.md               (every step, source, pedigree, assumption,
                                     vegetation, reproducibility note)
  - sources/sources.md              (factors + identifiers)
  - results/manifest.json           (image tag, package versions, seeds, hashes)
"""
from __future__ import annotations

import json
import platform
import sys
from datetime import datetime, timezone
from typing import Any

import json as _json
import logging
from pydantic import ValidationError

from ..config import settings
from ..models import (
    ClusterResult,
    ReportingAdvice,
)
from .base import Agent

log = logging.getLogger("lca.agent.reporting")


def _strip_codeblock_wrapper(text: str) -> str:
    """Strip ```json ... ``` (or plain ```) fences some LLMs wrap JSON in,
    even when response_format=json_object is set."""
    if not text:
        return ""
    s = text.strip()
    if s.startswith("```"):
        # remove opening fence (with optional language tag)
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        # remove trailing fence
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3].rstrip()
    return s.strip()


class ReportingAgent(Agent):
    name = "reporting"

    def run(self, context: dict[str, Any]) -> dict[str, Any]:
        cluster: ClusterResult = context["cluster"]
        results_dir = self.run_dir / "results"
        sources_dir = self.run_dir / "sources"

        # KI-Empfehlungen aus den deterministisch berechneten Werten.
        # Wird vor dem Schreiben des Reports generiert, damit es im Markdown landet.
        if settings.reporting_llm_enabled and self.llm is not None:
            advice = self._generate_advice(cluster, context)
            if advice is not None:
                context["reporting_advice"] = advice
                # Persist standalone für Frontend / weitere Auswertung
                (results_dir / "advice.json").write_text(
                    advice.model_dump_json(indent=2), encoding="utf-8"
                )

        # Charts als PNG-Dateien generieren, damit sie im PDF eingebettet werden können.
        try:
            chart_paths = self._render_charts(cluster, results_dir)
            context["chart_paths"] = chart_paths
        except Exception as exc:
            log.warning("chart rendering failed: %s", exc)

        # 1) machine-readable
        (results_dir / "b6_results.json").write_text(
            cluster.model_dump_json(indent=2), encoding="utf-8"
        )
        self._write_csv(cluster, results_dir / "b6_results.csv")

        # 2) human-readable report
        (results_dir / "report.md").write_text(
            self._render_report(context), encoding="utf-8"
        )

        # 3) sources index
        (sources_dir / "sources.md").write_text(
            self._render_sources(cluster), encoding="utf-8"
        )

        # 4) reproducibility manifest
        (results_dir / "manifest.json").write_text(
            json.dumps(self._manifest(context), indent=2), encoding="utf-8"
        )
        return {}

    @staticmethod
    def _write_csv(cluster: ClusterResult, path) -> None:
        import csv

        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(
                [
                    "osm_id",
                    "name",
                    "end_energy_heat_kwh",
                    "end_energy_electricity_kwh",
                    "penrt_kwh",
                    "gwp_kg_co2eq",
                    "gwp_per_ngf_kg_co2eq_per_m2a",
                ]
            )
            for br in cluster.buildings:
                e = br.energy
                w.writerow(
                    [
                        br.building.osm_id,
                        br.building.name or "",
                        e.end_energy_heat_kwh.value,
                        e.end_energy_electricity_kwh.value,
                        e.penrt_kwh.value,
                        e.gwp_kg_co2eq.value,
                        e.gwp_per_ngf_kg_co2eq_per_m2a.value,
                    ]
                )

    @staticmethod
    def _render_report(context: dict[str, Any]) -> str:
        from ..config import settings

        cluster: ClusterResult = context["cluster"]
        lines = [
            "# Cluster-Bilanzierung B6 — Betriebsenergie",
            f"Erstellt: {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M UTC')}",
            f"Bezugsjahr: **{settings.reference_year}** · "
            f"ÖKOBAUDAT-Version: {settings.oekobaudat_db_version}",
            "",
            "## Bilanzgrenze",
            "- Modul **B6** (Betriebsenergie) bilanziert nach DIN EN 15978.",
            "- Module A1–A5, B1–B5, C1–C4: nicht bilanziert (MND).",
            "- ODP, AP, EP, POCP, ADP-Elemente/Brennstoffe: nicht bewertet (INA).",
            "",
            "## Cluster-Ergebnis",
            f"- Endenergie Wärme: {cluster.cluster_energy.end_energy_heat_kwh.value:.0f} kWh/a",
            f"- Endenergie Strom: {cluster.cluster_energy.end_energy_electricity_kwh.value:.0f} kWh/a",
            f"- PENRT:           {cluster.cluster_energy.penrt_kwh.value:.0f} kWh/a",
            f"- GWP:             {cluster.cluster_energy.gwp_kg_co2eq.value:.0f} kg CO₂-äq./a",
            f"- GWP / NGF:       {cluster.cluster_energy.gwp_per_ngf_kg_co2eq_per_m2a.value:.2f} kg/m²a",
        ]

        advice = context.get("reporting_advice")
        if advice is not None:
            lines += ReportingAgent._render_advice_section(advice)

        # Charts als PNG einbetten (relative Pfade, weasyprint löst sie beim
        # PDF-Rendering auf weil base_url = report.md-Verzeichnis ist).
        chart_paths = context.get("chart_paths") or {}
        if chart_paths:
            lines += ["", "## Diagramme", ""]
            if "energy" in chart_paths:
                lines.append("**Endenergie pro Gebäude (Wärme + Strom)**")
                lines.append("")
                lines.append(f"![Endenergie pro Gebäude]({chart_paths['energy']})")
                lines.append("")
            if "gwp" in chart_paths:
                lines.append("**Treibhausgaspotential pro Gebäude**")
                lines.append("")
                lines.append(f"![GWP pro Gebäude]({chart_paths['gwp']})")
                lines.append("")

        if cluster.vegetation:
            lines += [
                "",
                "## Grünraum — CO₂-Speicherung",
                f"- Kronenfläche: {cluster.vegetation.canopy_area_m2.value:.0f} m²",
                f"- Geschätzte Bäume: {cluster.vegetation.estimated_trees}",
                f"- CO₂-Speicher (Stock): {cluster.vegetation.co2_stock_kg.value:.0f} kg",
                "",
                "*Hinweis: Der Grünraum-CO₂-Speicher ist getrennt von der Betriebsenergie-Bilanz und wird nicht eingerechnet.*",
            ]
        elif context.get("vegetation_skipped"):
            lines += [
                "",
                "## Grünraum — CO₂-Speicherung",
                f"- Schritt übersprungen: {context.get('vegetation_skip_reason', 'kein Grund angegeben')}",
            ]

        # Spec §11: Pedigree-Bewertung gehört in den Report (nicht nur in
        # sources/pedigree.json).
        pedigree = context.get("pedigree") or {}
        if pedigree:
            lines += ReportingAgent._render_pedigree_section(pedigree)

        llm_usage = context.get("llm_usage")
        if llm_usage:
            lines += ReportingAgent._render_llm_usage_section(llm_usage)

        flagged = context.get("flagged") or []
        if flagged:
            lines += ["", "## Markierte Datenpunkte zur Prüfung"]
            for f in flagged:
                osm_id = f.get("osm_id", "?")
                field = f.get("field", "?")
                issue = f.get("issue", "")
                lines.append(f"- **{osm_id}** / `{field}`: {issue}")
                erklaerung = f.get("llm_erklaerung")
                if erklaerung:
                    lines.append(f"  - *Erklärung:* {erklaerung}")
                aktion = f.get("llm_aktion")
                if aktion:
                    lines.append(f"  - *Empfehlung:* {aktion}")
        return "\n".join(lines) + "\n"

    def _render_charts(
        self, cluster: ClusterResult, results_dir,
    ) -> dict[str, str]:
        """Generate stacked-bar and GWP-error-bar charts as PNGs next to report.md.

        Returns a dict {chart_key: filename} with paths *relative* to results_dir,
        so the Markdown links resolve when weasyprint renders the PDF.
        """
        # Matplotlib headless import — keep inside method to avoid startup cost.
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        kept = [b for b in cluster.buildings if not b.building.excluded]
        if not kept:
            return {}

        def _label(b):
            return b.building.name or b.building.osm_id.split("/")[-1]

        labels = [_label(b) for b in kept]
        heat = [b.energy.end_energy_heat_kwh.value / 1000 for b in kept]  # MWh
        elec = [b.energy.end_energy_electricity_kwh.value / 1000 for b in kept]
        gwp = [b.energy.gwp_kg_co2eq.value / 1000 for b in kept]          # t
        gwp_sigma = [
            (b.energy.gwp_kg_co2eq.uncertainty or 0) / 1000 for b in kept
        ]

        # --- Endenergie pro Gebäude (stacked) ---
        fig, ax = plt.subplots(figsize=(8, 4.2))
        x = list(range(len(labels)))
        ax.bar(x, heat, label="Wärme", color="#dc2626")
        ax.bar(x, elec, bottom=heat, label="Strom", color="#2563eb")
        ax.set_xticks(x)
        ax.set_xticklabels(labels, rotation=20, ha="right", fontsize=8)
        ax.set_ylabel("MWh / Jahr")
        ax.set_title("Endenergiebedarf pro Gebäude")
        ax.legend(loc="upper right", fontsize=9)
        ax.grid(axis="y", linestyle="--", alpha=0.4)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        fig.tight_layout()
        energy_path = results_dir / "chart_energy.png"
        fig.savefig(energy_path, dpi=130)
        plt.close(fig)

        # --- GWP pro Gebäude mit σ-Fehlerbalken ---
        fig, ax = plt.subplots(figsize=(8, 4.2))
        ax.bar(x, gwp, yerr=gwp_sigma, capsize=4, color="#1f2937",
               error_kw={"ecolor": "#6b7280", "linewidth": 1.2})
        ax.set_xticks(x)
        ax.set_xticklabels(labels, rotation=20, ha="right", fontsize=8)
        ax.set_ylabel("Tonnen CO₂-äq. / Jahr")
        ax.set_title("Treibhausgaspotential pro Gebäude (mit Unsicherheits-Band)")
        ax.grid(axis="y", linestyle="--", alpha=0.4)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        fig.tight_layout()
        gwp_path = results_dir / "chart_gwp.png"
        fig.savefig(gwp_path, dpi=130)
        plt.close(fig)

        return {
            "energy": energy_path.name,
            "gwp": gwp_path.name,
        }

    _ADVICE_SYSTEM_PROMPT = (
        "Du bist ein technischer Berater für Gebäude-Energiebilanzen und Sanierungs-"
        "priorisierung. Eingangsdaten sind eine bereits durchgerechnete B6-Cluster-"
        "Bilanz (DIN EN 15978, DIN V 18599-2). Deine Aufgabe: interpretiere die "
        "Zahlen für einen Laien und gib priorisierte Sanierungs-Empfehlungen.\n\n"
        "STRIKTE REGELN:\n"
        "- Du erfindest KEINE Zahlen — nutze nur die übergebenen Werte.\n"
        "- Hotspots = Gebäude mit überproportionalem GWP/NGF, GWP-Beitrag oder "
        "  spezifischem Strom/Wärmebedarf gegenüber dem Cluster-Mittel.\n"
        "- Sanierungs-Empfehlungen sollen sich an üblichen Maßnahmen orientieren "
        "  (Hülle, Anlagentechnik, Nutzerverhalten); für Standards nutze das "
        "  web_search-Tool (KfW, BAFA, GEG, TABULA).\n"
        "- Auffällige Befunde = Inkonsistenzen, OSM-Anomalien, Daten-Ausreißer.\n\n"
        "Antworte ausschließlich als JSON nach diesem Schema:\n"
        '{"executive_summary": string, "cluster_assessment": string, '
        '"hotspots": [{"osm_id": string, "name": string|null, "prioritaet": '
        '"hoch"|"mittel"|"niedrig", "begruendung": string, '
        '"massnahmen": [{"massnahme": string, "erwartete_einsparung": string|null, '
        '"investitions_kategorie": "klein"|"mittel"|"gro\\u00df"|null}]}], '
        '"auffaellige_befunde": [{"kennzahl": string, "befund": string, '
        '"vermutete_ursache": string|null, "empfohlene_pruefung": string|null}]}'
    )

    def _generate_advice(
        self, cluster: ClusterResult, context: dict[str, Any],
    ) -> ReportingAdvice | None:
        """Run a single Sonnet call with web-search to derive recommendations."""
        # Compact summary of all numeric inputs the LLM is allowed to reason over.
        cluster_e = cluster.cluster_energy
        per_building = []
        total_ngf = 0.0
        for br in cluster.buildings:
            if br.building.excluded:
                continue
            ngf = br.building.ngf_m2.value if br.building.ngf_m2 else 0.0
            total_ngf += ngf
            per_building.append({
                "osm_id": br.building.osm_id,
                "name": br.building.name,
                "ngf_m2": round(ngf, 0),
                "build_year": br.building.build_year,
                "use_mix": br.building.use_mix,
                "heat_kwh": round(br.energy.end_energy_heat_kwh.value, 0),
                "electricity_kwh": round(br.energy.end_energy_electricity_kwh.value, 0),
                "gwp_kg_co2eq": round(br.energy.gwp_kg_co2eq.value, 0),
                "gwp_per_ngf": round(br.energy.gwp_per_ngf_kg_co2eq_per_m2a.value, 2),
            })
        payload = {
            "cluster": {
                "ngf_m2": round(total_ngf, 0),
                "heat_kwh_a": round(cluster_e.end_energy_heat_kwh.value, 0),
                "electricity_kwh_a": round(cluster_e.end_energy_electricity_kwh.value, 0),
                "penrt_kwh_a": round(cluster_e.penrt_kwh.value, 0),
                "gwp_kg_co2eq_a": round(cluster_e.gwp_kg_co2eq.value, 0),
                "gwp_per_ngf": round(cluster_e.gwp_per_ngf_kg_co2eq_per_m2a.value, 2),
                "benchmark_gwp_per_ngf": 40.0,  # Literatur-Mittel Hochschulgebäude (Loga et al. 2016)
            },
            "buildings": per_building,
            "flagged_outliers": context.get("flagged", []),
        }

        messages = [
            {"role": "system", "content": self._ADVICE_SYSTEM_PROMPT},
            {"role": "user", "content": (
                "B6-Cluster-Bilanz als JSON:\n```json\n"
                + _json.dumps(payload, ensure_ascii=False, indent=2)
                + "\n```\n\n"
                "Erstelle Executive Summary, identifiziere Hotspots (priorisiert "
                "nach GWP/NGF und absolutem GWP-Beitrag), schlage Sanierungs-"
                "Maßnahmen vor und nenne auffällige Befunde. Antworte nur als JSON."
            )},
        ]

        def _try_call(with_search: bool) -> str | None:
            try:
                resp = self.llm.chat(
                    agent="reporting",
                    messages=messages,
                    enable_web_search=with_search,
                    web_search_max_results=3,
                    temperature=0.1,
                    response_format={"type": "json_object"},
                )
            except Exception as exc:
                log.warning("reporting LLM call (web_search=%s) failed: %s",
                            with_search, exc)
                return None
            return resp.get("content")

        content = _try_call(with_search=settings.reporting_llm_use_websearch)
        if not content:
            log.warning("reporting LLM: empty content, retrying without web_search")
            content = _try_call(with_search=False)
        if not content:
            log.warning("reporting LLM: still empty content after retry — skipping advice")
            return None
        cleaned = _strip_codeblock_wrapper(content)
        try:
            data = _json.loads(cleaned)
            return ReportingAdvice.model_validate(data)
        except (_json.JSONDecodeError, ValidationError) as exc:
            log.warning(
                "reporting LLM output failed validation: %s -- raw[:200]=%r",
                exc, (content or "")[:200],
            )
            return None

    @staticmethod
    def _render_advice_section(advice: ReportingAdvice) -> list[str]:
        """Markdown-Sektion für die KI-Empfehlungen."""
        out = [
            "",
            "## KI-Empfehlungen (Interpretation der Bilanz)",
            "",
            "*Hinweis: Die folgenden Empfehlungen sind eine KI-gestützte Interpretation "
            "der berechneten Werte und ersetzen keine Vor-Ort-Begehung oder "
            "ingenieurmäßige Detailplanung.*",
            "",
            "### Executive Summary",
            "",
            advice.executive_summary,
            "",
            "### Cluster-Bewertung",
            "",
            advice.cluster_assessment,
        ]
        if advice.hotspots:
            out += ["", "### Sanierungs-Priorisierung (Hotspots)", ""]
            for i, h in enumerate(advice.hotspots, 1):
                label = f"{h.name} ({h.osm_id})" if h.name else h.osm_id
                out.append(f"**Priorität {i} — {label}** *(Priorität: {h.prioritaet})*")
                out.append("")
                out.append(h.begruendung)
                if h.massnahmen:
                    out.append("")
                    out.append("Empfohlene Maßnahmen:")
                    for m in h.massnahmen:
                        bullet = f"- {m.massnahme}"
                        if m.erwartete_einsparung:
                            bullet += f" *(Einsparung: {m.erwartete_einsparung})*"
                        if m.investitions_kategorie:
                            bullet += f" *(Invest: {m.investitions_kategorie})*"
                        out.append(bullet)
                out.append("")
        if advice.auffaellige_befunde:
            out += ["", "### Auffällige Befunde", ""]
            for b in advice.auffaellige_befunde:
                out.append(f"**{b.kennzahl}:** {b.befund}")
                if b.vermutete_ursache:
                    out.append(f"  - Vermutete Ursache: {b.vermutete_ursache}")
                if b.empfohlene_pruefung:
                    out.append(f"  - Empfohlene Prüfung: {b.empfohlene_pruefung}")
                out.append("")
        return out

    @staticmethod
    def _render_llm_usage_section(usage: dict[str, Any]) -> list[str]:
        """Per-agent LLM token + cost summary."""
        out = [
            "",
            "## KI-Nutzung & Kosten",
            "",
        ]
        pricing = usage.get("pricing_source") or {}
        if pricing:
            out.append(
                f"Pricing-Quelle: {pricing.get('source', 'n/a')} "
                f"(geprüft {pricing.get('checked_on', 'n/a')})"
            )
            out.append("")
        out += [
            "| Agent | Calls | Prompt-Tokens | Completion-Tokens | Web-Searches | Kosten (USD) |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        per_agent = usage.get("per_agent") or {}
        for agent, row in per_agent.items():
            out.append(
                f"| {agent} | {row['n_calls']} | {row['prompt_tokens']} | "
                f"{row['completion_tokens']} | {row.get('n_web_searches', 0)} | "
                f"{row['cost_usd']:.4f} |"
            )
        out.append(
            f"| **Total** | – | {usage.get('total_tokens', 0)} Tokens | – | "
            f"{usage.get('total_web_searches', 0)} | "
            f"**{usage.get('total_cost_usd', 0.0):.4f}** |"
        )
        return out

    @staticmethod
    def _render_pedigree_section(
        pedigree: dict[str, dict[str, int | str]],
    ) -> list[str]:
        """Markdown table — Datenqualitäts-Matrix per source class."""
        out = [
            "",
            "## Datenqualitäts-Matrix",
            "",
            "Bewertung jeder Datenquelle auf einer Skala 1 (sehr gut) bis 5 (schwach) "
            "in fünf Dimensionen: Zuverlässigkeit (R), Vollständigkeit (C), zeitliche (T), "
            "geografische (G) und technologische (Tech) Korrelation.",
            "",
            "| Quelle | Klasse | R | C | T | G | Tech |",
            "|---|---|:-:|:-:|:-:|:-:|:-:|",
        ]
        for label, row in pedigree.items():
            source_class = row.get("source_class", "?")
            out.append(
                f"| {label} | {source_class} | "
                f"{row.get('reliability', '?')} | "
                f"{row.get('completeness', '?')} | "
                f"{row.get('temporal_correlation', '?')} | "
                f"{row.get('geographical_correlation', '?')} | "
                f"{row.get('technological_correlation', '?')} |"
            )
        return out

    @staticmethod
    def _render_sources(cluster: ClusterResult) -> str:
        seen: dict[str, str] = {}
        for br in cluster.buildings:
            for v in (
                br.building.footprint_area_m2,
                br.energy.end_energy_heat_kwh,
                br.energy.end_energy_electricity_kwh,
                br.energy.gwp_kg_co2eq,
            ):
                if v is None:
                    continue
                key = v.source.label
                detail = []
                if v.source.uuid:
                    detail.append(f"UUID={v.source.uuid}")
                if v.source.citation:
                    detail.append(v.source.citation)
                if v.source.retrieved_at:
                    detail.append(f"retrieved={v.source.retrieved_at.isoformat()}")
                seen[key] = "; ".join(detail)
        lines = ["# Quellen", ""]
        for label, detail in seen.items():
            lines.append(f"- **{label}** — {detail or '(no identifier)'}")
        return "\n".join(lines) + "\n"

    def _manifest(self, context: dict[str, Any]) -> dict[str, Any]:
        from ..config import settings

        return {
            "run_id": self.run_dir.name,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "python": sys.version,
            "platform": platform.platform(),
            "seed": context.get("seed", 42),
            "reference_year": settings.reference_year,
            "oekobaudat_db_version": settings.oekobaudat_db_version,
            "input_hashes": context.get("input_hashes", {}),
            "llm_usage": context.get("llm_usage", {}),  # filled by LLM router
            "image_tag": context.get("image_tag", "lca-backend:dev"),
        }
