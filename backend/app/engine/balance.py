"""Monthly heating-demand balance — DIN V 18599-2 (simplified).

Per month m for one building zone:

    Q_T,m = ΣU·A · (θ_i − θ_e,m) · Δt_m              [Wh]   transmission
    Q_V,m = c_p,air · ρ_air · n · V · (θ_i − θ_e,m) · Δt_m  ventilation
    Q_S,m = I_S,m · g · F_S · F_F · A_window         [Wh]   solar gains
    Q_I,m = q_I · A_NGF · t_op,m                     [Wh]   internal gains
    γ_m   = (Q_S,m + Q_I,m) / (Q_T,m + Q_V,m)               gain/loss ratio
    η_m   = utilisation factor (Annex C, eq. C.3 with a=1, τ=∞ default ≈ 1/γ if γ>1)
    Q_h,m = max(0, Q_T,m + Q_V,m − η_m · (Q_S,m + Q_I,m))   heating demand

Aggregation: Q_h = Σ_m Q_h,m  → end-energy = Q_h / η_system.

The utilisation-factor model is the DIN V 18599-2 standard formulation,
simplified to one zone per building (no zone splitting in this skeleton —
the Building model carries a single use_mix that we'll handle later by
weighted-averaging profile parameters before calling this function).
"""
from __future__ import annotations

from dataclasses import dataclass

from .climate import ClimateDataset
from .envelope import EnvelopeAreas
from .profiles import EnvelopeDefaults, UsageProfile

# DIN V 1946 / 18599: ρ_air · c_p,air ≈ 0.34 Wh/(m³·K)
_VOL_HEAT_CAPACITY_AIR_WH_PER_M3K = 0.34
# Internal storey volume share treated as ventilated (V = A_NGF · h_room).
# DIN 18599 uses a clear-height of ~2.5 m for institutional buildings.
_CLEAR_ROOM_HEIGHT_M = 2.5
# Utilisation-factor exponent for heavy/medium construction (DIN V 18599-2 Annex C).
# a = 1.0 for medium-mass, sufficient for this skeleton.
_UTIL_A = 1.0


@dataclass(frozen=True)
class BalanceResult:
    Q_T_kWh_a: float
    Q_V_kWh_a: float
    Q_S_kWh_a: float
    Q_I_kWh_a: float
    Q_h_kWh_a: float          # heating demand (useful, before system losses)
    monthly_Q_h_kWh: tuple[float, ...]


def monthly_heating_demand(
    envelope: EnvelopeAreas,
    env_defaults: EnvelopeDefaults,
    profile: UsageProfile,
    climate: ClimateDataset,
) -> BalanceResult:
    UA = (
        env_defaults.u_wall * envelope.a_wall_opaque_m2
        + env_defaults.u_roof * envelope.a_roof_m2
        + env_defaults.u_floor * envelope.a_floor_m2
        + env_defaults.u_window * envelope.a_window_m2
    )
    volume_m3 = envelope.ngf_m2 * _CLEAR_ROOM_HEIGHT_M
    cV = (
        _VOL_HEAT_CAPACITY_AIR_WH_PER_M3K
        * profile.air_change_rate_1_per_h
        * volume_m3
    )  # [Wh / (h·K)]

    theta_i = profile.theta_i_set_heating_C
    # Internal gains as average power × operating hours per month.
    operation_hours_per_year = profile.operation_hours_per_day * profile.operation_days_per_year
    avg_op_hours_per_day = operation_hours_per_year / 365.0
    q_int_total_avg_W = profile.q_internal_W_per_m2 * envelope.ngf_m2

    # Aperture for solar gains (window area × glazing properties).
    solar_aperture_m2 = (
        envelope.a_window_m2
        * env_defaults.g_value
        * env_defaults.shading_factor_F_S
        * env_defaults.frame_factor_F_F
    )

    Q_T_kWh = Q_V_kWh = Q_S_kWh = Q_I_kWh = Q_h_kWh = 0.0
    monthly: list[float] = []

    for m in climate.months:
        delta_T = max(0.0, theta_i - m.theta_e_C)
        hours = m.hours

        Q_T_m_Wh = UA * delta_T * hours
        Q_V_m_Wh = cV * delta_T * hours
        # I_S is monthly kWh/m² on the horizontal — for an equivalent vertical
        # facade we apply a 0.7 obliquity factor (rough average for the four
        # cardinal facades at 50°N). A full 18599-2 §6 model would project to
        # each orientation; that's the next refinement step.
        Q_S_m_kWh = m.I_S_horizontal_kWh_m2 * 0.7 * solar_aperture_m2
        Q_I_m_kWh = q_int_total_avg_W * avg_op_hours_per_day * m.days / 1000.0

        Q_T_m_kWh = Q_T_m_Wh / 1000.0
        Q_V_m_kWh = Q_V_m_Wh / 1000.0
        losses = Q_T_m_kWh + Q_V_m_kWh
        gains = Q_S_m_kWh + Q_I_m_kWh

        eta = _utilisation_factor(gains, losses, a=_UTIL_A)
        Q_h_m = max(0.0, losses - eta * gains)

        Q_T_kWh += Q_T_m_kWh
        Q_V_kWh += Q_V_m_kWh
        Q_S_kWh += Q_S_m_kWh
        Q_I_kWh += Q_I_m_kWh
        Q_h_kWh += Q_h_m
        monthly.append(Q_h_m)

    return BalanceResult(
        Q_T_kWh_a=Q_T_kWh,
        Q_V_kWh_a=Q_V_kWh,
        Q_S_kWh_a=Q_S_kWh,
        Q_I_kWh_a=Q_I_kWh,
        Q_h_kWh_a=Q_h_kWh,
        monthly_Q_h_kWh=tuple(monthly),
    )


def _utilisation_factor(gains: float, losses: float, a: float = 1.0) -> float:
    """DIN V 18599-2 Annex C utilisation factor for heat gains.

    Simplified form (a = 1, no time-constant correction):
        γ = gains / losses
        η = (1 − γ^a) / (1 − γ^(a+1))    for γ ≠ 1
        η = a / (a + 1)                  for γ = 1
        η = 1                            for losses ≤ 0
    """
    if losses <= 0:
        return 1.0
    gamma = gains / losses
    if gamma <= 0:
        return 1.0
    if abs(gamma - 1.0) < 1e-9:
        return a / (a + 1.0)
    return (1.0 - gamma**a) / (1.0 - gamma ** (a + 1.0))


def electricity_demand_kWh_a(profile: UsageProfile, ngf_m2: float) -> float:
    """Annual electricity demand from profile value × NGF.

    This is a CEA-style simplification — a full DIN V 18599 (Teile 4/7/9)
    model would split into lighting / ventilation / aux pumps. The tool uses
    the area-normalised profile value (the common Teilenergiekennwert form).
    """
    return profile.q_electricity_kWh_per_m2a * ngf_m2
