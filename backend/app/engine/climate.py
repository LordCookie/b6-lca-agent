"""Climate-data loader for the monthly balance (DIN V 18599-2).

Supports two input formats (auto-detected by file extension):
  *.epw  — EnergyPlus Weather, 8760 hourly rows. Aggregated to monthly
           mean dry-bulb temperature and monthly sum of global horizontal
           radiation. The preferred format for the thesis — TMYx
           (climate.onebuilding.org) ships as .epw.
  *.yaml — 12 pre-aggregated monthly rows (Platzhalter format).

EPW column reference (standard EnergyPlus order, zero-indexed):
   0=Year, 1=Month, 2=Day, 3=Hour, 4=Minute, 5=Data-Source flags,
   6=Dry Bulb [°C], 7=Dew Point, 8=Relative Humidity, 9=Pressure,
   10=Extraterrestrial Horizontal, 11=Extraterrestrial Direct Normal,
   12=Horizontal Infrared, 13=Global Horizontal Radiation [Wh/m²], …

Reference: EnergyPlus Auxiliary Programs Documentation, "Weather Converter".
"""
from __future__ import annotations

import calendar
import csv
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from ..config import settings


@dataclass(frozen=True)
class ClimateMonth:
    month: int
    theta_e_C: float
    I_S_horizontal_kWh_m2: float
    days: int

    @property
    def seconds(self) -> float:
        return self.days * 86_400.0

    @property
    def hours(self) -> int:
        return self.days * 24


@dataclass(frozen=True)
class ClimateDataset:
    source: str
    location: str
    months: tuple[ClimateMonth, ...]
    heating_setpoint_C_default: float

    def __post_init__(self) -> None:
        if len(self.months) != 12:
            raise ValueError(
                f"climate dataset must have 12 monthly rows, got {len(self.months)}"
            )


@lru_cache(maxsize=4)
def load_climate(path: Path | None = None) -> ClimateDataset:
    p = Path(path) if path else _default_climate_path()
    if p.suffix.lower() == ".epw":
        return _load_epw(p)
    return _load_yaml(p)


def _load_yaml(p: Path) -> ClimateDataset:
    with p.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    months = tuple(
        ClimateMonth(
            month=int(m["month"]),
            theta_e_C=float(m["theta_e_C"]),
            I_S_horizontal_kWh_m2=float(m["I_S_horizontal_kWh_m2"]),
            days=int(m["days"]),
        )
        for m in raw["months"]
    )
    return ClimateDataset(
        source=str(raw["source"]),
        location=str(raw["location"]),
        months=months,
        heating_setpoint_C_default=float(raw.get("heating_setpoint_C_default", 20.0)),
    )


# --- EPW parser -----------------------------------------------------------

# Standard EPW column indices (zero-based) — see EnergyPlus Aux Programs Doc.
_EPW_COL_MONTH = 1
_EPW_COL_DAY = 2
_EPW_COL_HOUR = 3
_EPW_COL_DRYBULB_C = 6
_EPW_COL_GHI_WH_M2 = 13
_EPW_HEADER_ROWS = 8


def _load_epw(path: Path) -> ClimateDataset:
    """Aggregate 8760 hourly rows into 12 monthly climate values.

    θ_e,m = mean(Dry Bulb) over month m
    I_S,m = sum(GHI [Wh/m²]) / 1000  → kWh/m² per month
    """
    monthly_temp_sum = [0.0] * 12
    monthly_temp_count = [0] * 12
    monthly_ghi_wh = [0.0] * 12

    location = "(unknown — EPW header missing)"
    with path.open("r", encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f)
        # First header line: LOCATION,<city>,<state>,<country>,<source>,<wmo>,<lat>,<lon>,<tz>,<elev>
        first = next(reader, None)
        if first and len(first) >= 6 and first[0].upper() == "LOCATION":
            city = first[1].strip()
            country = first[3].strip()
            location = f"{city}, {country}"
        # Skip the remaining header lines (we already consumed line 1).
        for _ in range(_EPW_HEADER_ROWS - 1):
            next(reader, None)
        # Data rows.
        for row in reader:
            if len(row) <= _EPW_COL_GHI_WH_M2:
                continue
            try:
                m = int(row[_EPW_COL_MONTH]) - 1
                t = float(row[_EPW_COL_DRYBULB_C])
                ghi = float(row[_EPW_COL_GHI_WH_M2])
            except ValueError:
                continue
            if not 0 <= m < 12:
                continue
            monthly_temp_sum[m] += t
            monthly_temp_count[m] += 1
            monthly_ghi_wh[m] += ghi

    months = []
    # Determine days per month from the Year column (use a non-leap reference).
    for i in range(12):
        if monthly_temp_count[i] == 0:
            raise ValueError(f"EPW {path.name}: month {i+1} has no data rows")
        theta_e = monthly_temp_sum[i] / monthly_temp_count[i]
        i_s_kwh = monthly_ghi_wh[i] / 1000.0
        # Reference year 2023 (non-leap) for days-per-month — matches the
        # standard TMY assumption of a non-leap composite year.
        days = calendar.monthrange(2023, i + 1)[1]
        months.append(
            ClimateMonth(
                month=i + 1,
                theta_e_C=round(theta_e, 2),
                I_S_horizontal_kWh_m2=round(i_s_kwh, 1),
                days=days,
            )
        )

    return ClimateDataset(
        source=f"TMYx EPW (EnergyPlus Weather) — {path.name}",
        location=location,
        months=tuple(months),
        heating_setpoint_C_default=20.0,
    )


def _default_climate_path() -> Path:
    # Allow override via env if ever needed.
    base = getattr(settings, "climate_data_path", None)
    return Path(base) if base else Path("/app/data/climate/oldenburg_tmyx.yaml")
