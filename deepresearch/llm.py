"""Thin wrapper over the Claude Agent SDK.

Every model interaction goes through `run_agent`. Centralising it buys uniform
cost accounting, uniform tolerant JSON parsing, plan-limit handling, and one
place to enforce the subscription-only rule.

Model policy: Sonnet by default. The docs' own cost guidance for subscription
plans is to use Sonnet and reserve Opus for hard reasoning, and Opus has a
separate, smaller weekly bucket on a plan -- burning it inside a research loop
would lock you out of Opus everywhere else, including normal Claude use.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    RateLimitEvent,
    ResultMessage,
    TextBlock,
    query,
)

from .guardrails import PlanGuard, scrubbed_env

# Sonnet everywhere by default -- see module docstring. Override per-run if you
# have Opus headroom to spare.
ORCHESTRATOR_MODEL = "sonnet"
WORKER_MODEL = "sonnet"
VERIFIER_MODEL = "sonnet"

# The package root holds .claude/skills/. Workers get our bundled research
# skills by pointing cwd here with the "project" setting source.
PACKAGE_ROOT = Path(__file__).resolve().parent.parent

# Bundled skills, by name. Loaded via the filesystem, which is the only way the
# SDK discovers skills -- there is no programmatic registration API.
RESEARCH_SKILLS = [
    "search-strategy",
    "source-triage",
    "claim-extraction",
    "numeric-scrutiny",
]


@dataclass
class AgentResult:
    text: str
    cost_usd: float = 0.0   # local estimate at list price; NOT a charge
    turns: int = 0
    subtype: str = ""
    tool_calls: list[str] = field(default_factory=list)
    skills_used: list[str] = field(default_factory=list)
    rate_limited: str | None = None   # halt reason if the plan limit was hit

    @property
    def ok(self) -> bool:
        return self.subtype == "success" or not self.subtype.startswith("error")


# Result subtypes / error text that mean "plan usage window exhausted".
_LIMIT_MARKERS = (
    "usage limit",
    "weekly limit",
    "session limit",
    "rate_limit",
    "rate limit",
)


def _looks_like_limit(result: ResultMessage) -> bool:
    haystack = " ".join(
        filter(
            None,
            [
                result.subtype or "",
                result.terminal_reason or "",
                result.stop_reason or "",
                " ".join(result.errors or []),
            ],
        )
    ).lower()
    return any(marker in haystack for marker in _LIMIT_MARKERS)


async def run_agent(
    prompt: str,
    *,
    system_prompt: str,
    model: str = ORCHESTRATOR_MODEL,
    allowed_tools: list[str] | None = None,
    effort: str = "medium",
    max_turns: int = 24,
    task_budget_tokens: int | None = None,
    skills: list[str] | None = None,
    guard: PlanGuard | None = None,
) -> AgentResult:
    """One isolated agent run. Each call is its own fresh context.

    `setting_sources=["project"]` with `cwd=PACKAGE_ROOT` is a deliberate
    compromise. Skills are discovered only through filesystem setting sources,
    so a fully hermetic `setting_sources=[]` would silently disable every
    research skill. Scoping to "project" with cwd pinned to this package picks
    up our bundled `.claude/skills/` while still ignoring your personal
    `~/.claude` config -- so runs stay reproducible regardless of where you
    launch from.
    """
    tools = list(allowed_tools or [])
    if skills and "Skill" not in tools:
        tools.append("Skill")

    opts: dict[str, Any] = {
        "model": model,
        "system_prompt": system_prompt,
        "allowed_tools": tools,
        "permission_mode": "bypassPermissions",
        "cwd": str(PACKAGE_ROOT),
        "setting_sources": ["project"],
        "skills": skills or [],
        "max_turns": max_turns,
        "effort": effort,
        "thinking": {"type": "adaptive"},
        # No uncontrolled subagent trees: this engine owns the fan-out, and
        # every stray subagent would draw on the same plan windows.
        "env": scrubbed_env(
            {
                "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "1",
                "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": "2",
                # Never auto-wait on a usage limit inside a loop -- we halt and
                # checkpoint instead, so the GUI does not hang for hours.
                "CLAUDE_CODE_AUTO_CONTINUE_AT_USAGE_LIMIT": "0",
            }
        ),
    }
    if task_budget_tokens is not None:
        opts["task_budget"] = {"type": "tokens", "total": task_budget_tokens}

    out: list[str] = []
    result = AgentResult(text="")
    try:
        async for message in query(prompt=prompt, options=ClaudeAgentOptions(**opts)):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        out.append(block.text)
                    elif getattr(block, "name", None):
                        name = str(block.name)
                        result.tool_calls.append(name)
                        if name == "Skill":
                            used = (getattr(block, "input", {}) or {}).get("skill")
                            if used:
                                result.skills_used.append(str(used))
            elif isinstance(message, RateLimitEvent):
                # Plan-usage signal. The guard decides whether to halt.
                if guard is not None:
                    halt = guard.observe(message.rate_limit_info)
                    if halt:
                        result.rate_limited = halt
            elif isinstance(message, ResultMessage):
                result.cost_usd = message.total_cost_usd or 0.0
                result.turns = message.num_turns or 0
                result.subtype = message.subtype or ""
                if _looks_like_limit(message):
                    result.rate_limited = result.rate_limited or (
                        "plan usage limit reached mid-run; state is checkpointed "
                        "-- resume after your window resets"
                    )
    except Exception as exc:  # noqa: BLE001
        # query() raises after yielding an error result, so collected output is
        # still valid partial work.
        result.subtype = result.subtype or f"error:{type(exc).__name__}"
        if any(m in str(exc).lower() for m in _LIMIT_MARKERS):
            result.rate_limited = result.rate_limited or f"plan usage limit: {exc}"

    result.text = "\n".join(out).strip()
    return result


# -- tolerant JSON extraction ---------------------------------------------
# We ask for JSON in the prompt rather than using output_format, so the parser
# has to cope with fenced blocks, preamble prose, and trailing commentary.

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> dict[str, Any] | list[Any] | None:
    """Pull the first well-formed JSON object/array out of model output."""
    for candidate in _candidates(text):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def _candidates(text: str):
    for m in _FENCE.finditer(text):
        yield m.group(1).strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            yield text[start : end + 1]
    yield text.strip()


def json_or_empty(text: str, key: str) -> list[Any]:
    """Fetch a list field from model JSON, tolerating a bare list response."""
    data = extract_json(text)
    if isinstance(data, dict):
        value = data.get(key, [])
        return value if isinstance(value, list) else []
    if isinstance(data, list):
        return data
    return []


JSON_CONTRACT = (
    "Respond with a single JSON object inside one ```json fenced block. "
    "No prose before or after it. Every field in the schema is required; use "
    "an empty list or empty string rather than omitting a field."
)
