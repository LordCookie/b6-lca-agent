"""Material agent (spec §7).

Resolves GWP / emission factors per energy carrier (Modul B6).

Hierarchy enforced strictly (spec §5, §10):
  1. central Excel lookup table (e.g. SWO Fernwärmefaktor AGFW FW-309)
  2. documented sensitivity default for Fernwärme (until SWO publishes Fnet)
  3. local ÖKOBAUDAT 2024-I snapshot (UUID-pinned via data/oekobaudat/carrier_map.yaml)
  4. STOP — never fabricate (AgentError)

Provenance for every emitted Value:
  - lookup table   -> Provenance.PROVIDED (label + sheet ref)
  - SWO default    -> Provenance.ASSUMPTION (clearly labelled sensitivity)
  - ÖKOBAUDAT row  -> Provenance.PROVIDED (UUID + version + snapshot date)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import json
import yaml
from pydantic import ValidationError

from ..config import settings
from ..data.oekobaudat_loader import (
    OekobaudatRecord,
    OekobaudatSnapshot,
    load_snapshot,
)
from ..models import (
    MaterialCarrierSuggestion,
    Provenance,
    SourceRef,
    Value,
)
from .base import Agent, AgentError

log = logging.getLogger("lca.agent.material")

# Documented sensitivity default for Fernwärme (AGFW FW-309 fossile KWK,
# spec §5). Only used for the SWO carrier and only when the central lookup
# table has no row.
_SWO_FERNWAERME_DEFAULT_KG_CO2EQ_PER_KWH = 0.27
_SWO_CARRIER_KEY = "heat_fernwaerme_swo"


class MaterialAgent(Agent):
    name = "material"

    def __init__(self, llm, run_dir: Path) -> None:
        super().__init__(llm, run_dir)
        self._carrier_map: dict[str, dict[str, Any]] | None = None
        self._snapshot: OekobaudatSnapshot | None = None

    def run(self, context: dict[str, Any]) -> dict[str, Any]:
        carriers = context.get("energy_carriers") or [
            _SWO_CARRIER_KEY,
            "electricity_grid",
        ]
        factors: dict[str, Value] = {}
        for carrier in carriers:
            factors[carrier] = self._resolve_factor(carrier)
        self._log_step(
            "GWP factors resolved",
            carriers=list(factors.keys()),
            provenances={k: v.source.provenance for k, v in factors.items()},
        )
        return {"gwp_factors": factors}

    # --- resolution chain ----------------------------------------------------

    def _resolve_factor(self, carrier: str) -> Value:
        v = self._lookup_central(carrier)
        if v is not None:
            return v
        if carrier == _SWO_CARRIER_KEY:
            return self._swo_default()
        v = self._lookup_oekobaudat(carrier)
        if v is not None:
            return v
        raise AgentError(
            f"material: no source for '{carrier}' in central lookup or ÖKOBAUDAT "
            f"snapshot — STOP (spec §10). Pin a UUID in {settings.carrier_map_path.name} "
            "or add a row to the lookup table."
        )

    def _lookup_central(self, carrier: str) -> Value | None:
        # Central Excel lookup is primary per spec §5 but the user has not
        # supplied one yet. When present, this function must:
        #   - open settings.lookup_table_path with openpyxl
        #   - resolve the `carrier` key
        #   - return a Value(provenance=PROVIDED) with sheet/cell citation
        # TODO: implement once the central lookup table is provided.
        if not settings.lookup_table_path.is_file():
            return None
        log.warning(
            "central lookup table exists at %s but reader not implemented yet; "
            "falling through to ÖKOBAUDAT",
            settings.lookup_table_path,
        )
        return None

    def _swo_default(self) -> Value:
        log.info(
            "using documented Fernwärme sensitivity default %.3f kg CO2eq/kWh "
            "(SWO Fnet not yet in lookup table)",
            _SWO_FERNWAERME_DEFAULT_KG_CO2EQ_PER_KWH,
        )
        return Value(
            value=_SWO_FERNWAERME_DEFAULT_KG_CO2EQ_PER_KWH,
            unit="kg_CO2eq/kWh",
            source=SourceRef(
                label="Default fossile KWK-Fernwärme (AGFW FW-309)",
                provenance=Provenance.ASSUMPTION,
                citation=(
                    "GEG 2024 Anlage 4; sensitivity until SWO publishes "
                    "network-specific Fnet"
                ),
            ),
        )

    def _lookup_oekobaudat(self, carrier: str) -> Value | None:
        carrier_map = self._load_carrier_map()
        entry = carrier_map.get(carrier)
        snapshot = self._load_oekobaudat()

        if entry is None:
            # Spec §1+§13: LLM-Vorschlag als Fallback. Strikte UUID-Validation.
            if settings.material_llm_enabled and self.llm is not None:
                suggestion = self._llm_suggest_uuid(carrier, snapshot)
                if suggestion and suggestion.suggested_uuid:
                    entry = {
                        "oekobaudat_uuid": suggestion.suggested_uuid,
                        "oekobaudat_module": suggestion.modul,
                        "display_name": (suggestion.suggested_name or
                                          f"LLM-Vorschlag: {suggestion.suggested_uuid}"),
                        "note": suggestion.reasoning,
                        "_provenance": "llm_suggested",
                    }
                    log.info("LLM suggested UUID for %s: %s", carrier,
                             suggestion.suggested_uuid)
            if entry is None:
                log.warning("no carrier_map entry for %r — cannot resolve via ÖKOBAUDAT",
                            carrier)
                return None

        uuid = entry["oekobaudat_uuid"]
        modul = entry.get("oekobaudat_module", "B6")
        display_name = entry.get("display_name") or f"ÖKOBAUDAT {uuid} ({modul})"

        try:
            rec: OekobaudatRecord = snapshot.get(uuid, modul)
        except KeyError as exc:
            raise AgentError(str(exc)) from exc

        try:
            factor = rec.to_factor_kg_co2eq_per_kwh()
        except ValueError as exc:
            raise AgentError(f"material: {exc}") from exc

        retrieved_at = (
            datetime.combine(snapshot.snapshot_date, datetime.min.time(), tzinfo=timezone.utc)
            if snapshot.snapshot_date
            else None
        )
        return Value(
            value=factor,
            unit="kg_CO2eq/kWh",
            source=SourceRef(
                label=display_name,
                provenance=Provenance.PROVIDED,
                uuid=uuid,
                citation=(
                    f"ÖKOBAUDAT {snapshot.version_label or ''} | Modul {modul} | "
                    f"{rec.name_de} ({rec.konformitaet})"
                ).strip(),
                retrieved_at=retrieved_at,
                version=f"OBD {snapshot.version_label or '?'} / dataset v{rec.version}",
            ),
        )

    # --- caching loaders -----------------------------------------------------

    def _load_carrier_map(self) -> dict[str, dict[str, Any]]:
        if self._carrier_map is None:
            with settings.carrier_map_path.open("r", encoding="utf-8") as f:
                self._carrier_map = yaml.safe_load(f) or {}
        return self._carrier_map

    def _load_oekobaudat(self) -> OekobaudatSnapshot:
        if self._snapshot is None:
            self._snapshot = load_snapshot(settings.oekobaudat_csv_path)
            self._log_step(
                "ÖKOBAUDAT snapshot loaded",
                path=str(self._snapshot.path),
                rows=self._snapshot.n_rows,
                snapshot_date=str(self._snapshot.snapshot_date),
                version=self._snapshot.version_label,
            )
        return self._snapshot

    # ------------------------------------------------------------------
    # LLM-Vorschlag für UUID wenn carrier_map.yaml unvollständig (spec §1+§13)
    # ------------------------------------------------------------------

    _UUID_SYSTEM_PROMPT = (
        "Du bist ein ÖKOBAUDAT-Experte. Eingangsdaten: ein interner Energieträger-"
        "Schlüssel (z.B. 'heat_fernwaerme_swo' oder 'electricity_grid') und eine "
        "vorgeschlagene Liste von ÖKOBAUDAT-Datensätzen (Modul B6) als JSON.\n\n"
        "Deine Aufgabe: wähle den passendsten ÖKOBAUDAT-Datensatz aus und "
        "begründe kurz warum. Wenn kein Datensatz wirklich passt: confidence='no_good_match'.\n\n"
        "STRIKTE REGELN:\n"
        "- UUID MUSS aus der übergebenen Kandidatenliste stammen (keine erfundenen UUIDs).\n"
        "- Antworte ausschließlich als JSON nach diesem Schema:\n"
        '{"carrier_key": string, "suggested_uuid": string|null, '
        '"suggested_name": string|null, "modul": "B6", "reasoning": string, '
        '"confidence": "found"|"no_good_match"}'
    )

    def _llm_suggest_uuid(
        self, carrier_key: str, snapshot: OekobaudatSnapshot,
    ) -> MaterialCarrierSuggestion | None:
        if self.llm is None:
            return None
        # Kandidaten: alle Modul-B6-Datensätze. Auf max 30 Einträge gekürzt damit
        # der Prompt klein bleibt — vorgefiltert nach Schlüsselwörtern aus carrier_key.
        keywords = carrier_key.lower().replace("_", " ").split()
        cands = []
        for (uuid, modul), rec in snapshot.by_uuid_module.items():
            if modul != "B6":
                continue
            name_l = rec.name_de.lower()
            if any(k in name_l for k in keywords) or any(
                # Heuristic: 'strom' → also include 'electricity', 'fernwärme', 'gas' etc.
                k in name_l for k in ("strom", "elektr", "fernwärme", "fernwaerme",
                                      "erdgas", "pellet", "öl", "oel", "biomasse",
                                      "biogas", "kohle")
            ):
                cands.append({
                    "uuid": uuid,
                    "name_de": rec.name_de,
                    "factor_kg_co2eq_per_kwh": (
                        round(rec.gwp_total_kg_co2eq / (rec.bezug_value / 3.6 if rec.bezug_unit == "MJ" else rec.bezug_value), 4)
                        if rec.gwp_total_kg_co2eq is not None else None
                    ),
                    "conformity": rec.konformitaet,
                })
            if len(cands) >= 30:
                break
        if not cands:
            return None

        try:
            resp = self.llm.chat(
                agent="material",
                messages=[
                    {"role": "system", "content": self._UUID_SYSTEM_PROMPT},
                    {"role": "user", "content": (
                        f"Energieträger-Schlüssel: {carrier_key!r}\n\n"
                        f"Kandidaten (max. 30 vorgefiltert):\n```json\n"
                        + json.dumps(cands, ensure_ascii=False, indent=2)
                        + "\n```\n\nWähle den passendsten Datensatz."
                    )},
                ],
                temperature=0.0,
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            log.warning("material LLM call failed for %s: %s", carrier_key, exc)
            return None

        content = resp.get("content")
        if not content:
            return None
        try:
            data = json.loads(content)
            suggestion = MaterialCarrierSuggestion.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as exc:
            log.warning("material LLM output failed validation: %s", exc)
            return None

        # Strikte Validierung: UUID muss in Kandidaten gewesen sein
        valid_uuids = {c["uuid"] for c in cands}
        if suggestion.suggested_uuid and suggestion.suggested_uuid not in valid_uuids:
            log.warning("LLM suggested UUID %s not in candidate set — rejecting",
                        suggestion.suggested_uuid)
            return None
        return suggestion
