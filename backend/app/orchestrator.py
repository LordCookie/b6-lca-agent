"""Master orchestrator (spec §1, §7).

Decomposes the job, calls specialists in order, merges intermediate state,
triggers validation, holds the overall log. Does not produce expert values itself.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Root logger we attach the per-run FileHandler to. Anything logged under
# "lca.*" lands in runs/<id>/logs/run.log per spec §11.
_LCA_ROOT_LOGGER = "lca"

from .agents.energy import EnergyAgent
from .agents.geometry import GeometryAgent
from .agents.material import MaterialAgent
from .agents.reporting import ReportingAgent
from .agents.research import ResearchAgent
from .agents.vegetation import VegetationAgent
from .config import settings
from .llm.router import LLMClient
from .models import (
    AOI,
    ClusterResult,
    RunState,
    RunStatus,
    StepStatus,
)

log = logging.getLogger("lca.orchestrator")


class Orchestrator:
    def __init__(self, llm: LLMClient, run_dir: Path) -> None:
        self.llm = llm
        self.run_dir = run_dir
        self.state = RunState(
            run_id=run_dir.name,
            started_at=datetime.now(timezone.utc),
            status=RunStatus.PENDING,
            steps=[
                StepStatus(name=name, status=RunStatus.PENDING)
                for name in (
                    "geometry",
                    "vegetation",
                    "research",
                    "material",
                    "energy",
                    "reporting",
                )
            ],
        )

    def _step(self, name: str) -> StepStatus:
        return next(s for s in self.state.steps if s.name == name)

    def _begin(self, name: str) -> None:
        s = self._step(name)
        s.status = RunStatus.RUNNING
        s.started_at = datetime.now(timezone.utc)
        log.info("step_start", extra={"step": name})

    def _done(self, name: str) -> None:
        s = self._step(name)
        s.status = RunStatus.DONE
        s.finished_at = datetime.now(timezone.utc)
        log.info("step_done", extra={"step": name})

    def _fail(self, name: str, err: Exception) -> None:
        s = self._step(name)
        s.status = RunStatus.FAILED
        s.finished_at = datetime.now(timezone.utc)
        s.message = str(err)
        log.error("step_failed", extra={"step": name, "err": str(err)})

    def run(self, aoi: AOI) -> RunState:
        self.state.aoi = aoi
        self.state.status = RunStatus.RUNNING
        ctx: dict[str, Any] = {"aoi": aoi, "seed": 42}

        # Spec §11: per-run log file. Attach a FileHandler to the "lca" root
        # logger for the duration of this run; detach in finally.
        log_path = self.run_dir / "logs" / "run.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        run_handler = logging.FileHandler(log_path, encoding="utf-8")
        run_handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s"
        ))
        logging.getLogger(_LCA_ROOT_LOGGER).addHandler(run_handler)
        log.info("run_started", extra={"run_id": self.state.run_id})

        try:
            for name, agent in (
                ("geometry", GeometryAgent(self.llm, self.run_dir)),
                ("vegetation", VegetationAgent(self.llm, self.run_dir)),
                ("research", ResearchAgent(self.llm, self.run_dir)),
                ("material", MaterialAgent(self.llm, self.run_dir)),
                ("energy", EnergyAgent(self.llm, self.run_dir)),
            ):
                self._begin(name)
                try:
                    ctx.update(agent.run(ctx))
                except Exception as exc:
                    self._fail(name, exc)
                    # vegetation is separate from B6 — non-fatal (spec §8).
                    # With graceful-skip in VegetationAgent.run() this branch
                    # only triggers on a real crash (e.g. ONNX runtime error).
                    if name != "vegetation":
                        raise
                else:
                    self._done(name)
                    # If the agent decided to skip itself (vegetation without
                    # raster), surface the reason in the step status so the UI
                    # can show "DONE — skipped (reason)".
                    if ctx.get(f"{name}_skipped"):
                        self._step(name).message = ctx.get(f"{name}_skip_reason")

            cluster = ClusterResult(
                aoi=aoi,
                buildings=ctx.get("building_results", []),
                cluster_energy=ctx["cluster_energy"],
                vegetation=ctx.get("vegetation"),
            )
            ctx["cluster"] = cluster
            self.state.result = cluster

            # Spec §13: cumulative LLM usage (tokens + USD) per agent.
            if self.llm is not None and hasattr(self.llm, "usage_summary"):
                ctx["llm_usage"] = self.llm.usage_summary()

            self._begin("reporting")
            ReportingAgent(self.llm, self.run_dir).run(ctx)
            self._done("reporting")

            self.state.status = RunStatus.DONE
        except Exception as exc:
            self.state.status = RunStatus.FAILED
            self.state.error = str(exc)
            log.exception("run_failed")
        finally:
            log.info("run_finished", extra={
                "run_id": self.state.run_id,
                "status": self.state.status,
            })
            logging.getLogger(_LCA_ROOT_LOGGER).removeHandler(run_handler)
            run_handler.close()

        return self.state
