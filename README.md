# deepresearch

Iterative, multi-agent deep research on top of the Claude Agent SDK.

Runs on your Claude Code subscription auth — no API key needed.

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

Type a question, pick a depth preset, hit **Research**. Three tabs:

- **Progress** — live per-worker log (claims found, cost, turns) as the loop runs
- **Report** — the finished report, with a stats line for claims / challenged
  claims / sources / unresolved conflicts / spend
- **History** — every past run; double-click to reopen a report, or select and
  **Resume run** to continue an interrupted one

**Stop** cancels mid-run; state is checkpointed, so resume it from History.

## Use — CLI

```bash
python -m deepresearch "your question"                  # run
python -m deepresearch "..." --rounds 8 --budget 60     # go deeper
python -m deepresearch --list                           # past runs
python -m deepresearch --resume <run_id>                # resume a run
```

Reports land in `reports/` as markdown plus the full state JSON, so any run is
auditable after the fact. State is checkpointed to SQLite
(`~/.deepresearch/research.db`) after every phase — Ctrl-C loses at most one
phase.

## Cost and time

Depth is bought with tokens. Defaults: Opus 5 everywhere, `effort="max"` on the
orchestrator and verifier, 6 concurrent workers per round, 5 rounds. Expect
tens of minutes and a meaningful spend per question. `--budget` is a hard cap
(`max_budget_usd`, enforced by the SDK across subagent spend); `--workers` and
`--rounds` are the other two dials.

Turn it down for routine questions: `--effort high --rounds 2 --workers 3`.

## Layout

| File | Role |
|---|---|
| `state.py` | `ResearchState`, `Claim`, `Source`, `Contradiction` — the object the loop revolves around |
| `store.py` | SQLite persistence, resume, cross-run claim/source reuse, page cache |
| `llm.py` | Single wrapper over the SDK's `query()`; cost accounting, tolerant JSON parsing |
| `prompts.py` | The five role prompts — the real intellectual content |
| `credibility.py` | Deterministic source weighting and corroboration |
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
