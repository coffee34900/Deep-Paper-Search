"""Research state: the serializable object the whole loop revolves around.

The central design claim of this tool is that research depth comes from
maintaining explicit state across many small model calls, rather than from
one large call. Everything the orchestrator knows lives here.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SourceKind(str, Enum):
    """Coarse source taxonomy. Drives credibility weighting."""

    PEER_REVIEWED = "peer_reviewed"
    PREPRINT = "preprint"
    OFFICIAL = "official"          # government, standards body, central bank
    PRIMARY_DATA = "primary_data"  # datasets, filings, raw statistics
    QUALITY_PRESS = "quality_press"
    TRADE_PRESS = "trade_press"
    CORPORATE = "corporate"        # vendor blog, press release, marketing
    BLOG = "blog"
    FORUM = "forum"
    UNKNOWN = "unknown"


class Source(BaseModel):
    url: str
    title: str = ""
    kind: SourceKind = SourceKind.UNKNOWN
    published: str | None = None
    # Set by the credibility policy, not by the model that found the source.
    credibility: float = 0.5
    retrieved_at: str = Field(default_factory=_now)

    @property
    def id(self) -> str:
        return hashlib.sha256(self.url.encode()).hexdigest()[:16]


class Claim(BaseModel):
    """One atomic, checkable assertion extracted from a source.

    Atomicity matters: "X grew 12% in 2024 because of Y" is two claims. Keeping
    them separate is what makes contradiction detection work at all.
    """

    id: str = ""
    subquestion_id: str
    text: str
    source_url: str
    # The model's own confidence that the source actually supports this claim.
    support: float = 0.5
    quote: str = ""
    round: int = 0
    # Set during verification.
    challenged: bool = False
    challenge_note: str = ""

    def assign_id(self) -> Claim:
        if not self.id:
            digest = hashlib.sha256(f"{self.text}|{self.source_url}".encode())
            self.id = digest.hexdigest()[:16]
        return self


class Contradiction(BaseModel):
    """Two claims that cannot both be true. The engine's main depth signal."""

    claim_a: str
    claim_b: str
    description: str
    salience: float = 0.5   # how much resolving this changes the conclusion
    resolved: bool = False
    resolution: str = ""
    # Round this conflict was first detected. The stopping rule uses its age
    # to tell "worth another round" from "nobody is going to settle this".
    first_seen_round: int = 0

    def age(self, current_round: int) -> int:
        return max(0, current_round - self.first_seen_round)


class SubQuestion(BaseModel):
    id: str
    question: str
    rationale: str = ""
    salience: float = 0.5
    status: Literal["open", "investigating", "saturated"] = "open"
    rounds_spent: int = 0


class RoundMetrics(BaseModel):
    """Per-round telemetry. The stopping rule reads this history."""

    round: int
    new_claims: int
    new_sources: int
    new_contradictions: int
    cost_usd: float = 0.0
    duration_s: float = 0.0


class ResearchState(BaseModel):
    run_id: str
    question: str
    created_at: str = Field(default_factory=_now)
    round: int = 0
    subquestions: list[SubQuestion] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    contradictions: list[Contradiction] = Field(default_factory=list)
    metrics: list[RoundMetrics] = Field(default_factory=list)
    report: str = ""
    total_cost_usd: float = 0.0

    # -- convenience views the orchestrator prompt is built from ------------

    def open_subquestions(self) -> list[SubQuestion]:
        return [s for s in self.subquestions if s.status != "saturated"]

    def unresolved_contradictions(self) -> list[Contradiction]:
        return [c for c in self.contradictions if not c.resolved]

    def known_claim_ids(self) -> set[str]:
        return {c.id for c in self.claims}

    def known_urls(self) -> set[str]:
        return {s.url for s in self.sources}

    def source_for(self, url: str) -> Source | None:
        return next((s for s in self.sources if s.url == url), None)

    def claims_for(self, subquestion_id: str) -> list[Claim]:
        return [c for c in self.claims if c.subquestion_id == subquestion_id]

    def digest(self, max_claims: int = 400) -> str:
        """Compact text view of state for the orchestrator.

        Deliberately excludes raw page content -- that is the whole point of
        the worker/orchestrator split.
        """
        lines = [f"QUESTION: {self.question}", f"ROUND: {self.round}", ""]
        lines.append("SUBQUESTIONS:")
        for s in self.subquestions:
            lines.append(
                f"  [{s.id}] ({s.status}, salience={s.salience:.2f}, "
                f"rounds={s.rounds_spent}) {s.question}"
            )
        lines.append("")
        lines.append(f"CLAIMS ({len(self.claims)} total):")
        for c in self.claims[:max_claims]:
            src = self.source_for(c.source_url)
            cred = f"{src.credibility:.2f}" if src else "?"
            kind = src.kind.value if src else "?"
            flag = " [CHALLENGED]" if c.challenged else ""
            lines.append(
                f"  [{c.id}] sq={c.subquestion_id} cred={cred} kind={kind} "
                f"support={c.support:.2f}{flag} :: {c.text}"
            )
        lines.append("")
        unresolved = self.unresolved_contradictions()
        lines.append(f"UNRESOLVED CONTRADICTIONS ({len(unresolved)}):")
        for c in unresolved:
            lines.append(
                f"  {c.claim_a} vs {c.claim_b} (salience={c.salience:.2f}): "
                f"{c.description}"
            )
        return "\n".join(lines)
