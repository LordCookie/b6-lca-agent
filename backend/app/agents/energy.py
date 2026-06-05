"""Energy agent (spec §4 + §6).

Quasi-stationary monthly balance per DIN V 18599-2 (simplified — see
backend/app/engine/balance.py docstring).

Workflow per building:
  1. resolve envelope class (by build year — research agent later sharpens this)
  2. estimate envelope areas from footprint/storeys/height (engine.envelope)
  3. weighted-average the usage profile by `use_mix` (e.g. MR: 70% UNIVERSITY + 30% RESTAURANT)
  4. monthly heating-demand balance (engine.balance)
  5. system-loss division -> end-energy heat
  6. profile-based electricity demand -> end-energy electricity
  7. PEF (GEG Anlage 4) -> PENRT
  8. material agent GWP factors -> GWP
  9. per-building Value with gauss-propagated uncertainty band

Strict per spec §4:
  - heat and electricity reported separately,
  - exported energy NOT netted (any credits go to Modul D, not B6).
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from typing import Any

from ..engine.balance import (
    BalanceResult,
    electricity_demand_kWh_a,
    monthly_heating_demand,
)
from ..engine.climate import ClimateDataset, load_climate
from ..engine.envelope import EnvelopeAreas, estimate_envelope
from ..engine.profiles import (
    EnvelopeDefaults,
    UsageProfile,
    load_envelope_defaults,
    load_system_efficiency,
    load_usage_profiles,
    pick_envelope_class,
)
from ..engine.uncertainty import absolute_band, combine_relative
from ..models import (
    Building,
    BuildingResult,
    EnergyResult,
    Provenance,
    SourceRef,
    Value,
)
from .base import Agent, AgentError

log = logging.getLogger("lca.agent.energy")

# GEG 2024 Anlage 4 — applied identically to all methods (spec §5).
F_PNR_ELECTRICITY = 1.8
F_PNR_FERNWAERME_DEFAULT = 0.7  # documented sensitivity until SWO publishes Fnet

# Default carrier mapping per spec §5 (Jade HS = Fernwärme + Strom).
_DEFAULT_HEAT_CARRIER = "heat_fernwaerme_swo"
_DEFAULT_ELEC_CARRIER = "electricity_grid"
_DEFAULT_HEAT_SYSTEM_KEY = "fernwaerme"
_DEFAULT_ELEC_SYSTEM_KEY = "electricity_direct"


class EnergyAgent(Agent):
    name = "energy"

    def run(self, context: dict[str, Any]) -> dict[str, Any]:
        buildings: list[Building] = context.get("buildings", [])
        gwp_factors: dict[str, Value] = context.get("gwp_factors", {})
        if not buildings:
            raise AgentError("energy: no buildings in context")
        for required in (_DEFAULT_HEAT_CARRIER, _DEFAULT_ELEC_CARRIER):
            if required not in gwp_factors:
                raise AgentError(
                    f"energy: GWP factor for '{required}' missing — material agent "
                    "must run first (spec §6 chain)"
                )

        climate = load_climate()
        env_classes, default_class = load_envelope_defaults()
        usage_profiles = load_usage_profiles()
        systems = load_system_efficiency()

        heat_sys = systems[_DEFAULT_HEAT_SYSTEM_KEY]
        elec_sys = systems[_DEFAULT_ELEC_SYSTEM_KEY]
        f_p_heat = F_PNR_FERNWAERME_DEFAULT
        f_p_elec = F_PNR_ELECTRICITY
        gwp_heat = gwp_factors[_DEFAULT_HEAT_CARRIER]
        gwp_elec = gwp_factors[_DEFAULT_ELEC_CARRIER]

        building_results: list[BuildingResult] = []
        sum_heat_kwh = sum_elec_kwh = 0.0
        sum_penrt = sum_gwp = sum_ngf = 0.0
        per_building_log: list[dict[str, Any]] = []

        for b in buildings:
            if b.excluded:
                continue
            try:
                envelope_class_key = self._envelope_class_for(b, env_classes, default_class)
                env_defaults = env_classes[envelope_class_key]
                envelope = estimate_envelope(b, env_defaults)
                profile, profile_label = self._effective_profile(b, usage_profiles)

                balance = monthly_heating_demand(envelope, env_defaults, profile, climate)
                ngf = b.ngf_m2.value if b.ngf_m2 else envelope.ngf_m2

                end_heat = balance.Q_h_kWh_a / heat_sys.eta_total
                end_elec = electricity_demand_kWh_a(profile, ngf)

                rel_heat = combine_relative(
                    env_defaults.uncertainty_rel,
                    profile.uncertainty_rel,
                    heat_sys.uncertainty_rel,
                    0.05,  # climate
                )
                rel_elec = combine_relative(profile.uncertainty_rel, elec_sys.uncertainty_rel)

                penrt = end_heat * f_p_heat + end_elec * f_p_elec
                gwp = end_heat * gwp_heat.value + end_elec * gwp_elec.value
                gwp_per_ngf = gwp / ngf if ngf > 0 else 0.0

                rel_penrt = combine_relative(rel_heat, rel_elec, 0.02)  # PEF tiny σ
                rel_gwp = combine_relative(rel_heat, rel_elec, 0.10)    # GWP factor σ

                energy = EnergyResult(
                    end_energy_heat_kwh=self._value(
                        end_heat, "kWh",
                        "DIN V 18599 Monatsbilanz (vereinfacht) → Endenergie Wärme",
                        rel_heat,
                    ),
                    end_energy_electricity_kwh=self._value(
                        end_elec, "kWh",
                        f"Profilwert {profile_label}: q_el × NGF",
                        rel_elec,
                    ),
                    penrt_kwh=self._value(
                        penrt, "kWh",
                        "Endenergie × f_P,nr (GEG 2024 Anlage 4)",
                        rel_penrt,
                    ),
                    gwp_kg_co2eq=self._value(
                        gwp, "kg_CO2eq",
                        f"Endenergie × GWP-Faktor ({gwp_heat.source.label} / {gwp_elec.source.label})",
                        rel_gwp,
                    ),
                    gwp_per_ngf_kg_co2eq_per_m2a=self._value(
                        gwp_per_ngf, "kg_CO2eq/m2a",
                        "GWP / NGF (DIN 277)",
                        rel_gwp,
                    ),
                )
                building_results.append(BuildingResult(building=b, energy=energy))

                sum_heat_kwh += end_heat
                sum_elec_kwh += end_elec
                sum_penrt += penrt
                sum_gwp += gwp
                sum_ngf += ngf

                per_building_log.append(
                    {
                        "osm_id": b.osm_id,
                        "name": b.name,
                        "envelope_class": envelope_class_key,
                        "profile": profile_label,
                        "ngf_m2": round(ngf, 1),
                        "Q_T_kWh_a": round(balance.Q_T_kWh_a, 1),
                        "Q_V_kWh_a": round(balance.Q_V_kWh_a, 1),
                        "Q_S_kWh_a": round(balance.Q_S_kWh_a, 1),
                        "Q_I_kWh_a": round(balance.Q_I_kWh_a, 1),
                        "Q_h_kWh_a": round(balance.Q_h_kWh_a, 1),
                        "end_energy_heat_kwh": round(end_heat, 1),
                        "end_energy_electricity_kwh": round(end_elec, 1),
                        "penrt_kwh": round(penrt, 1),
                        "gwp_kg_co2eq": round(gwp, 1),
                        "gwp_per_ngf_kg_co2eq_per_m2a": round(gwp_per_ngf, 2),
                        "rel_sigma_gwp": round(rel_gwp, 3),
                    }
                )
            except (AgentError, ValueError) as exc:
                # Spec §10 rule 2: missing source = STOP. Re-raise to halt the run
                # rather than silently dropping a building.
                raise AgentError(
                    f"energy: building {b.osm_id} cannot be balanced: {exc}"
                ) from exc

        if not building_results:
            raise AgentError("energy: no buildings produced a result (all excluded?)")

        rel_cluster = combine_relative(0.12, 0.08)  # rough portfolio σ
        cluster_energy = EnergyResult(
            end_energy_heat_kwh=self._value(
                sum_heat_kwh, "kWh", "Cluster sum heat", rel_cluster
            ),
            end_energy_electricity_kwh=self._value(
                sum_elec_kwh, "kWh", "Cluster sum electricity", rel_cluster
            ),
            penrt_kwh=self._value(
                sum_heat_kwh * f_p_heat + sum_elec_kwh * f_p_elec,
                "kWh", "Cluster PENRT", rel_cluster,
            ),
            gwp_kg_co2eq=self._value(
                sum_gwp, "kg_CO2eq", "Cluster GWP", rel_cluster
            ),
            gwp_per_ngf_kg_co2eq_per_m2a=self._value(
                sum_gwp / sum_ngf if sum_ngf > 0 else 0.0,
                "kg_CO2eq/m2a", "Cluster GWP / NGF (DIN 277)", rel_cluster,
            ),
        )

        self._write_per_building_log(per_building_log, climate)
        self._log_step(
            "energy balance complete",
            n_buildings=len(building_results),
            heat_kwh=round(sum_heat_kwh, 1),
            elec_kwh=round(sum_elec_kwh, 1),
            gwp_kg=round(sum_gwp, 1),
        )
        return {
            "building_results": building_results,
            "cluster_energy": cluster_energy,
        }

    # --- helpers ---------------------------------------------------------

    @staticmethod
    def _envelope_class_for(
        building: Building,
        env_classes: dict[str, EnvelopeDefaults],
        default_class: str,
    ) -> str:
        # The research agent fills build_year (web research / heuristic).
        # If still missing, fall back to the configured default class.
        key = pick_envelope_class(building.build_year)
        return key if key in env_classes else default_class

    @staticmethod
    def _effective_profile(
        building: Building,
        usage_profiles: dict[str, UsageProfile],
    ) -> tuple[UsageProfile, str]:
        """Resolve the effective profile, weighted by `use_mix`.

        Spec §5 MR: 70% UNIVERSITY / 30% RESTAURANT — this returns one
        synthetic profile with linearly weighted numeric fields and a
        label that documents the mix in sources. Note: the research agent
        always fills a use_mix (UNIVERSITY default), so this branch should
        not normally trigger.
        """
        if not building.use_mix:
            raise AgentError(
                f"energy: building {building.osm_id} has empty use_mix even after "
                "research agent — check orchestrator order (research must run before energy)"
            )

        total = sum(building.use_mix.values())
        if abs(total - 1.0) > 1e-3:
            raise AgentError(
                f"energy: building {building.osm_id} use_mix sums to {total:.3f}, "
                "expected 1.0"
            )

        def w(field: str) -> float:
            return sum(
                share * getattr(usage_profiles[key], field)
                for key, share in building.use_mix.items()
                if key in usage_profiles
            )

        first_key = next(iter(building.use_mix))
        # Hours/days: weighted average rounded to int (less critical than energy fields).
        synthetic = UsageProfile(
            key="MIX",
            source="weighted mix per building.use_mix",
            theta_i_set_heating_C=w("theta_i_set_heating_C"),
            q_internal_W_per_m2=w("q_internal_W_per_m2"),
            q_electricity_kWh_per_m2a=w("q_electricity_kWh_per_m2a"),
            air_change_rate_1_per_h=w("air_change_rate_1_per_h"),
            operation_hours_per_day=int(round(w("operation_hours_per_day"))),
            operation_days_per_year=int(round(w("operation_days_per_year"))),
            uncertainty_rel=max(
                usage_profiles[k].uncertainty_rel for k in building.use_mix if k in usage_profiles
            ),
        )
        label = " + ".join(
            f"{int(round(share * 100))}% {key}" for key, share in building.use_mix.items()
        )
        _ = first_key
        return synthetic, label

    @staticmethod
    def _value(value: float, unit: str, label: str, rel_sigma: float) -> Value:
        return Value(
            value=value,
            unit=unit,
            uncertainty=absolute_band(value, rel_sigma),
            source=SourceRef(label=label, provenance=Provenance.PROVIDED),
        )

    def _write_per_building_log(
        self,
        rows: list[dict[str, Any]],
        climate: ClimateDataset,
    ) -> None:
        out = self.run_dir / "results" / "energy_per_building.json"
        out.write_text(
            json.dumps(
                {
                    "climate_source": climate.source,
                    "climate_location": climate.location,
                    "buildings": rows,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self._log_step("per-building energy table written", path=str(out))
