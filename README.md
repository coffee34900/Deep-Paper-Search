# deepresearch

Iterative, multi-agent deep research on top of the Claude Agent SDK.

Runs entirely on your Claude subscription. No API key, no per-token billing,
no usage credits — enforced in code, not just by convention (see
[Subscription-only](#subscription-only)).

## Why this goes deeper than off-the-shelf research tools

Most "deep research" features are **single-pass fan-out**: decompose once →
search in parallel → stuff everything into one context → summarize. Three
structural ceilings follow:

1. the plan is written before any evidence exists, so it can't react;
2. all sources are weighted equally, so a vendor blog and a journal article
   carry the same force;
3. depth is a fixed constant, not a function of how contested a subquestion
   turns out to be.

This tool is built as a **research state machine** instead. An explicit,
serializable `ResearchState` (open subquestions, atomic claims with source
pointers, detected contradictions, credibility weights, coverage gaps) is
carried across rounds:

```
plan ──> [worker | worker | worker]  ──> reconcile ──┐
            (isolated contexts,          (claims only,  │  re-plan from
             web search + fetch)           never raw pages)  updated state
                                                      │
         ◄────────────────────────────────────────────┘
                        │ stopping rule
                        ▼
          adversarial verify (fresh context) ──> report
```

### Research skills

The official [anthropics/skills](https://github.com/anthropics/skills)
repository has no web-research, source-evaluation, or fact-checking skill — its
skills are document and creative ones. So this project bundles four of its own
under `.claude/skills/`, loaded into the roles that need them:

| Skill | Loaded into | What it encodes |
| --- | --- | --- |
| `search-strategy` | planner, workers | Query ladders (naive → technical → primary-source → dissent-targeted), operators, how to find the one original behind several agreeing sources, stop conditions |
| `source-triage` | workers, verifier | Evidence-type taxonomy, URL-path and funding checks that override a source's self-presentation, independence testing, per-domain staleness |
| `claim-extraction` | workers | Atomicity rules, quote discipline, the hedge-drift table (`may` → `does`), support calibration, deliberate disconfirmation |
| `numeric-scrutiny` | workers, reconciler, verifier | Denominators, base rates, definitional swaps, selection effects, forecast provenance, overlapping intervals vs. real conflicts |

Skills are discovered only through filesystem setting sources, so `llm.py` runs
agents with `cwd` pinned to the package root and `setting_sources=["project"]`
— picking up these bundled skills while still ignoring your personal
`~/.claude` config, which keeps runs reproducible wherever you launch from.

Four things do the actual work:

- **Worker/orchestrator split.** Workers read pages and return *atomic sourced
  claims*. Raw page text never enters the orchestrator's context. This is what
  lets the run go wide without context rot.
- **Re-planning from evidence.** Each `reconcile` pass proposes new
  subquestions the evidence itself raised. A round that proposes none has
  either finished or stopped looking.
- **Deterministic credibility.** Source weight is computed in `credibility.py`,
  not asked of a model — domain patterns promote (`arxiv.org` → preprint),
  path markers demote (a `/blog/` post can't be `official`), and per-kind
  half-lives decay stale evidence. Two runs weigh the same source identically,
  and you can argue with the policy by reading it.
- **Adversarial verification.** A fresh-context pass that *attacks* the claim
  set, hunting named failure patterns (circular sourcing, quote drift,
  definitional swap, missing denominator) rather than "checking logic". This is
  the single highest-value step and the one no off-the-shelf tool does.

## Install

```bash
pip install -r requirements.txt
```

## Use — GUI

```bash
python -m deepresearch.gui
```

Type a question, pick **Light** or **Thorough**, hit **Research**. Three tabs:

- **Progress** — live per-worker log (claims found, cost, turns) as the loop runs
- **Report** — the finished report, with a stats line for claims / challenged
  claims / sources / unresolved conflicts / estimated plan usage
- **History** — every past run; double-click to reopen a report, or select and
  **Resume run** to continue an interrupted one

**Stop** cancels mid-run; state is checkpointed, so resume it from History.

## Use — CLI

```bash
python -m deepresearch "your question"                  # run
python -m deepresearch "..." --rounds 4 --calls 32      # go deeper
python -m deepresearch --list                           # past runs
python -m deepresearch --resume <run_id>                # resume a run
```

Reports land in `reports/` as markdown plus the full state JSON, so any run is
auditable after the fact. State is checkpointed to SQLite
(`~/.deepresearch/research.db`) after every phase — Ctrl-C loses at most one
phase.

## Subscription-only

Two separate risks are closed in `guardrails.py`, because they fail differently:

**1. Billed-API risk.** The Agent SDK resolves credentials in a fixed order, and
`ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` **outrank** your subscription
login. If either is set, every request is billed per-token to that key —
silently. `require_subscription_auth()` refuses to start in that case, and is
called before any model request. Cloud-provider routing vars
(`CLAUDE_CODE_USE_BEDROCK`, `ANTHROPIC_BASE_URL`, …) are refused the same way,
and every agent subprocess gets a scrubbed environment as a second layer.

**2. Overage risk.** Past your plan limit, Anthropic offers paid *usage
credits*. `PlanGuard` watches the SDK's `RateLimitEvent` and halts the moment
the plan window is exhausted — so credits are never consumed even when
available and enabled. It also halts at `stop_at_utilization` (default 80%) so
the research loop leaves headroom for your normal Claude use. Halting, rather
than waiting, is deliberate: waiting out a 5-hour window would hang the run for
hours, and state is checkpointed so you just resume after the reset.

## Cost and time

**Nothing here is billed.** Plan usage is metered in rolling 5-hour and 7-day
windows, not dollars, so the dial that matters is **agent calls** — each
worker, reconcile, verify, and report step is one call, and each draws on those
windows. `--calls` caps them.

The `~$N est.` figure the tool reports is Claude Code's local estimate at list
price. It is **not a charge**; it exists only as a token-volume proxy and a
backstop (`notional_cap_usd`) against a pathological run.

Defaults are tuned for Pro: **Sonnet** everywhere, `effort="high"` on the
orchestrator, 3 workers, 3 rounds, 20 calls. Sonnet is deliberate — Opus has a
separate, smaller weekly bucket on a plan, so burning it inside a research loop
would lock you out of Opus everywhere else, including normal Claude use. Pass
`--model opus` only if you have headroom to spare.

Lighter still: `--rounds 2 --workers 2 --calls 10 --effort medium`.

## Layout

| File | Role |
|---|---|
| `state.py` | `ResearchState`, `Claim`, `Source`, `Contradiction` — the object the loop revolves around |
| `store.py` | SQLite persistence, resume, cross-run claim/source reuse, page cache |
| `llm.py` | Single wrapper over the SDK's `query()`; cost accounting, tolerant JSON parsing |
| `prompts.py` | The five role prompts — the real intellectual content |
| `credibility.py` | Deterministic source weighting and corroboration |
| `guardrails.py` | Subscription-only enforcement, plan-limit guard, call budget |
| `engine.py` | The loop: `plan` → `gather` → `reconcile` → `verify` → `report` |
| `cli.py` | Command-line entry point |
| `gui.py` | Tkinter GUI (worker thread + queue, so Tk stays on one thread) |

## The stopping rule

`engine.should_continue()` is the most consequential policy in the tool and
lives in one function so it is easy to replace. The shipped default:

- hard floor of `min_rounds` (2) — one round means nothing was ever re-planned
- stop when **both** independent signals agree: self-reported saturation
  `>= 0.85` *and* fewer than 3 new claims last round
- stop on two consecutive low-yield rounds even if the orchestrator is not
  ready to call it saturated — trust the measurement over the self-report
- an unresolved contradiction at salience `>= 0.7` **vetoes** a stop, but only
  for 2 rounds; past that it is treated as genuinely unresolvable and reported
  as an open question rather than chased until the budget is gone
- stop at 90% of budget, leaving headroom to verify and write the report

The veto expiry is the load-bearing detail: a pure salience rule never
terminates on a disagreement no source can settle.
