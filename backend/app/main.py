"""FastAPI entry point.

On startup: run pre-flight (spec §2). On failure, the app refuses to serve.
"""
from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.routes import router as api_router
from .config import settings
from .preflight import PreflightError, run_preflight

logging.basicConfig(
    level=settings.app_log_level,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("lca")


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        # In thesis-demo mode we run with strict=False so the UI can still load
        # and the operator can see *which* check failed in /api/health.
        # Flip to strict=True in production-like deployments.
        results = run_preflight(strict=False)
        for r in results:
            log.info("preflight %s: %s — %s", r.name, "OK" if r.ok else "FAIL", r.detail)
    except PreflightError as exc:
        log.error("preflight aborted startup: %s", exc)
        raise
    yield


app = FastAPI(title="B6-Bilanzierung (DIN EN 15978)", lifespan=lifespan)

# Frontend is served from a different origin in dev; locked to the compose
# network in production. Adjust origins if you front everything via a reverse proxy.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)
