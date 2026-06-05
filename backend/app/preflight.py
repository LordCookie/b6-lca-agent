"""Pre-flight check enforced at container start (spec §2).

Aborts with a clear, actionable message if any required condition is missing.
The API key is only *probed for presence*, never echoed or logged.
"""
from __future__ import annotations

import socket
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import settings


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


class PreflightError(RuntimeError):
    pass


def _check_api_key() -> CheckResult:
    present = bool(settings.openrouter_api_key.strip())
    return CheckResult(
        name="OPENROUTER_API_KEY",
        ok=present,
        detail="set" if present else "missing — set OPENROUTER_API_KEY in .env",
    )


def _check_geo_stack() -> CheckResult:
    missing = []
    try:
        import shapely  # noqa: F401
        import pyproj  # noqa: F401
        import rasterio  # noqa: F401
    except ImportError as exc:
        missing.append(str(exc))
    try:
        import onnxruntime  # noqa: F401
    except ImportError as exc:
        missing.append(f"onnxruntime: {exc}")
    if missing:
        return CheckResult(name="geo_stack", ok=False, detail="; ".join(missing))
    return CheckResult(name="geo_stack", ok=True, detail="shapely/pyproj/rasterio/onnxruntime present")


def _check_deepness_model() -> CheckResult:
    p = settings.deepness_onnx_path
    if p.is_file():
        return CheckResult(name="deepness_model", ok=True, detail=str(p))
    return CheckResult(
        name="deepness_model",
        ok=False,
        detail=f"ONNX model not found at {p}. Mount it via docker-compose volume.",
    )


def _check_internet() -> CheckResult:
    try:
        socket.create_connection(("oekobaudat.de", 443), timeout=5).close()
    except OSError as exc:
        return CheckResult(name="internet", ok=False, detail=f"no outbound: {exc}")
    return CheckResult(name="internet", ok=True, detail="reachable")


def _check_lookup_table() -> CheckResult:
    p = settings.lookup_table_path
    if p.is_file():
        return CheckResult(name="lookup_table", ok=True, detail=str(p))
    return CheckResult(
        name="lookup_table",
        ok=False,
        detail=(
            f"central lookup table not found at {p}. "
            "ÖKOBAUDAT snapshot will be used as fallback for missing carriers."
        ),
    )


def _check_climate_and_profiles() -> CheckResult:
    missing = []
    if not settings.climate_data_path.is_file():
        missing.append(f"climate: {settings.climate_data_path}")
    pdir = settings.profiles_dir
    for name in ("usage_profiles.yaml", "envelope_defaults.yaml", "system_efficiency.yaml"):
        if not (pdir / name).is_file():
            missing.append(f"profile: {pdir / name}")
    if missing:
        return CheckResult(
            name="climate_and_profiles",
            ok=False,
            detail="missing input files — " + "; ".join(missing),
        )
    return CheckResult(
        name="climate_and_profiles",
        ok=True,
        detail=f"{settings.climate_data_path.name} + 3 profile files",
    )


def _check_oekobaudat_snapshot() -> CheckResult:
    p = settings.oekobaudat_csv_path
    if not p.is_file():
        return CheckResult(
            name="oekobaudat_snapshot",
            ok=False,
            detail=f"ÖKOBAUDAT CSV snapshot not found at {p}",
        )
    cm = settings.carrier_map_path
    if not cm.is_file():
        return CheckResult(
            name="oekobaudat_snapshot",
            ok=False,
            detail=f"carrier_map.yaml not found at {cm}",
        )
    return CheckResult(name="oekobaudat_snapshot", ok=True, detail=f"{p.name} + {cm.name}")


def _check_runs_dir() -> CheckResult:
    base = settings.app_run_base_dir
    try:
        base.mkdir(parents=True, exist_ok=True)
        probe = base / ".write_probe"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        return CheckResult(name="runs_dir", ok=False, detail=f"{base}: {exc}")
    return CheckResult(name="runs_dir", ok=True, detail=str(base))


def run_preflight(strict: bool = True) -> list[CheckResult]:
    results = [
        _check_api_key(),
        _check_geo_stack(),
        _check_deepness_model(),
        _check_internet(),
        _check_lookup_table(),
        _check_oekobaudat_snapshot(),
        _check_climate_and_profiles(),
        _check_runs_dir(),
    ]
    if strict and any(not r.ok for r in results):
        lines = [f"  - {r.name}: {r.detail}" for r in results if not r.ok]
        raise PreflightError(
            "Pre-flight check failed:\n" + "\n".join(lines)
        )
    return results


def new_run_dir() -> Path:
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    run = settings.app_run_base_dir / stamp
    for sub in ("logs", "results", "sources"):
        (run / sub).mkdir(parents=True, exist_ok=True)
    return run
