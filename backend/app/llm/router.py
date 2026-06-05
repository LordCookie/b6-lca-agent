"""OpenRouter client + per-agent model routing.

Spec §13:
- One key, OpenAI-compatible endpoint.
- Per-agent model selected by tier (strong / mid / fast).
- Model IDs come from models.yaml at runtime, never hardcoded.
- Every call logs model id, token count, cost (key never logged).
- Pricing per 1k tokens comes from data/llm/pricing.yaml — cumulative
  usage per agent is exposed via LLMClient.usage_summary().
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from openai import OpenAI

from ..config import settings

log = logging.getLogger("lca.llm")

_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


@dataclass
class AgentUsage:
    """Cumulative usage for one agent across an entire run."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    n_calls: int = 0
    n_web_searches: int = 0     # openrouter:web_search server-tool invocations
    web_search_cost_usd: float = 0.0


# OpenRouter Exa-engine pricing (May 2026): $0.005 / web_search request.
# Updated in pricing.yaml; this is the safety fallback.
_WEB_SEARCH_COST_USD_DEFAULT = 0.005


@dataclass
class PricingRow:
    input_per_1k: float
    output_per_1k: float
    note: str = ""


class PricingTable:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or settings.llm_pricing_path
        self._rows: dict[str, PricingRow] = {}
        self._default = PricingRow(0.010, 0.030, "no pricing file loaded")
        self.web_search_cost_usd = _WEB_SEARCH_COST_USD_DEFAULT
        self._source = ""
        self._checked_on = ""
        self.reload()

    def reload(self) -> None:
        if not self.path.is_file():
            log.warning("LLM pricing file not found at %s — using fallback", self.path)
            return
        with self.path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        self._source = str(raw.get("source", ""))
        self._checked_on = str(raw.get("checked_on", ""))
        models = raw.get("models") or {}
        self._rows = {
            mid: PricingRow(
                input_per_1k=float(v.get("input", 0.0)),
                output_per_1k=float(v.get("output", 0.0)),
                note="",
            )
            for mid, v in models.items()
        }
        default = raw.get("default") or {}
        if default:
            self._default = PricingRow(
                input_per_1k=float(default.get("input", 0.010)),
                output_per_1k=float(default.get("output", 0.030)),
                note=str(default.get("note", "fallback")),
            )
        # Server-tool pricing (openrouter:web_search via Exa, default $0.005/request)
        server_tools = raw.get("server_tools") or {}
        ws = server_tools.get("web_search") or {}
        self.web_search_cost_usd = float(ws.get("cost_per_request_usd",
                                                _WEB_SEARCH_COST_USD_DEFAULT))

    def lookup(self, model_id: str) -> tuple[PricingRow, bool]:
        """Returns (row, is_fallback). is_fallback=True warns the report reader."""
        row = self._rows.get(model_id)
        if row is None:
            return self._default, True
        return row, False

    @property
    def metadata(self) -> dict[str, str]:
        return {"source": self._source, "checked_on": self._checked_on,
                "path": str(self.path)}


def estimate_cost_usd(
    pricing: PricingTable,
    model_id: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> tuple[float, bool]:
    row, is_fallback = pricing.lookup(model_id)
    cost = (
        prompt_tokens * row.input_per_1k / 1000.0
        + completion_tokens * row.output_per_1k / 1000.0
    )
    return cost, is_fallback


@dataclass(frozen=True)
class AgentModel:
    agent: str
    tier: str
    model_id: str


class ModelRegistry:
    """Loads per-agent model assignments from models.yaml."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or settings.models_config_path
        self._by_agent: dict[str, AgentModel] = {}
        self._escalation: dict[str, str] = {}
        self.reload()

    def reload(self) -> None:
        with self.path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        agents = raw.get("agents", {})
        self._by_agent = {
            name: AgentModel(agent=name, tier=cfg["tier"], model_id=cfg["model"])
            for name, cfg in agents.items()
        }
        self._escalation = raw.get("escalation", {})

    def for_agent(self, agent: str) -> AgentModel:
        if agent not in self._by_agent:
            raise KeyError(f"no model assignment for agent '{agent}' in {self.path}")
        return self._by_agent[agent]

    def escalation_model(self, agent: str) -> str | None:
        return self._escalation.get(agent)


class LLMClient:
    """Thin wrapper around the OpenAI-compatible OpenRouter endpoint.

    Important: the API key is read from settings (env), never passed in by callers,
    never logged. Token/cost metadata is logged from response.usage.

    Per-run cumulative usage is tracked in `self.usage` (keyed by agent) so the
    manifest.json can report total tokens + USD per agent (spec §13).
    """

    def __init__(self, registry: ModelRegistry | None = None,
                 pricing: PricingTable | None = None) -> None:
        if not settings.openrouter_api_key:
            raise RuntimeError("OPENROUTER_API_KEY is not set")
        self._client = OpenAI(
            api_key=settings.openrouter_api_key,
            base_url=_OPENROUTER_BASE_URL,
            timeout=settings.timeout_llm_call,
        )
        self.registry = registry or ModelRegistry()
        self.pricing = pricing or PricingTable()
        self.usage: dict[str, AgentUsage] = defaultdict(AgentUsage)

    def chat(
        self,
        agent: str,
        messages: list[dict[str, Any]],
        *,
        seed: int | None = 42,
        temperature: float = 0.0,
        escalate: bool = False,
        tools: list[dict] | None = None,
        enable_web_search: bool = False,
        web_search_max_results: int = 5,
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        model_id = (
            self.registry.escalation_model(agent)
            if escalate
            else self.registry.for_agent(agent).model_id
        )
        if not model_id:
            raise RuntimeError(f"no model resolved for agent '{agent}' (escalate={escalate})")

        kwargs: dict[str, Any] = {
            "model": model_id,
            "messages": messages,
            "temperature": temperature,
        }
        if seed is not None:
            kwargs["seed"] = seed

        # OpenRouter Web-Search Server-Tool (spec §10 + §13). We append it to
        # the tools list rather than overriding so the caller can still pass
        # their own tools.
        effective_tools = list(tools or [])
        if enable_web_search:
            effective_tools.append({
                "type": "openrouter:web_search",
                "parameters": {
                    "engine": "auto",
                    "max_results": web_search_max_results,
                },
            })
        if effective_tools:
            kwargs["tools"] = effective_tools
        if response_format is not None:
            kwargs["response_format"] = response_format

        resp = self._client.chat.completions.create(**kwargs)
        usage = getattr(resp, "usage", None)
        pt = getattr(usage, "prompt_tokens", 0) or 0
        ct = getattr(usage, "completion_tokens", 0) or 0
        tt = getattr(usage, "total_tokens", pt + ct) or 0

        cost_usd, is_fallback_price = estimate_cost_usd(self.pricing, model_id, pt, ct)

        # Extract web_search usage from the response metadata (only present
        # when the openrouter:web_search server-tool fired).
        web_search_requests = 0
        try:
            stu = getattr(usage, "server_tool_use", None)
            if stu is not None:
                wsr = getattr(stu, "web_search_requests", None)
                if isinstance(wsr, int):
                    web_search_requests = wsr
                elif isinstance(stu, dict):
                    web_search_requests = int(stu.get("web_search_requests", 0))
        except Exception:
            web_search_requests = 0

        web_search_cost = web_search_requests * self.pricing.web_search_cost_usd
        total_call_cost = cost_usd + web_search_cost

        bucket = self.usage[agent]
        bucket.prompt_tokens += pt
        bucket.completion_tokens += ct
        bucket.total_tokens += tt
        bucket.cost_usd += total_call_cost
        bucket.n_calls += 1
        bucket.n_web_searches += web_search_requests
        bucket.web_search_cost_usd += web_search_cost

        log.info(
            "llm_call",
            extra={
                "agent": agent,
                "model": model_id,
                "prompt_tokens": pt,
                "completion_tokens": ct,
                "total_tokens": tt,
                "cost_usd": round(total_call_cost, 6),
                "web_search_requests": web_search_requests,
                "fallback_price": is_fallback_price,
            },
        )
        choice = resp.choices[0]

        # Extract url_citation annotations (where the web-search results land)
        # so the research agent can pin sources to its results.
        annotations: list[dict[str, Any]] = []
        try:
            ann = getattr(choice.message, "annotations", None) or []
            for a in ann:
                if hasattr(a, "model_dump"):
                    annotations.append(a.model_dump())
                elif isinstance(a, dict):
                    annotations.append(a)
        except Exception:
            pass

        return {
            "model": model_id,
            "content": choice.message.content,
            "tool_calls": choice.message.tool_calls,
            "annotations": annotations,
            "usage": usage.model_dump() if usage else None,
            "cost_usd": total_call_cost,
            "web_search_requests": web_search_requests,
        }

    def record_web_search(
        self, agent: str, n: int = 1, cost_usd: float = 0.0
    ) -> None:
        """Record web searches we executed ourselves (not via the OpenRouter
        server-tool). Keeps the report's "Web-Searches" column accurate when the
        research agents use the self-hosted DuckDuckGo search (llm.web_search).
        DuckDuckGo is keyless → cost_usd defaults to 0.
        """
        bucket = self.usage[agent]
        bucket.n_web_searches += n
        bucket.web_search_cost_usd += cost_usd
        bucket.cost_usd += cost_usd

    def usage_summary(self) -> dict[str, Any]:
        """Aggregated usage for the manifest. Spec §13."""
        per_agent = {
            agent: {
                "n_calls": u.n_calls,
                "prompt_tokens": u.prompt_tokens,
                "completion_tokens": u.completion_tokens,
                "total_tokens": u.total_tokens,
                "cost_usd": round(u.cost_usd, 6),
                "n_web_searches": u.n_web_searches,
                "web_search_cost_usd": round(u.web_search_cost_usd, 6),
            }
            for agent, u in self.usage.items()
        }
        total_tokens = sum(u.total_tokens for u in self.usage.values())
        total_cost = sum(u.cost_usd for u in self.usage.values())
        total_searches = sum(u.n_web_searches for u in self.usage.values())
        return {
            "pricing_source": self.pricing.metadata,
            "per_agent": per_agent,
            "total_tokens": total_tokens,
            "total_web_searches": total_searches,
            "total_cost_usd": round(total_cost, 6),
        }
