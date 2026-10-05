"""Tkinter GUI.

    python -m deepresearch.gui

Threading model: the research loop is async and long-running, so it runs on a
worker thread with its own asyncio event loop. It never touches Tk widgets --
it pushes events onto a queue.Queue, and Tk drains that queue from the main
thread via `after()`. This is the standard way to keep Tk safe, since Tk
widgets may only be touched from the thread that created them.

SQLite connections are thread-bound, so the worker opens its own Store and the
history tab opens a separate one on the main thread.
"""

from __future__ import annotations

import asyncio
import queue
import threading
import tkinter as tk
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .cli import _write_outputs
from .engine import Config, research
from .guardrails import WouldCostMoney, billing_risks
from .state import ResearchState
from .store import DEFAULT_DB, Store

PAD = 8


@dataclass
class Event:
    """Message from the worker thread to the UI."""

    kind: str  # "log" | "done" | "error" | "state"
    text: str = ""
    state: ResearchState | None = None


class ResearchWorker:
    """Runs one research job on a background thread with its own event loop."""

    def __init__(self, outbox: queue.Queue[Event]) -> None:
        self.outbox = outbox
        self.thread: threading.Thread | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.task: asyncio.Task | None = None

    @property
    def busy(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, question: str, cfg: Config, db: str, resume: str | None) -> None:
        cfg.on_event = lambda msg: self.outbox.put(Event("log", msg))
        self.thread = threading.Thread(
            target=self._run, args=(question, cfg, db, resume), daemon=True
        )
        self.thread.start()

    def cancel(self) -> None:
        """Cancel the in-flight task from the UI thread.

        `call_soon_threadsafe` is required -- touching the task directly from
        the Tk thread would race the event loop.
        """
        if self.loop and self.task and not self.task.done():
            self.loop.call_soon_threadsafe(self.task.cancel)

    def _run(self, question: str, cfg: Config, db: str, resume: str | None) -> None:
        store = None
        try:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            store = Store(db)  # opened on this thread; sqlite is thread-bound
            self.task = self.loop.create_task(
                research(question, cfg, store, resume_run_id=resume)
            )
            state = self.loop.run_until_complete(self.task)
            self.outbox.put(Event("done", state=state))
        except asyncio.CancelledError:
            self.outbox.put(Event("error", "cancelled -- state was checkpointed, "
                                           "resume it from the History tab"))
        except WouldCostMoney as exc:
            self.outbox.put(Event("error", str(exc)))
        except Exception as exc:  # noqa: BLE001
            self.outbox.put(Event("error", f"{type(exc).__name__}: {exc}"))
        finally:
            if store is not None:
                store.close()
            if self.loop is not None:
                self.loop.close()


class App(ttk.Frame):
    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master, padding=PAD)
        self.master.title("deepresearch")
        self.master.geometry("1040x740")
        self.master.minsize(820, 560)
        self.grid(sticky="nsew")
        self.master.columnconfigure(0, weight=1)
        self.master.rowconfigure(0, weight=1)

        self.outbox: queue.Queue[Event] = queue.Queue()
        self.worker = ResearchWorker(self.outbox)
        self.result: ResearchState | None = None
        self.db_path = tk.StringVar(value=str(DEFAULT_DB))
        self._resume_id: str | None = None

        self._build()
        self._pump()

    # -- layout ------------------------------------------------------------

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        # --- question row
        top = ttk.Frame(self)
        top.grid(row=0, column=0, sticky="ew", pady=(0, PAD))
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="Question").grid(row=0, column=0, padx=(0, PAD))
        self.question = ttk.Entry(top, font=("Segoe UI", 11))
        self.question.grid(row=0, column=1, sticky="ew")
        self.question.bind("<Return>", lambda _e: self._start())
        self.go = ttk.Button(top, text="Research", command=self._start)
        self.go.grid(row=0, column=2, padx=(PAD, 0))
        self.stop = ttk.Button(top, text="Stop", command=self._cancel, state="disabled")
        self.stop.grid(row=0, column=3, padx=(4, 0))

        # --- settings row
        opts = ttk.LabelFrame(self, text="Depth and spend", padding=PAD)
        opts.grid(row=1, column=0, sticky="ew", pady=(0, PAD))

        self.rounds = tk.IntVar(value=4)
        self.workers = tk.IntVar(value=5)
        self.calls = tk.IntVar(value=20)
        self.effort = tk.StringVar(value="high")
        self.model = tk.StringVar(value="sonnet")

        def spin(col: int, label: str, var, frm, to, inc=1, width=6) -> None:
            cell = ttk.Frame(opts)
            cell.grid(row=0, column=col, padx=(0, 18), sticky="w")
            ttk.Label(cell, text=label).grid(row=0, column=0, sticky="w")
            ttk.Spinbox(cell, from_=frm, to=to, increment=inc, textvariable=var,
                        width=width).grid(row=1, column=0, sticky="w")

        spin(0, "Max rounds", self.rounds, 1, 12)
        spin(1, "Workers / round", self.workers, 1, 8)
        spin(2, "Max agent calls", self.calls, 4, 120, 2, 8)

        cell = ttk.Frame(opts)
        cell.grid(row=0, column=3, padx=(0, 14), sticky="w")
        ttk.Label(cell, text="Effort").grid(row=0, column=0, sticky="w")
        ttk.Combobox(cell, textvariable=self.effort, width=8, state="readonly",
                     values=["low", "medium", "high", "xhigh", "max"]
                     ).grid(row=1, column=0, sticky="w")

        cell = ttk.Frame(opts)
        cell.grid(row=0, column=4, padx=(0, 18), sticky="w")
        ttk.Label(cell, text="Model").grid(row=0, column=0, sticky="w")
        ttk.Combobox(cell, textvariable=self.model, width=8, state="readonly",
                     values=["sonnet", "haiku", "opus"]
                     ).grid(row=1, column=0, sticky="w")

        self.preset = ttk.Button(opts, text="Light", command=self._quick)
        self.preset.grid(row=0, column=5, sticky="s")
        self.deep = ttk.Button(opts, text="Thorough", command=self._deep)
        self.deep.grid(row=0, column=6, padx=(4, 0), sticky="s")

        risks = billing_risks()
        note = ("Usage draws on your Claude plan (5-hour + weekly windows). "
                "Nothing is billed; the run halts at your utilization cap\n"
                "and never spends usage credits.")
        ttk.Label(
            opts,
            text=note if not risks else "BILLING RISK: " + risks[0],
            foreground="#2a7" if not risks else "#c33",
            justify="left",
        ).grid(row=1, column=0, columnspan=7, sticky="w", pady=(8, 0))

        # --- notebook
        nb = ttk.Notebook(self)
        nb.grid(row=2, column=0, sticky="nsew")
        self.nb = nb

        self.log = self._text_tab(nb, "Progress")
        self.report = self._text_tab(nb, "Report", wrap="word")
        self._history_tab(nb)

        # --- status bar
        bar = ttk.Frame(self)
        bar.grid(row=3, column=0, sticky="ew", pady=(PAD, 0))
        bar.columnconfigure(1, weight=1)
        self.status = tk.StringVar(value="idle")
        ttk.Label(bar, textvariable=self.status).grid(row=0, column=0, sticky="w")
        self.stats = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.stats, foreground="#555").grid(
            row=0, column=1, sticky="e"
        )
        self.progress = ttk.Progressbar(bar, mode="indeterminate", length=160)
        self.progress.grid(row=0, column=2, padx=(PAD, 0))

        self.save = ttk.Button(bar, text="Save report...", command=self._save,
                               state="disabled")
        self.save.grid(row=0, column=3, padx=(PAD, 0))

    def _text_tab(self, nb: ttk.Notebook, title: str, wrap: str = "none") -> tk.Text:
        frame = ttk.Frame(nb, padding=4)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        nb.add(frame, text=title)
        text = tk.Text(frame, wrap=wrap, font=("Cascadia Mono", 10),
                       relief="flat", background="#fbfbfb", padx=8, pady=6)
        text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        text.configure(yscrollcommand=scroll.set, state="disabled")
        # Light markdown-ish styling for the report tab.
        text.tag_configure("h1", font=("Segoe UI", 15, "bold"), spacing1=10, spacing3=6)
        text.tag_configure("h2", font=("Segoe UI", 12, "bold"), spacing1=10, spacing3=4)
        text.tag_configure("body", font=("Segoe UI", 10), spacing3=4)
        text.tag_configure("dim", foreground="#777")
        text.tag_configure("warn", foreground="#a33")
        return text

    def _history_tab(self, nb: ttk.Notebook) -> None:
        frame = ttk.Frame(nb, padding=4)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        nb.add(frame, text="History")

        cols = ("run_id", "created", "status", "cost", "question")
        self.tree = ttk.Treeview(frame, columns=cols, show="headings", height=12)
        for col, width, anchor in (
            ("run_id", 100, "w"), ("created", 125, "w"), ("status", 80, "w"),
            ("cost", 70, "e"), ("question", 460, "w"),
        ):
            self.tree.heading(col, text=col.replace("_", " ").title())
            self.tree.column(col, width=width, anchor=anchor)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<Double-1>", lambda _e: self._open_selected())

        row = ttk.Frame(frame)
        row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(row, text="Refresh", command=self._refresh_history).pack(side="left")
        ttk.Button(row, text="Open report", command=self._open_selected).pack(
            side="left", padx=4)
        ttk.Button(row, text="Resume run", command=self._resume_selected).pack(
            side="left", padx=4)
        ttk.Label(row, textvariable=self.db_path, foreground="#888").pack(
            side="right")
        self._refresh_history()

    # -- actions -----------------------------------------------------------

    def _quick(self) -> None:
        """Light: cheap on plan usage, good for checking the pipeline."""
        self.rounds.set(2)
        self.workers.set(2)
        self.calls.set(10)
        self.effort.set("medium")
        self.model.set("sonnet")

    def _deep(self) -> None:
        """Thorough: still Sonnet, still plan-only, just more of it."""
        self.rounds.set(4)
        self.workers.set(4)
        self.calls.set(32)
        self.effort.set("high")
        self.model.set("sonnet")

    def _start(self, resume: str | None = None) -> None:
        if self.worker.busy:
            messagebox.showinfo("deepresearch", "a run is already in progress")
            return
        question = self.question.get().strip()
        if not question and not resume:
            messagebox.showwarning("deepresearch", "enter a question first")
            return

        cfg = Config(
            max_rounds=self.rounds.get(),
            max_workers_per_round=self.workers.get(),
            max_agent_calls=self.calls.get(),
            orchestrator_effort=self.effort.get(),
            orchestrator_model=self.model.get(),
            worker_model=self.model.get(),
            verifier_model=self.model.get(),
        )
        self.result = None
        self._clear(self.log)
        self._clear(self.report)
        self._append(self.log, f"question: {question or resume}\n"
                               f"rounds<={cfg.max_rounds} workers={cfg.max_workers_per_round} "
                               f"calls<={cfg.max_agent_calls} model={cfg.orchestrator_model} "
                               f"effort={cfg.orchestrator_effort}\n"
                               + "-" * 60 + "\n")
        self.nb.select(0)
        self._set_running(True)
        self.worker = ResearchWorker(self.outbox)
        self.worker.start(question, cfg, self.db_path.get(), resume)

    def _cancel(self) -> None:
        self.status.set("cancelling...")
        self.worker.cancel()

    def _set_running(self, running: bool) -> None:
        self.go.configure(state="disabled" if running else "normal")
        self.stop.configure(state="normal" if running else "disabled")
        self.save.configure(state="disabled" if running else
                            ("normal" if self.result else "disabled"))
        self.status.set("researching..." if running else "idle")
        if running:
            self.progress.start(12)
        else:
            self.progress.stop()

    def _save(self) -> None:
        if not self.result:
            return
        default = _write_outputs(self.result)
        target = filedialog.asksaveasfilename(
            initialfile=default.name, initialdir=str(default.parent),
            defaultextension=".md", filetypes=[("Markdown", "*.md")],
        )
        if target and Path(target) != default:
            Path(target).write_text(default.read_text(encoding="utf-8"),
                                    encoding="utf-8")
        messagebox.showinfo("deepresearch", f"saved to\n{target or default}")

    # -- history -----------------------------------------------------------

    def _refresh_history(self) -> None:
        self.tree.delete(*self.tree.get_children())
        try:
            store = Store(self.db_path.get())  # main-thread connection
            for row in store.list_runs(100):
                self.tree.insert("", "end", values=(
                    row["run_id"], row["created_at"][:16], row["status"],
                    f"${row['cost_usd']:.2f}", row["question"][:120],
                ))
            store.close()
        except Exception as exc:  # noqa: BLE001
            self._append(self.log, f"could not read history: {exc}\n", "warn")

    def _selected_run(self) -> str | None:
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("deepresearch", "select a run first")
            return None
        return str(self.tree.item(sel[0], "values")[0])

    def _open_selected(self) -> None:
        run_id = self._selected_run()
        if not run_id:
            return
        store = Store(self.db_path.get())
        state = store.load(run_id)
        store.close()
        if state is None:
            messagebox.showerror("deepresearch", f"run {run_id} not found")
            return
        self.result = state
        self._render_report(state)
        self.nb.select(1)
        self.save.configure(state="normal")

    def _resume_selected(self) -> None:
        run_id = self._selected_run()
        if run_id:
            self._start(resume=run_id)

    # -- rendering ---------------------------------------------------------

    def _clear(self, widget: tk.Text) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.configure(state="disabled")

    def _append(self, widget: tk.Text, text: str, tag: str | None = None) -> None:
        widget.configure(state="normal")
        widget.insert("end", text, tag or ())
        widget.see("end")
        widget.configure(state="disabled")

    def _render_report(self, state: ResearchState) -> None:
        self._clear(self.report)
        self.report.configure(state="normal")
        self.report.insert("end", f"{state.question}\n", "h1")
        self.report.insert(
            "end",
            f"round {state.round} - {len(state.claims)} claims "
            f"({sum(1 for c in state.claims if c.challenged)} challenged) - "
            f"{len(state.sources)} sources - "
            f"{len(state.unresolved_contradictions())} unresolved conflicts - "
            f"${state.total_cost_usd:.2f}\n\n",
            "dim",
        )
        for line in (state.report or "(no report -- run did not finish)").split("\n"):
            stripped = line.lstrip("#").strip()
            if line.startswith("# "):
                self.report.insert("end", stripped + "\n", "h1")
            elif line.startswith(("## ", "### ")):
                self.report.insert("end", stripped + "\n", "h2")
            else:
                self.report.insert("end", line + "\n", "body")
        self.report.configure(state="disabled")

    def _update_stats(self, state: ResearchState) -> None:
        self.stats.set(
            f"round {state.round}  |  {len(state.claims)} claims  |  "
            f"{len(state.sources)} sources  |  "
            f"{len(state.unresolved_contradictions())} conflicts  |  "
            f"~${state.total_cost_usd:.2f} est. (not billed)"
        )

    # -- event pump --------------------------------------------------------

    def _pump(self) -> None:
        """Drain the worker queue on the Tk thread. Runs every 120ms."""
        try:
            while True:
                event = self.outbox.get_nowait()
                if event.kind == "log":
                    self._append(self.log, event.text + "\n")
                elif event.kind == "done" and event.state:
                    self.result = event.state
                    self._update_stats(event.state)
                    self._render_report(event.state)
                    path = _write_outputs(event.state)
                    self._append(self.log, f"\ndone -- report written to {path}\n")
                    self._set_running(False)
                    self._refresh_history()
                    self.nb.select(1)
                elif event.kind == "error":
                    self._append(self.log, f"\n{event.text}\n", "warn")
                    self.status.set("stopped")
                    self._set_running(False)
                    self._refresh_history()
        except queue.Empty:
            pass
        self.after(120, self._pump)


def main() -> int:
    root = tk.Tk()
    try:
        root.call("source", "sun-valley.tcl")  # optional nicer theme if present
    except tk.TclError:
        pass
    style = ttk.Style()
    if "vista" in style.theme_names():
        style.theme_use("vista")
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
