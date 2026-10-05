"""Command line entry point.

    python -m deepresearch "your question"
    python -m deepresearch --list
    python -m deepresearch --resume <run_id>
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .engine import Config, research
from .guardrails import WouldCostMoney
from .state import ResearchState
from .store import DEFAULT_DB, Store

OUT_DIR = Path.home() / "Documents" / "deepresearch" / "reports"


def _log(message: str) -> None:
    print(f"  {message}", flush=True)


def _write_outputs(state: ResearchState) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stem = OUT_DIR / f"{state.created_at[:10]}-{state.run_id}"
    report = stem.with_suffix(".md")
    report.write_text(
        f"# {state.question}\n\n{state.report}\n\n"
        f"---\n\n## Methodology\n\n"
        f"- Rounds: {state.round}\n"
        f"- Subquestions: {len(state.subquestions)}\n"
        f"- Claims: {len(state.claims)} "
        f"({sum(1 for c in state.claims if c.challenged)} challenged)\n"
        f"- Sources: {len(state.sources)}\n"
        f"- Contradictions: {len(state.contradictions)} "
        f"({len(state.unresolved_contradictions())} unresolved)\n"
        f"- Cost: ${state.total_cost_usd:.2f}\n"
        f"- Run id: `{state.run_id}`\n",
        encoding="utf-8",
    )
    # Full state alongside the report, so a run is auditable after the fact.
    stem.with_suffix(".json").write_text(
        state.model_dump_json(indent=2), encoding="utf-8"
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="deepresearch", description="Iterative multi-agent deep research."
    )
    parser.add_argument("question", nargs="?", help="the research question")
    parser.add_argument("--rounds", type=int, default=3, help="max research rounds")
    parser.add_argument("--workers", type=int, default=3, help="workers per round")
    parser.add_argument("--calls", type=int, default=20,
                        help="max agent calls (the real plan-usage dial)")
    parser.add_argument("--effort", default="high",
                        choices=["low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--model", default="sonnet",
                        help="sonnet (default, Pro-friendly) or opus")
    parser.add_argument("--max-utilization", type=float, default=0.80,
                        help="halt at this share of your plan window")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--resume", metavar="RUN_ID")
    parser.add_argument("--list", action="store_true", help="list past runs")
    args = parser.parse_args(argv)

    store = Store(args.db)

    if args.list:
        rows = store.list_runs()
        if not rows:
            print("no runs yet")
        for row in rows:
            print(f"{row['run_id']}  {row['created_at'][:16]}  "
                  f"{row['status']:9}  ${row['cost_usd']:6.2f}  {row['question'][:70]}")
        store.close()
        return 0

    if not args.question and not args.resume:
        parser.error("give a question, or --resume RUN_ID, or --list")

    cfg = Config(
        max_rounds=args.rounds,
        max_workers_per_round=args.workers,
        max_agent_calls=args.calls,
        orchestrator_effort=args.effort,
        orchestrator_model=args.model,
        worker_model=args.model,
        verifier_model=args.model,
        stop_at_utilization=args.max_utilization,
        on_event=_log,
    )

    print(f"deepresearch: {args.question or args.resume}")
    try:
        state = asyncio.run(
            research(args.question or "", cfg, store, resume_run_id=args.resume)
        )
    except WouldCostMoney as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("\ninterrupted -- state checkpointed, resume with --resume", file=sys.stderr)
        return 130
    finally:
        pass

    path = _write_outputs(state)
    print(f"\nreport: {path}")
    print(f"usage:  ~${state.total_cost_usd:.2f} est. at list price "
          f"(not billed -- included in your Claude plan)")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
