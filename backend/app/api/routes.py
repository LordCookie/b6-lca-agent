"""HTTP API for the web UI.

Endpoints:
  GET  /api/health            -> liveness + preflight summary
  POST /api/runs              -> start a run from an AOI polygon
  GET  /api/runs/{id}         -> poll run state
  GET  /api/runs/{id}/report  -> served Markdown report
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse

from ..llm.router import LLMClient
from ..models import AOI, RunState
from ..orchestrator import Orchestrator
from ..preflight import new_run_dir, run_preflight

log = logging.getLogger("lca.api")
router = APIRouter(prefix="/api")

# in-memory run registry (per process — fine for the thesis demo).
_RUNS: dict[str, RunState] = {}


@router.get("/health")
def health() -> dict[str, Any]:
    results = run_preflight(strict=False)
    return {
        "ok": all(r.ok for r in results),
        "checks": [r.__dict__ for r in results],
    }


@router.post("/runs", response_model=RunState)
async def start_run(aoi: AOI, background: BackgroundTasks) -> RunState:
    run_dir = new_run_dir()
    try:
        llm = LLMClient()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    orch = Orchestrator(llm, run_dir)
    _RUNS[orch.state.run_id] = orch.state

    async def _execute() -> None:
        # CPU/IO-bound work — push to thread so the event loop stays free.
        await asyncio.to_thread(orch.run, aoi)

    background.add_task(_execute)
    return orch.state


@router.get("/runs/{run_id}", response_model=RunState)
def get_run(run_id: str) -> RunState:
    if run_id not in _RUNS:
        raise HTTPException(404, "unknown run_id")
    return _RUNS[run_id]


@router.get("/runs/{run_id}/report", response_class=PlainTextResponse)
def get_report(run_id: str) -> str:
    if run_id not in _RUNS:
        raise HTTPException(404, "unknown run_id")
    from ..config import settings

    report = settings.app_run_base_dir / run_id / "results" / "report.md"
    if not report.is_file():
        raise HTTPException(404, "report not generated yet")
    return report.read_text(encoding="utf-8")


@router.get("/runs/{run_id}/aoi_raster_preview")
def get_aoi_preview(run_id: str) -> FileResponse:
    """Spec §3 step 3: clipped AOI raster, surfaced to the UI."""
    if run_id not in _RUNS:
        raise HTTPException(404, "unknown run_id")
    from ..config import settings

    png = settings.app_run_base_dir / run_id / "results" / "aoi_raster_preview.png"
    if not png.is_file():
        raise HTTPException(404, "AOI raster preview not generated (no DOP mounted?)")
    return FileResponse(png, media_type="image/png")


@router.get("/runs/{run_id}/advice")
def get_advice(run_id: str) -> FileResponse:
    """KI-Empfehlungen (advice.json) als strukturiertes JSON."""
    if run_id not in _RUNS:
        raise HTTPException(404, "unknown run_id")
    from ..config import settings

    p = settings.app_run_base_dir / run_id / "results" / "advice.json"
    if not p.is_file():
        raise HTTPException(404, "advice not generated (LLM disabled or failed?)")
    return FileResponse(p, media_type="application/json")


@router.get("/runs/{run_id}/pdf")
def get_run_pdf(run_id: str) -> FileResponse:
    """Render report.md to a PDF and return it for download."""
    if run_id not in _RUNS:
        raise HTTPException(404, "unknown run_id")
    from ..config import settings
    from ..engine import pdf_export
    from ..engine.pdf_export import render_report_to_pdf

    report = settings.app_run_base_dir / run_id / "results" / "report.md"
    pdf = settings.app_run_base_dir / run_id / "results" / "report.pdf"
    if not report.is_file():
        raise HTTPException(404, "report not generated yet")
    # Cache-Invalidierung: regenerieren wenn PDF älter als (a) report.md
    # oder (b) das pdf_export-Modul selbst (CSS-Updates schlagen sonst nicht durch).
    exporter_mtime = Path(pdf_export.__file__).stat().st_mtime
    needs_regen = (
        not pdf.is_file()
        or pdf.stat().st_mtime < report.stat().st_mtime
        or pdf.stat().st_mtime < exporter_mtime
    )
    if needs_regen:
        try:
            render_report_to_pdf(report, pdf)
        except Exception as exc:
            raise HTTPException(500, f"PDF export failed: {exc}") from exc
    return FileResponse(
        pdf,
        media_type="application/pdf",
        filename=f"Cluster-Bilanz-B6_{run_id}.pdf",
    )
