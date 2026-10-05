"""Subscription-only guardrails.

This tool is for a Claude Pro subscription with ZERO extra spend. Two separate
risks have to be closed, because they fail in different ways:

1. BILLED-API RISK. The Agent SDK resolves credentials in a fixed order, and
   `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` *outrank* your subscription
   login. If either is set in the environment, every request this tool makes
   is billed per-token to that key instead of drawing on your plan -- silently,
   with no warning. `require_subscription_auth()` refuses to start in that
   case. This is the only way the tool could actually charge you money.

2. OVERAGE RISK. Past your plan's limit, Anthropic offers "usage credits",
   which are paid. They are off unless you enable them on claude.ai, but if
   they ever are on, continuing past the plan limit spends real money.
   `PlanGuard` halts the run the moment the plan limit is reached, so credits
   are never consumed even when available.

Plan usage itself is measured in rolling windows (a 5-hour window and a 7-day
window, with separate weekly buckets per model family) -- not dollars. The
dollar figure Claude Code reports to subscribers is a local estimate at list
price and is explicitly not a bill. This module treats it as a token-volume
proxy only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Env vars that divert requests off your subscription and onto paid billing.
BILLING_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
)

# Env vars that route to a cloud provider, billed to that cloud account.
PROVIDER_ENV_VARS = (
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "ANTHROPIC_BASE_URL",
)


class WouldCostMoney(RuntimeError):
    """Raised instead of starting a run that would be billed."""


def billing_risks() -> list[str]:
    """Environment settings that would move spend off the subscription."""
    risks = []
    for var in BILLING_ENV_VARS:
        if os.environ.get(var):
            risks.append(
                f"{var} is set -- it outranks your subscription login, so every "
                f"request would be billed per-token to that key"
            )
    for var in PROVIDER_ENV_VARS:
        if os.environ.get(var):
            risks.append(
                f"{var} is set -- requests would route to a cloud provider and "
                f"be billed to that account, not your plan"
            )
    return risks


def require_subscription_auth() -> None:
    """Refuse to run unless requests will draw on the Claude subscription."""
    risks = billing_risks()
    if risks:
        raise WouldCostMoney(
            "refusing to start: this run would not be covered by your Claude "
            "subscription.\n\n"
            + "\n".join(f"  - {r}" for r in risks)
            + "\n\nUnset the variable(s) above and make sure Claude Code is "
            "logged in with your Claude account (`claude` then `/login`)."
        )


# Environment handed to every agent subprocess. Belt-and-braces: even if the
# parent process picks up a key later, the child never sees one.
def scrubbed_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {var: "" for var in BILLING_ENV_VARS}
    env.update(extra or {})
    return env


@dataclass
class PlanGuard:
    """Tracks plan-limit signals and decides when to halt.

    Halting (rather than waiting or retrying) is deliberate. Waiting out a
    5-hour window inside a research loop would leave the run hanging for
    hours; retrying risks tipping into paid usage credits. Stopping keeps the
    checkpointed state resumable, so you continue after the window resets.
    """

    # Halt once this share of a window is consumed, leaving headroom for your
    # normal Claude usage. The research loop is not the only thing drawing on
    # your plan.
    stop_at_utilization: float = 0.80
    # Never spend usage credits, even if the account has them enabled.
    allow_overage: bool = False

    status: str = "allowed"
    limit_type: str | None = None
    utilization: float | None = None
    resets_at: int | None = None
    overage_available: bool = False
    halted_reason: str | None = None
    history: list[str] = field(default_factory=list)

    def observe(self, info: object) -> str | None:
        """Fold in a RateLimitInfo from the SDK. Returns a reason if we halt."""
        self.status = str(getattr(info, "status", "allowed") or "allowed")
        self.limit_type = getattr(info, "rate_limit_type", None)
        self.utilization = getattr(info, "utilization", None)
        self.resets_at = getattr(info, "resets_at", None)
        overage = getattr(info, "overage_status", None)
        self.overage_available = overage in ("allowed", "allowed_warning")

        note = self.summary()
        if note not in self.history:
            self.history.append(note)

        # Plan window exhausted. Stop here -- continuing would either fail or
        # start drawing on paid usage credits.
        if self.status == "rejected":
            extra = (
                " Usage credits appear available, but this tool will not spend "
                "them." if self.overage_available and not self.allow_overage else ""
            )
            self.halted_reason = (
                f"plan limit reached ({self.limit_type or 'usage window'})"
                f"{self._reset_phrase()}.{extra} State is checkpointed -- resume "
                f"the run after it resets."
            )
            return self.halted_reason

        if (
            self.utilization is not None
            and self.utilization >= self.stop_at_utilization
        ):
            self.halted_reason = (
                f"stopping at {self.utilization:.0%} of your "
                f"{self.limit_type or 'usage'} window (threshold "
                f"{self.stop_at_utilization:.0%}), leaving headroom for your "
                f"other Claude usage{self._reset_phrase()}. Resume later to "
                f"continue."
            )
            return self.halted_reason

        return None

    @property
    def halted(self) -> bool:
        return self.halted_reason is not None

    def _reset_phrase(self) -> str:
        if not self.resets_at:
            return ""
        try:
            when = datetime.fromtimestamp(self.resets_at, tz=timezone.utc)
            return f"; resets {when.astimezone():%Y-%m-%d %H:%M}"
        except (OverflowError, OSError, ValueError):
            return ""

    def summary(self) -> str:
        parts = [f"plan: {self.status}"]
        if self.limit_type:
            parts.append(str(self.limit_type))
        if self.utilization is not None:
            parts.append(f"{self.utilization:.0%} used")
        if self.resets_at:
            parts.append(f"resets{self._reset_phrase()[1:]}")
        return "  |  ".join(parts)


@dataclass
class CallBudget:
    """Caps the amount of work a run may do, in agent calls.

    This is the user-facing dial, because it is the thing that actually
    consumes plan usage: every worker, reconcile, verify and report step is one
    agent call, each sending its own requests. Dollars are not the unit on a
    subscription, so the tool does not pretend they are.
    """

    max_calls: int = 24
    used: int = 0
    # Notional token-volume backstop, computed from local list-price estimates.
    # NOT a charge -- subscription usage is included in the plan. It exists
    # only to catch a pathological run that stays under max_calls while
    # consuming a huge amount of context.
    notional_cap_usd: float | None = 15.0
    notional_used_usd: float = 0.0

    def remaining(self) -> int:
        return max(0, self.max_calls - self.used)

    def spend(self, calls: int = 1, notional_usd: float = 0.0) -> None:
        self.used += calls
        self.notional_used_usd += notional_usd

    def exhausted(self) -> tuple[bool, str]:
        if self.used >= self.max_calls:
            return True, f"call budget spent ({self.used}/{self.max_calls} agent calls)"
        if (
            self.notional_cap_usd is not None
            and self.notional_used_usd >= self.notional_cap_usd
        ):
            return True, (
                f"notional token-volume cap reached "
                f"(~${self.notional_used_usd:.2f} of ${self.notional_cap_usd:.2f} "
                f"estimated at list price -- not a charge, your plan covers this)"
            )
        return False, ""

    def summary(self) -> str:
        cap = (
            f"  |  ~${self.notional_used_usd:.2f} est."
            if self.notional_cap_usd is not None
            else ""
        )
        return f"{self.used}/{self.max_calls} agent calls{cap}"
