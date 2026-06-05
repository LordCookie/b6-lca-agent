"""Base class for specialised agents.

Spec §9: values come from *tool calls* (DB lookup, geo op, calculation),
never from free LLM generation. Each subclass implements `run()` and
exposes tools that can be called by an LLM or directly.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from ..llm.router import LLMClient

log = logging.getLogger("lca.agent")


class AgentError(RuntimeError):
    """Raised when an agent cannot proceed and must STOP (spec §10 rule 2)."""


class Agent(ABC):
    name: str = "agent"

    def __init__(self, llm: LLMClient, run_dir: Path) -> None:
        self.llm = llm
        self.run_dir = run_dir

    @abstractmethod
    def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """Execute the agent's step. Must be idempotent where possible.

        Returns a dict merged into the run context for downstream agents.
        Raises AgentError on missing source / hard validation failure.
        """

    def _log_step(self, msg: str, **fields: Any) -> None:
        log.info(msg, extra={"agent": self.name, **fields})
