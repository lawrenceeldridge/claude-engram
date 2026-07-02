"""SQLite repository for memory facts (Data Mapper — rows never persist themselves).

One global database under CLAUDE_PLUGIN_DATA holds every project's memory, each
row tagged with its project key. Facts are content-addressed per project
(``id = hash(project_key + normalised_text)``). Re-encountering the same fact
reinforces it (frequency++, last_seen refreshed) rather than duplicating it;
a semantically near-identical newer fact can supersede older ones.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path

from core.project import Project

_FTS_TOKEN = re.compile(r"[A-Za-z0-9_]+")


def _fts_match_expr(query: str) -> str:
    """Turn a free-text query into a safe FTS5 MATCH expression (OR of quoted terms).

    Quoting each token defuses FTS5 operator characters in user input, so an
    arbitrary query can never raise a syntax error; OR keeps it recall-oriented.
    """
    return " OR ".join(f'"{t}"' for t in _FTS_TOKEN.findall(query.lower()))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
  id            TEXT PRIMARY KEY,
  project_key   TEXT NOT NULL,
  project_label TEXT,
  project_path  TEXT,
  session_id    TEXT,
  kind          TEXT,
  text          TEXT NOT NULL,
  title         TEXT,
  narrative     TEXT,
  files         TEXT,
  created_at    REAL,
  last_seen     REAL,
  dim           INTEGER,
  scale         REAL,
  vec_int8      BLOB,
  vec_bits      BLOB,
  importance    REAL DEFAULT 0,
  frequency     INTEGER DEFAULT 1,
  status        TEXT DEFAULT 'active',
  superseded_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_facts_project ON facts(project_key, status);
CREATE INDEX IF NOT EXISTS idx_facts_created ON facts(created_at);

CREATE TABLE IF NOT EXISTS recall_events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          REAL,
  project_key TEXT,
  query       TEXT,
  returned    INTEGER,
  top_sim     REAL,
  confidence  REAL,
  verdict     TEXT
);
CREATE INDEX IF NOT EXISTS idx_recall_project ON recall_events(project_key);

CREATE TABLE IF NOT EXISTS capture_cursors (
  cursor_key  TEXT PRIMARY KEY,
  offset      INTEGER NOT NULL,
  updated_at  REAL
);
"""

# Full-text index over the searchable columns. External-content FTS5 keyed on the
# facts rowid, kept in sync by triggers so every insert/update/supersede/delete is
# reflected without maintenance in Python. Complements the vector channel with
# exact-term recall.
_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
  text, title, narrative, content='facts', content_rowid='rowid', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
  INSERT INTO facts_fts(rowid, text, title, narrative)
  VALUES (new.rowid, new.text, COALESCE(new.title,''), COALESCE(new.narrative,''));
END;
CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
  INSERT INTO facts_fts(facts_fts, rowid, text, title, narrative)
  VALUES ('delete', old.rowid, old.text, COALESCE(old.title,''), COALESCE(old.narrative,''));
END;
CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
  INSERT INTO facts_fts(facts_fts, rowid, text, title, narrative)
  VALUES ('delete', old.rowid, old.text, COALESCE(old.title,''), COALESCE(old.narrative,''));
  INSERT INTO facts_fts(rowid, text, title, narrative)
  VALUES (new.rowid, new.text, COALESCE(new.title,''), COALESCE(new.narrative,''));
END;
"""


def _add_columns(db: sqlite3.Connection, specs: list[tuple[str, str]]) -> None:
    existing = {row[1] for row in db.execute("PRAGMA table_info(facts)")}
    for name, ddl in specs:
        if name not in existing:
            db.execute(f"ALTER TABLE facts ADD COLUMN {ddl}")


def _v1_lifecycle(db: sqlite3.Connection) -> None:
    _add_columns(
        db,
        [
            ("last_seen", "last_seen REAL"),
            ("frequency", "frequency INTEGER DEFAULT 1"),
            ("status", "status TEXT DEFAULT 'active'"),
            ("superseded_by", "superseded_by TEXT"),
        ],
    )
    db.execute("UPDATE facts SET last_seen = created_at WHERE last_seen IS NULL")


def _v2_structured(db: sqlite3.Connection) -> None:
    _add_columns(db, [("title", "title TEXT"), ("narrative", "narrative TEXT"), ("files", "files TEXT")])


def _v3_fts(db: sqlite3.Connection) -> None:
    existed = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='facts_fts'"
    ).fetchone()
    db.executescript(_FTS_SCHEMA)
    if not existed:
        # External-content FTS5 is populated with the 'rebuild' command (a manual
        # INSERT...SELECT creates rows that don't match); run it once, when the index
        # is first created, to backfill facts written before it existed.
        db.execute("INSERT INTO facts_fts(facts_fts) VALUES ('rebuild')")


# Ordered schema migrations. user_version marks how many have run; every step is
# also individually idempotent (ADD COLUMN only if missing, CREATE ... IF NOT
# EXISTS, rebuild only on first creation), so a database at any prior version —
# including the legacy FTS flag of 1 — converges by running the rest as no-ops.
_MIGRATIONS = [_v1_lifecycle, _v2_structured, _v3_fts]
_SCHEMA_VERSION = len(_MIGRATIONS)


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Run the schema-migration ladder up to _SCHEMA_VERSION, then stamp it.

        user_version is the fast path: once stamped, opens are a single PRAGMA read.
        Below the head we replay every step — cheap because each is idempotent — so
        a fresh, partial, or legacy database all converge to the same schema.
        """
        if self.db.execute("PRAGMA user_version").fetchone()[0] == _SCHEMA_VERSION:
            return
        for step in _MIGRATIONS:
            step(self.db)
        self.db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    @staticmethod
    def fact_id(project_key: str, text: str) -> str:
        norm = " ".join(text.lower().split())
        return hashlib.sha256(f"{project_key}\x00{norm}".encode()).hexdigest()[:24]

    def exists(self, fact_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM facts WHERE id = ?", (fact_id,)).fetchone() is not None

    def reinforce(self, fact_id: str, now: float | None = None) -> None:
        """Consolidation — strengthen a fact seen again and refresh its recency."""
        self.db.execute(
            "UPDATE facts SET frequency = frequency + 1, last_seen = ?, status = 'active' WHERE id = ?",
            (now if now is not None else time.time(), fact_id),
        )
        self.db.commit()

    def supersede(self, fact_ids: list[str], by_id: str) -> int:
        """Retroactive interference — archive facts replaced by a newer one."""
        if not fact_ids:
            return 0
        placeholders = ",".join("?" for _ in fact_ids)
        cur = self.db.execute(
            f"UPDATE facts SET status = 'superseded', superseded_by = ? "
            f"WHERE id IN ({placeholders}) AND status = 'active'",
            (by_id, *fact_ids),
        )
        self.db.commit()
        return cur.rowcount

    def add(
        self,
        *,
        project: Project,
        session_id: str,
        kind: str,
        text: str,
        vec_int8: bytes,
        scale: float,
        dim: int,
        vec_bits: bytes,
        importance: float,
        created_at: float | None = None,
        title: str = "",
        narrative: str = "",
        files: list[str] | None = None,
    ) -> bool:
        fid = self.fact_id(project["key"], text)
        stamp = created_at if created_at is not None else time.time()
        cur = self.db.execute(
            "INSERT OR IGNORE INTO facts "
            "(id, project_key, project_label, project_path, session_id, kind, text, "
            " title, narrative, files, created_at, last_seen, dim, scale, vec_int8, vec_bits, "
            " importance, frequency, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'active')",
            (
                fid,
                project["key"],
                project["label"],
                project["path"],
                session_id,
                kind,
                text,
                title or None,
                narrative or None,
                json.dumps(files) if files else None,
                stamp,
                stamp,
                dim,
                scale,
                vec_int8,
                vec_bits,
                importance,
            ),
        )
        self.db.commit()
        return cur.rowcount > 0

    def get(self, fact_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()

    def rows_for_project(
        self, project_key: str, limit: int | None = None, offset: int = 0
    ) -> list[sqlite3.Row]:
        """Facts for a project, newest first. Paginate with limit/offset; limit=None returns all."""
        sql = "SELECT * FROM facts WHERE project_key = ? ORDER BY created_at DESC, rowid DESC"
        params: list = [project_key]
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params += [limit, offset]
        return self.db.execute(sql, params).fetchall()

    def active_rows_for_project(self, project_key: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM facts WHERE project_key = ? AND status = 'active'", (project_key,)
        ).fetchall()

    def active_rows(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM facts WHERE status = 'active'").fetchall()

    def stored_dims(self, project_key: str) -> set[int]:
        """Distinct embedding dimensions of a project's active facts.

        Lets recall detect a write/read embedding-space divergence: if the query
        embedder's dimension is absent here, every candidate was silently skipped
        by the dim gate and the result would otherwise masquerade as 'no memory'.
        """
        rows = self.db.execute(
            "SELECT DISTINCT dim FROM facts "
            "WHERE project_key = ? AND status = 'active' AND dim IS NOT NULL",
            (project_key,),
        ).fetchall()
        return {row[0] for row in rows}

    def recent(self, project_key: str, limit: int) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM facts WHERE project_key = ? AND status = 'active' "
            "ORDER BY frequency DESC, last_seen DESC LIMIT ?",
            (project_key, limit),
        ).fetchall()

    def projects(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT project_key, project_label, project_path, "
            "COUNT(*) AS c, MAX(created_at) AS last "
            "FROM facts WHERE status = 'active' GROUP BY project_key ORDER BY last DESC"
        ).fetchall()

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM facts WHERE status = 'active'").fetchone()[0]

    def active_count(self, project_key: str) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM facts WHERE project_key = ? AND status = 'active'",
            (project_key,),
        ).fetchone()[0]

    def clear_session_kind(self, project_key: str, session_id: str, kind: str) -> int:
        """Delete a session's facts of a given kind (used to replace its session summary)."""
        cur = self.db.execute(
            "DELETE FROM facts WHERE project_key = ? AND session_id = ? AND kind = ?",
            (project_key, session_id, kind),
        )
        self.db.commit()
        return cur.rowcount

    def fts_search(self, project_key: str, query: str, limit: int = 50) -> list[str]:
        """Active fact ids for a project matching an FTS5 keyword query, best-ranked first."""
        match = _fts_match_expr(query)
        if not match:
            return []
        rows = self.db.execute(
            "SELECT f.id FROM facts_fts JOIN facts f ON f.rowid = facts_fts.rowid "
            "WHERE facts_fts MATCH ? AND f.project_key = ? AND f.status = 'active' "
            "ORDER BY bm25(facts_fts) LIMIT ?",
            (match, project_key, limit),
        ).fetchall()
        return [row[0] for row in rows]

    def sweep(
        self,
        now: float,
        ttl_seconds: float,
        keep_frequency: int,
        project_key: str | None = None,
    ) -> int:
        """Archive stale facts (forgetting curve, hard expiry).

        Retires active facts unseen for longer than the TTL, unless they have been
        reinforced enough (``frequency >= keep_frequency``). Reversible: rows are
        marked 'expired', not deleted, so the viewer can still show them.
        """
        cutoff = now - ttl_seconds
        sql = (
            "UPDATE facts SET status = 'expired' "
            "WHERE status = 'active' AND last_seen < ? AND frequency < ?"
        )
        params: list = [cutoff, keep_frequency]
        if project_key:
            sql += " AND project_key = ?"
            params.append(project_key)
        cur = self.db.execute(sql, params)
        self.db.commit()
        return cur.rowcount

    def prune_project(self, project_key: str) -> int:
        cur = self.db.execute("DELETE FROM facts WHERE project_key = ?", (project_key,))
        self.db.commit()
        return cur.rowcount

    def log_recall(
        self,
        project_key: str,
        query: str,
        *,
        returned: int,
        top_sim: float,
        confidence: float,
        verdict: str,
        now: float | None = None,
    ) -> None:
        """Append one recall to the telemetry ledger (feeds stats and future tuning). Best-effort."""
        try:
            self.db.execute(
                "INSERT INTO recall_events (ts, project_key, query, returned, top_sim, confidence, verdict) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (now if now is not None else time.time(), project_key, query, returned, top_sim, confidence, verdict),
            )
            self.db.commit()
        except sqlite3.Error:
            pass

    def recall_stats(self, project_key: str | None = None) -> dict:
        """Aggregate recall telemetry: call count and per-verdict breakdown."""
        where = "WHERE project_key = ?" if project_key else ""
        params = (project_key,) if project_key else ()
        total = self.db.execute(f"SELECT COUNT(*) FROM recall_events {where}", params).fetchone()[0]
        rows = self.db.execute(
            f"SELECT verdict, COUNT(*) AS c FROM recall_events {where} GROUP BY verdict", params
        ).fetchall()
        return {"total": total, "by_verdict": {row["verdict"]: row["c"] for row in rows}}

    def data_version(self) -> int:
        """SQLite change counter — bumps on every commit by another connection (cache-invalidation signal)."""
        return self.db.execute("PRAGMA data_version").fetchone()[0]

    def get_capture_cursor(self, cursor_key: str) -> int:
        """Byte offset already distilled for this session, so incremental capture reads only new turns."""
        row = self.db.execute(
            "SELECT offset FROM capture_cursors WHERE cursor_key = ?", (cursor_key,)
        ).fetchone()
        return row["offset"] if row else 0

    def set_capture_cursor(self, cursor_key: str, offset: int, now: float | None = None) -> None:
        self.db.execute(
            "INSERT INTO capture_cursors (cursor_key, offset, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(cursor_key) DO UPDATE SET offset = excluded.offset, updated_at = excluded.updated_at",
            (cursor_key, offset, now if now is not None else time.time()),
        )
        self.db.commit()
