"""SQLite persistence. Stdlib only.

Two jobs:
  1. Durable research state, so an interrupted run resumes instead of restarting.
  2. A cross-run source/claim corpus, so repeated research on adjacent topics
     reuses prior reading instead of re-fetching it.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .state import Claim, Contradiction, ResearchState, Source

DEFAULT_DB = Path.home() / ".deepresearch" / "research.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    question    TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    state_json  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'running',
    cost_usd    REAL NOT NULL DEFAULT 0
);

-- Sources and claims are also stored relationally (not just inside
-- state_json) so they can be queried across runs.
CREATE TABLE IF NOT EXISTS sources (
    url          TEXT PRIMARY KEY,
    title        TEXT,
    kind         TEXT,
    published    TEXT,
    credibility  REAL,
    retrieved_at TEXT
);

CREATE TABLE IF NOT EXISTS claims (
    id              TEXT NOT NULL,
    run_id          TEXT NOT NULL,
    subquestion_id  TEXT,
    text            TEXT NOT NULL,
    source_url      TEXT,
    support         REAL,
    quote           TEXT,
    round           INTEGER,
    challenged      INTEGER DEFAULT 0,
    PRIMARY KEY (id, run_id)
);

CREATE TABLE IF NOT EXISTS contradictions (
    run_id      TEXT NOT NULL,
    claim_a     TEXT, claim_b TEXT,
    description TEXT, salience REAL,
    resolved    INTEGER DEFAULT 0, resolution TEXT
);

-- Fetched page text, keyed by URL. Avoids paying to re-read the same page
-- in a later round or a later run.
CREATE TABLE IF NOT EXISTS page_cache (
    url         TEXT PRIMARY KEY,
    content     TEXT,
    fetched_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_claims_run    ON claims(run_id);
CREATE INDEX IF NOT EXISTS idx_claims_source ON claims(source_url);
"""


class Store:
    def __init__(self, path: Path | str = DEFAULT_DB) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- run-level ---------------------------------------------------------

    def save(self, state: ResearchState) -> None:
        """Checkpoint the whole run. Called after every phase, so a crash
        loses at most one phase of work."""
        self.conn.execute(
            "INSERT INTO runs (run_id, question, created_at, state_json, cost_usd) "
            "VALUES (?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET "
            "state_json=excluded.state_json, cost_usd=excluded.cost_usd",
            (
                state.run_id,
                state.question,
                state.created_at,
                state.model_dump_json(),
                state.total_cost_usd,
            ),
        )
        for s in state.sources:
            self._put_source(s)
        for c in state.claims:
            self._put_claim(state.run_id, c)
        self.conn.execute("DELETE FROM contradictions WHERE run_id=?", (state.run_id,))
        for c in state.contradictions:
            self._put_contradiction(state.run_id, c)
        self.conn.commit()

    def load(self, run_id: str) -> ResearchState | None:
        row = self.conn.execute(
            "SELECT state_json FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        return ResearchState.model_validate_json(row["state_json"]) if row else None

    def mark_status(self, run_id: str, status: str) -> None:
        self.conn.execute("UPDATE runs SET status=? WHERE run_id=?", (status, run_id))
        self.conn.commit()

    def list_runs(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT run_id, question, created_at, status, cost_usd FROM runs "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

    # -- row-level ---------------------------------------------------------

    def _put_source(self, s: Source) -> None:
        self.conn.execute(
            "INSERT INTO sources (url,title,kind,published,credibility,retrieved_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET "
            "credibility=excluded.credibility, kind=excluded.kind",
            (s.url, s.title, s.kind.value, s.published, s.credibility, s.retrieved_at),
        )

    def _put_claim(self, run_id: str, c: Claim) -> None:
        self.conn.execute(
            "INSERT INTO claims (id,run_id,subquestion_id,text,source_url,support,"
            "quote,round,challenged) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id,run_id) DO UPDATE SET challenged=excluded.challenged",
            (c.id, run_id, c.subquestion_id, c.text, c.source_url, c.support,
             c.quote, c.round, int(c.challenged)),
        )

    def _put_contradiction(self, run_id: str, c: Contradiction) -> None:
        self.conn.execute(
            "INSERT INTO contradictions (run_id,claim_a,claim_b,description,"
            "salience,resolved,resolution) VALUES (?,?,?,?,?,?,?)",
            (run_id, c.claim_a, c.claim_b, c.description, c.salience,
             int(c.resolved), c.resolution),
        )

    # -- cross-run reuse ---------------------------------------------------

    def prior_claims_about(self, keyword: str, limit: int = 30) -> list[sqlite3.Row]:
        """Claims from *previous* runs mentioning a keyword. Seeds a new run
        with what you already established, instead of starting cold."""
        return self.conn.execute(
            "SELECT c.text, c.source_url, s.credibility FROM claims c "
            "LEFT JOIN sources s ON s.url = c.source_url "
            "WHERE c.text LIKE ? ORDER BY s.credibility DESC LIMIT ?",
            (f"%{keyword}%", limit),
        ).fetchall()

    def cache_page(self, url: str, content: str, fetched_at: str) -> None:
        self.conn.execute(
            "INSERT INTO page_cache (url,content,fetched_at) VALUES (?,?,?) "
            "ON CONFLICT(url) DO UPDATE SET content=excluded.content, "
            "fetched_at=excluded.fetched_at",
            (url, content, fetched_at),
        )
        self.conn.commit()

    def cached_page(self, url: str) -> str | None:
        row = self.conn.execute(
            "SELECT content FROM page_cache WHERE url=?", (url,)
        ).fetchone()
        return row["content"] if row else None
