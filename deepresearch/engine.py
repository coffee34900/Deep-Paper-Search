"""The research loop.

plan -> [gather | gather | gather] -> reconcile -> (loop) -> verify -> report

The loop is driven here in Python rather than by a prompt, so fan-out width,
round count, and spend are code-level invariants instead of model decisions.
Workers run as independent `query()` calls so raw page content never reaches
the orchestrator's context -- only extracted claims do.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field

from . import credibility, prompts
from .guardrails import CallBudget, PlanGuard, require_subscription_auth
from .llm import (
    ORCHESTRATOR_MODEL,
    RESEARCH_SKILLS,
    VERIFIER_MODEL,
    WORKER_MODEL,
    AgentResult,
    extract_json,
    json_or_empty,
    run_agent,
)
from .state import (
    Claim,
    Contradiction,
    ResearchState,
    RoundMetrics,
    Source,
    SourceKind,
    SubQuestion,
)
from .store import Store

WEB_TOOLS = ["WebSearch", "WebFetch"]


@dataclass
class Config:
    """Run configuration.

    Defaults are tuned for a Claude Pro subscription: Sonnet, moderate effort,
    modest fan-out. Plan usage is measured in rolling 5-hour and 7-day
    windows, so the dial that matters is how many agent calls a run makes --
    not dollars, which are not the unit on a subscription.
    """

    max_rounds: int = 3
    max_workers_per_round: int = 3
    # Primary work cap: every worker / reconcile / verify / report step is one
    # agent call, each drawing on your plan windows.
    max_agent_calls: int = 20
    # Notional backstop computed from local list-price estimates. NOT a charge.
    notional_cap_usd: float | None = 12.0
    # Halt once this share of a plan window is used, leaving headroom for your
    # normal Claude usage.
    stop_at_utilization: float = 0.80

    worker_turns: int = 22
    worker_task_budget: int = 45_000
    orchestrator_effort: str = "high"
    worker_effort: str = "medium"

    orchestrator_model: str = ORCHESTRATOR_MODEL
    worker_model: str = WORKER_MODEL
    verifier_model: str = VERIFIER_MODEL

    # Bundled research skills loaded into each role. Skills are discovered
    # from the filesystem, so these names must exist in .claude/skills/.
    worker_skills: list[str] = field(default_factory=lambda: list(RESEARCH_SKILLS))
    verifier_skills: list[str] = field(
        default_factory=lambda: ["source-triage", "numeric-scrutiny"]
    )
    # Stopping-rule thresholds, read by should_continue().
    min_rounds: int = 2
    saturation_threshold: float = 0.85
    min_new_claims_per_round: int = 3
    # A contradiction at or above this salience can veto a stop...
    contradiction_veto_salience: float = 0.7
    # ...but only for this many rounds, after which it is treated as
    # unresolvable and reported as an open question instead of chased.
    contradiction_veto_rounds: int = 2
    on_event: object = field(default=None)  # callable(str) -> None, for progress

    # Run-scoped mutable state, populated by research(). Kept here so the
    # phase functions keep a single `cfg` parameter.
    guard: PlanGuard | None = field(default=None)
    budget: CallBudget | None = field(default=None)


def _emit(cfg: Config, message: str) -> None:
    if callable(cfg.on_event):
        cfg.on_event(message)


def _charge(cfg: Config, result: AgentResult) -> None:
    """Record one agent call against the work budget, and surface skill use."""
    if cfg.budget is not None:
        cfg.budget.spend(1, result.cost_usd)
    if result.rate_limited and cfg.guard is not None and not cfg.guard.halted:
        cfg.guard.halted_reason = result.rate_limited
    if result.skills_used:
        _emit(cfg, f"  skills: {', '.join(sorted(set(result.skills_used)))}")


# ---------------------------------------------------------------------------
# Stopping rule
# ---------------------------------------------------------------------------


def should_continue(state: ResearchState, cfg: Config, saturation: float) -> tuple[bool, str]:
    """Decide whether to run another research round.

    This is the single most consequential policy in the tool. Everything
    available to you:

      state.round                      -- rounds completed so far
      state.metrics                    -- list[RoundMetrics], one per round:
                                          .new_claims .new_sources
                                          .new_contradictions .cost_usd
      state.total_cost_usd             -- spend so far
      state.open_subquestions()        -- not yet marked saturated
      state.unresolved_contradictions()-- each has .salience in [0, 1]
      saturation                       -- the orchestrator's own 0-1 estimate
      cfg.max_rounds, cfg.budget, cfg.guard,
      cfg.saturation_threshold, cfg.min_new_claims_per_round

    Return (continue?, reason). The reason string is logged and ends up in the
    report's methodology note, so make it specific.

    DEFAULT POLICY (written by Claude; swap it for your own judgement).

    The trade-off: yield-based stopping is efficient but blind to importance --
    a round can return almost nothing new while a high-salience contradiction
    sits unresolved, and stopping there ships a confident report built on a
    known conflict. Salience-based stopping is thorough but can burn the whole
    budget chasing a disagreement no available source can settle.

    This resolution: salience overrides yield, but only while the conflict is
    young. A high-salience contradiction vetoes a stop for up to
    `contradiction_veto_rounds` rounds; past that it is treated as genuinely
    unresolvable and reported as an open question rather than chased. That
    keeps the veto from becoming an infinite loop, which is the failure mode of
    a pure salience rule.
    """
    last = state.metrics[-1] if state.metrics else None

    # Hard floor. One round is never enough to have re-planned from evidence,
    # which is the entire premise of the loop.
    if state.round < cfg.min_rounds:
        return True, f"round {state.round} < min_rounds {cfg.min_rounds}"

    # Hard ceilings. Checked here too (not just in the driver) so the reason
    # string is specific in the report.
    if state.round >= cfg.max_rounds:
        return False, f"max_rounds {cfg.max_rounds} reached"
    if cfg.guard is not None and cfg.guard.halted:
        return False, str(cfg.guard.halted_reason)
    if cfg.budget is not None:
        # Reserve calls for verify + report, which must still run.
        reserved = 2
        if cfg.budget.remaining() <= reserved:
            return False, (
                f"{cfg.budget.summary()} -- stopping with {reserved} calls "
                f"reserved to verify and write the report"
            )
        spent, why = cfg.budget.exhausted()
        if spent:
            return False, why

    # Nothing left to investigate -- no amount of budget helps.
    if not state.open_subquestions():
        return False, "all subquestions marked saturated"

    low_yield = last is not None and last.new_claims < cfg.min_new_claims_per_round
    saturated = saturation >= cfg.saturation_threshold

    # The veto: a load-bearing conflict still young enough to be worth another
    # round overrides both low yield and self-reported saturation.
    pressing = [
        c
        for c in state.unresolved_contradictions()
        if c.salience >= cfg.contradiction_veto_salience
        and c.age(state.round) < cfg.contradiction_veto_rounds
    ]
    if pressing and (low_yield or saturated):
        top = max(pressing, key=lambda c: c.salience)
        return True, (
            f"{len(pressing)} unresolved contradiction(s) at salience "
            f">={cfg.contradiction_veto_salience} override the stop signal; "
            f"highest ({top.salience:.2f}, open {top.age(state.round)} round(s)): "
            f"{top.description[:120]}"
        )

    # Both independent stop signals agreeing is the clean exit.
    if saturated and low_yield:
        return False, (
            f"saturation {saturation:.2f} >= {cfg.saturation_threshold} and "
            f"only {last.new_claims} new claims last round"
        )

    # Two consecutive low-yield rounds: the search has stopped finding things
    # even if the orchestrator is not ready to call it saturated. Trust the
    # measurement over the self-report.
    recent = state.metrics[-2:]
    if len(recent) == 2 and all(
        m.new_claims < cfg.min_new_claims_per_round for m in recent
    ):
        return False, (
            f"two consecutive low-yield rounds "
            f"({recent[0].new_claims}, {recent[1].new_claims} new claims)"
        )

    if saturated:
        return True, (
            f"saturation {saturation:.2f} is high but last round still "
            f"yielded {last.new_claims if last else 0} new claims"
        )
    return True, (
        f"saturation {saturation:.2f} < {cfg.saturation_threshold}, "
        f"{len(state.open_subquestions())} subquestion(s) open"
    )


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------


async def plan(state: ResearchState, cfg: Config) -> AgentResult:
    """Decompose the question into subquestions."""
    _emit(cfg, "planning")
    result = await run_agent(
        f"Research question:\n\n{state.question}\n\nDecompose it.",
        system_prompt=prompts.PLANNER,
        model=cfg.orchestrator_model,
        effort=cfg.orchestrator_effort,
        max_turns=4,
        skills=["search-strategy"],
        guard=cfg.guard,
    )
    _charge(cfg, result)
    for raw in json_or_empty(result.text, "subquestions"):
        if not isinstance(raw, dict) or not raw.get("question"):
            continue
        state.subquestions.append(
            SubQuestion(
                id=str(raw.get("id") or f"sq{len(state.subquestions) + 1}"),
                question=str(raw["question"]),
                rationale=str(raw.get("rationale", "")),
                salience=float(raw.get("salience", 0.5) or 0.5),
            )
        )
    state.total_cost_usd += result.cost_usd
    _emit(cfg, f"planned {len(state.subquestions)} subquestions")
    return result


async def gather_one(
    state: ResearchState, sq: SubQuestion, cfg: Config
) -> tuple[SubQuestion, AgentResult]:
    """Investigate one subquestion in an isolated context."""
    known = "\n".join(f"- {c.text}" for c in state.claims_for(sq.id)[:40])
    already = (
        f"\n\nClaims already gathered for this subquestion -- do NOT repeat "
        f"them, extend or challenge them:\n{known}"
        if known
        else ""
    )
    seen_urls = list(state.known_urls())[:60]
    visited = (
        "\n\nAlready-read URLs (prefer new ones):\n"
        + "\n".join(f"- {u}" for u in seen_urls)
        if seen_urls
        else ""
    )
    prompt = (
        f"Overall research question (context only):\n{state.question}\n\n"
        f"YOUR SUBQUESTION [{sq.id}]:\n{sq.question}\n\n"
        f"Why it matters: {sq.rationale}"
        f"{already}{visited}"
    )
    result = await run_agent(
        prompt,
        system_prompt=prompts.WORKER,
        model=cfg.worker_model,
        allowed_tools=WEB_TOOLS,
        effort=cfg.worker_effort,
        max_turns=cfg.worker_turns,
        task_budget_tokens=cfg.worker_task_budget,
        skills=cfg.worker_skills,
        guard=cfg.guard,
    )
    return sq, result


def _ingest(state: ResearchState, sq: SubQuestion, result: AgentResult) -> tuple[int, int]:
    """Fold one worker's output into state, scoring and deduplicating."""
    payload = extract_json(result.text)
    if not isinstance(payload, dict):
        return 0, 0

    new_sources = 0
    for raw in payload.get("sources") or []:
        if not isinstance(raw, dict) or not raw.get("url"):
            continue
        url = str(raw["url"])
        if url in state.known_urls():
            continue
        try:
            kind = SourceKind(str(raw.get("kind", "unknown")))
        except ValueError:
            kind = SourceKind.UNKNOWN
        source = Source(
            url=url,
            title=str(raw.get("title", "")),
            kind=kind,
            published=raw.get("published") or None,
        )
        credibility.score(source)  # normalises kind and sets the weight
        state.sources.append(source)
        new_sources += 1

    new_claims = 0
    existing = state.known_claim_ids()
    for raw in payload.get("claims") or []:
        if not isinstance(raw, dict) or not raw.get("text"):
            continue
        claim = Claim(
            subquestion_id=sq.id,
            text=str(raw["text"]),
            source_url=str(raw.get("source_url", "")),
            quote=str(raw.get("quote", "")),
            support=float(raw.get("support", 0.5) or 0.5),
            round=state.round,
        ).assign_id()
        if claim.id in existing:
            continue
        # A claim whose source was never declared is unverifiable -- keep it
        # but register the source as unknown so credibility reflects that.
        if claim.source_url and claim.source_url not in state.known_urls():
            orphan = Source(url=claim.source_url, kind=SourceKind.UNKNOWN)
            credibility.score(orphan)
            state.sources.append(orphan)
        state.claims.append(claim)
        existing.add(claim.id)
        new_claims += 1

    return new_claims, new_sources


async def gather(state: ResearchState, cfg: Config) -> tuple[int, int, float]:
    """Fan out across the highest-salience open subquestions, concurrently."""
    width = cfg.max_workers_per_round
    if cfg.budget is not None:
        # Keep 3 calls back for reconcile + verify + report.
        width = min(width, max(0, cfg.budget.remaining() - 3))
    targets = sorted(
        state.open_subquestions(), key=lambda s: -s.salience
    )[:width]
    if not targets:
        return 0, 0, 0.0

    _emit(cfg, f"round {state.round}: {len(targets)} workers -> "
               + ", ".join(t.id for t in targets))

    results = await asyncio.gather(
        *(gather_one(state, sq, cfg) for sq in targets), return_exceptions=True
    )

    total_claims = total_sources = 0
    cost = 0.0
    for item in results:
        if isinstance(item, BaseException):
            _emit(cfg, f"  worker failed: {item}")
            continue
        sq, result = item
        cost += result.cost_usd
        _charge(cfg, result)
        claims, sources = _ingest(state, sq, result)
        sq.rounds_spent += 1
        sq.status = "investigating"
        total_claims += claims
        total_sources += sources
        _emit(cfg, f"  {sq.id}: +{claims} claims, +{sources} sources "
                   f"(${result.cost_usd:.2f}, {result.turns} turns)")

    state.total_cost_usd += cost
    return total_claims, total_sources, cost


async def reconcile(state: ResearchState, cfg: Config) -> tuple[int, float, str]:
    """Orchestrator reads claims only, finds conflicts, re-plans."""
    _emit(cfg, f"round {state.round}: reconciling {len(state.claims)} claims")
    result = await run_agent(
        state.digest(),
        system_prompt=prompts.RECONCILER,
        model=cfg.orchestrator_model,
        effort=cfg.orchestrator_effort,
        max_turns=4,
        skills=["numeric-scrutiny"],
        guard=cfg.guard,
    )
    _charge(cfg, result)
    state.total_cost_usd += result.cost_usd

    payload = extract_json(result.text)
    if not isinstance(payload, dict):
        return 0, 0.0, ""

    seen = {(c.claim_a, c.claim_b) for c in state.contradictions}
    new_contradictions = 0
    for raw in payload.get("contradictions") or []:
        if not isinstance(raw, dict):
            continue
        key = (str(raw.get("claim_a", "")), str(raw.get("claim_b", "")))
        if key in seen or key[::-1] in seen or not all(key):
            continue
        state.contradictions.append(
            Contradiction(
                claim_a=key[0],
                claim_b=key[1],
                description=str(raw.get("description", "")),
                salience=float(raw.get("salience", 0.5) or 0.5),
                first_seen_round=state.round,
            )
        )
        seen.add(key)
        new_contradictions += 1

    for sq_id in payload.get("saturated_subquestion_ids") or []:
        for sq in state.subquestions:
            if sq.id == str(sq_id):
                sq.status = "saturated"

    existing_ids = {s.id for s in state.subquestions}
    for raw in payload.get("new_subquestions") or []:
        if not isinstance(raw, dict) or not raw.get("question"):
            continue
        sq_id = str(raw.get("id") or f"sq{len(state.subquestions) + 1}")
        if sq_id in existing_ids:
            sq_id = f"sq{len(state.subquestions) + 1}"
        state.subquestions.append(
            SubQuestion(
                id=sq_id,
                question=str(raw["question"]),
                rationale=str(raw.get("rationale", "")),
                salience=float(raw.get("salience", 0.5) or 0.5),
            )
        )
        existing_ids.add(sq_id)

    saturation = float(payload.get("saturation", 0.0) or 0.0)
    assessment = str(payload.get("assessment", ""))
    _emit(cfg, f"  +{new_contradictions} contradictions, "
               f"saturation={saturation:.2f}")
    return new_contradictions, saturation, assessment


async def verify(state: ResearchState, cfg: Config) -> AgentResult:
    """Adversarial pass with fresh context. The highest-value single step."""
    _emit(cfg, "adversarial verification")
    result = await run_agent(
        state.digest(),
        system_prompt=prompts.VERIFIER,
        model=cfg.verifier_model,
        allowed_tools=WEB_TOOLS,
        effort=cfg.orchestrator_effort,
        max_turns=20,
        task_budget_tokens=40_000,
        skills=cfg.verifier_skills,
        guard=cfg.guard,
    )
    _charge(cfg, result)
    state.total_cost_usd += result.cost_usd

    by_id = {c.id: c for c in state.claims}
    challenged = 0
    for raw in json_or_empty(result.text, "challenges"):
        if not isinstance(raw, dict):
            continue
        claim = by_id.get(str(raw.get("claim_id", "")))
        if claim is None:
            continue
        claim.challenged = True
        claim.challenge_note = f"[{raw.get('pattern', 'other')}] {raw.get('note', '')}"
        challenged += 1
    _emit(cfg, f"  {challenged} claims challenged")
    return result


async def write_report(
    state: ResearchState, cfg: Config, verification: AgentResult
) -> AgentResult:
    _emit(cfg, "writing report")
    prompt = (
        f"{state.digest()}\n\n"
        f"=== ADVERSARIAL VERIFICATION ===\n{verification.text}\n\n"
        f"=== SOURCES ===\n"
        + "\n".join(
            f"- [{s.kind.value} cred={s.credibility:.2f}] {s.title or s.url} -- {s.url}"
            for s in sorted(state.sources, key=lambda x: -x.credibility)
        )
        + f"\n\nWrite the final report answering: {state.question}"
    )
    result = await run_agent(
        prompt,
        system_prompt=prompts.REPORTER,
        model=cfg.orchestrator_model,
        effort=cfg.orchestrator_effort,
        max_turns=4,
        guard=cfg.guard,
    )
    _charge(cfg, result)
    state.report = result.text
    state.total_cost_usd += result.cost_usd
    return result


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


async def research(
    question: str,
    cfg: Config | None = None,
    store: Store | None = None,
    resume_run_id: str | None = None,
) -> ResearchState:
    cfg = cfg or Config()
    # Refuse to start if the environment would route this off the Claude
    # subscription and onto per-token billing. This is the only path by which
    # the tool could actually charge money, so it is checked before any call.
    require_subscription_auth()

    if cfg.guard is None:
        cfg.guard = PlanGuard(stop_at_utilization=cfg.stop_at_utilization)
    if cfg.budget is None:
        cfg.budget = CallBudget(
            max_calls=cfg.max_agent_calls, notional_cap_usd=cfg.notional_cap_usd
        )

    store = store or Store()
    owns_store = store is not None

    if resume_run_id:
        loaded = store.load(resume_run_id)
        if loaded is None:
            raise ValueError(f"no such run: {resume_run_id}")
        state = loaded
        _emit(cfg, f"resuming {state.run_id} at round {state.round}")
    else:
        state = ResearchState(run_id=uuid.uuid4().hex[:12], question=question)
        store.save(state)
        await plan(state, cfg)
        store.save(state)

    saturation = 0.0
    while True:
        if state.round > 0:
            keep_going, reason = should_continue(state, cfg, saturation)
            _emit(cfg, f"stopping rule: {'continue' if keep_going else 'stop'} -- {reason}")
            if not keep_going:
                break
        if state.round >= cfg.max_rounds:
            _emit(cfg, "stopping: max_rounds reached")
            break
        if cfg.guard is not None and cfg.guard.halted:
            _emit(cfg, f"stopping: {cfg.guard.halted_reason}")
            break
        if cfg.budget is not None:
            spent, why = cfg.budget.exhausted()
            if spent:
                _emit(cfg, f"stopping: {why}")
                break

        state.round += 1
        started = time.monotonic()
        claims, sources, cost = await gather(state, cfg)
        contradictions, saturation, _ = await reconcile(state, cfg)
        state.metrics.append(
            RoundMetrics(
                round=state.round,
                new_claims=claims,
                new_sources=sources,
                new_contradictions=contradictions,
                cost_usd=cost,
                duration_s=round(time.monotonic() - started, 1),
            )
        )
        _emit(cfg, f"  {cfg.budget.summary()}"
                   + (f"  |  {cfg.guard.summary()}" if cfg.guard.history else ""))
        store.save(state)

    verification = await verify(state, cfg)
    store.save(state)
    await write_report(state, cfg, verification)
    store.save(state)
    store.mark_status(state.run_id, "complete")
    if owns_store:
        pass  # caller owns lifecycle; CLI closes it
    return state
