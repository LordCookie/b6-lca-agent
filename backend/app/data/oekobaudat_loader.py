"""ÖKOBAUDAT CSV snapshot loader.

Reads a local ÖKOBAUDAT export (semicolon-delimited, cp1252,
German decimal notation, version 2024-I or compatible) and indexes
the rows by UUID for fast lookup by the Material agent.

For *operational* energy (spec Modul B6) the rows that matter look like:
    Bezugsgröße = 3,6 MJ   (= 1 kWh)
    Konformität = EN 15804+A2 (EF 3.1)
    Modul       = B6
    GWPtotal (A2) — the GWP figure to use; the legacy `GWP` column is
                    empty for A2 datasets.

Spec §10: nothing is invented here. If a row is missing the required GWP
or the reference unit is unsupported, `to_factor_kg_co2eq_per_kwh()` raises.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path

OBD_ENCODING = "cp1252"
OBD_DELIMITER = ";"

# Required schema columns. If any are missing we refuse to load the file —
# this catches "user mounted the wrong CSV" early.
_REQUIRED_COLUMNS = (
    "UUID",
    "Version",
    "Name (de)",
    "Konformitaet",
    "Bezugsgroesse",
    "Bezugseinheit",
    "Modul",
    "GWP",
    "GWPtotal (A2)",
    "PENRT",
)

_SNAPSHOT_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_VERSION_RE = re.compile(r"(\d{4}-[IVX]+)", re.IGNORECASE)


@dataclass(frozen=True)
class OekobaudatRecord:
    uuid: str
    version: str
    name_de: str
    modul: str
    konformitaet: str
    bezug_value: float
    bezug_unit: str           # 'MJ' or 'kWh' for energy carriers
    gwp_total_kg_co2eq: float | None
    penrt_per_ref: float | None

    def to_factor_kg_co2eq_per_kwh(self) -> float:
        """Convert the row's GWP to kg CO₂-äq. per kWh end-energy.

        Raises if the row has no usable GWP value (spec §10: no invention).
        """
        if self.gwp_total_kg_co2eq is None:
            raise ValueError(
                f"OBD row {self.uuid} ({self.modul}) has no GWP value (neither "
                "legacy GWP nor GWPtotal (A2)) — refusing to fabricate"
            )
        kwh = _ref_to_kwh(self.bezug_value, self.bezug_unit)
        return self.gwp_total_kg_co2eq / kwh


def _ref_to_kwh(value: float, unit: str) -> float:
    u = unit.strip()
    if u in ("MJ", "Mj", "mj"):
        return value / 3.6
    if u.lower() == "kwh":
        return value
    raise ValueError(
        f"reference unit {unit!r} not supported for an energy carrier "
        "(expected 'MJ' or 'kWh')"
    )


def _parse_decimal(raw: str | None) -> float | None:
    """German decimal: '0,254' -> 0.254. Empty -> None."""
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def _row_to_record(row: dict[str, str]) -> OekobaudatRecord | None:
    bezug_value = _parse_decimal(row.get("Bezugsgroesse"))
    bezug_unit = (row.get("Bezugseinheit") or "").strip()
    if bezug_value is None or not bezug_unit:
        return None
    # Prefer EN 15804+A2 GWPtotal; fall back to the legacy GWP column
    # for older +A1 datasets.
    gwp = _parse_decimal(row.get("GWPtotal (A2)")) or _parse_decimal(row.get("GWP"))
    return OekobaudatRecord(
        uuid=(row.get("UUID") or "").strip(),
        version=(row.get("Version") or "").strip(),
        name_de=(row.get("Name (de)") or "").strip(),
        modul=(row.get("Modul") or "").strip(),
        konformitaet=(row.get("Konformitaet") or "").strip().strip("'"),
        bezug_value=bezug_value,
        bezug_unit=bezug_unit,
        gwp_total_kg_co2eq=gwp,
        penrt_per_ref=_parse_decimal(row.get("PENRT")),
    )


@dataclass(frozen=True)
class OekobaudatSnapshot:
    """In-memory ÖKOBAUDAT snapshot keyed by (UUID, Modul).

    A single UUID typically has multiple module rows (A1-A3, A4, A5, B6, C…),
    so callers must specify which life-cycle module they want.
    """
    path: Path
    snapshot_date: date | None
    version_label: str | None
    by_uuid_module: dict[tuple[str, str], OekobaudatRecord]

    def get(self, uuid: str, modul: str) -> OekobaudatRecord:
        try:
            return self.by_uuid_module[(uuid, modul)]
        except KeyError as exc:
            raise KeyError(
                f"ÖKOBAUDAT snapshot has no row for UUID={uuid} Modul={modul} "
                f"(snapshot={self.path.name})"
            ) from exc

    @property
    def n_rows(self) -> int:
        return len(self.by_uuid_module)


@lru_cache(maxsize=4)
def load_snapshot(path: Path) -> OekobaudatSnapshot:
    """Load and cache an OBD snapshot. Cache keyed by path; cheap to call repeatedly."""
    if not path.is_file():
        raise FileNotFoundError(f"ÖKOBAUDAT snapshot not found at {path}")

    with path.open("r", encoding=OBD_ENCODING, newline="") as f:
        reader = csv.DictReader(f, delimiter=OBD_DELIMITER)
        fieldnames = reader.fieldnames or []
        missing = [c for c in _REQUIRED_COLUMNS if c not in fieldnames]
        if missing:
            raise ValueError(
                f"ÖKOBAUDAT snapshot {path.name} is missing required columns: {missing}. "
                "Mount the official OBD export (CSV mit Sub-Module Aufschlüsselung)."
            )

        index: dict[tuple[str, str], OekobaudatRecord] = {}
        for row in reader:
            rec = _row_to_record(row)
            if rec is None or not rec.uuid or not rec.modul:
                continue
            index[(rec.uuid, rec.modul)] = rec

    snapshot_date = _extract_snapshot_date(path.name)
    version_label = _extract_version(path.name)
    return OekobaudatSnapshot(
        path=path,
        snapshot_date=snapshot_date,
        version_label=version_label,
        by_uuid_module=index,
    )


def _extract_snapshot_date(filename: str) -> date | None:
    m = _SNAPSHOT_DATE_RE.search(filename)
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(1))
    except ValueError:
        return None


def _extract_version(filename: str) -> str | None:
    m = _VERSION_RE.search(filename)
    return m.group(1) if m else None
