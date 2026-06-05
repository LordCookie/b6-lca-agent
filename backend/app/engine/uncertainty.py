"""Uncertainty propagation (spec §6: Gauß'sche Fortpflanzung).

Simple per-value relative-σ approach.  We treat the dominant inputs
(U-values, profile, climate, system efficiency) as independent and combine
their relative standard deviations in quadrature on the multiplicative chain.
"""
from __future__ import annotations

import math


def combine_relative(*rel_sigmas: float) -> float:
    """sqrt( Σ σ_i² ) for relative standard uncertainties on a product."""
    return math.sqrt(sum(s * s for s in rel_sigmas if s is not None))


def absolute_band(value: float, rel_sigma: float) -> float:
    """Convert relative σ to an absolute ± band."""
    return value * rel_sigma
