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
  subtitle      TEXT,
  narrative     TEXT,
  files         TEXT,
  type          TEXT,
  observation_id TEXT,
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
  text, title, subtitle, narrative, files, content='facts', content_rowid='rowid', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
  INSERT INTO facts_fts(rowid, text, title, subtitle, narrative, files)
  VALUES (new.rowid, new.text, COALESCE(new.title,''), COALESCE(new.subtitle,''), COALESCE(new.narrative,''), COALESCE(new.files,''));
END;
CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
  INSERT INTO facts_fts(facts_fts, rowid, text, title, subtitle, narrative, files)
  VALUES ('delete', old.rowid, old.text, COALESCE(old.title,''), COALESCE(old.subtitle,''), COALESCE(old.narrative,''), COALESCE(old.files,''));
END;
CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
  INSERT INTO facts_fts(facts_fts, rowid, text, title, subtitle, narrative, files)
  VALUES ('delete', old.rowid, old.text, COALESCE(old.title,''), COALESCE(old.subtitle,''), COALESCE(old.narrative,''), COALESCE(old.files,''));
  INSERT INTO facts_fts(rowid, text, title, subtitle, narrative, files)
  VALUES (new.rowid, new.text, COALESCE(new.title,''), COALESCE(new.subtitle,''), COALESCE(new.narrative,''), COALESCE(new.files,''));
END;
"""


# Code/docs index (Phase 1: doc sections). Separate tables in the same DB — never
# mixed into `facts`, so recall of learned memory is never polluted by raw source
# chunks. Vectors live inline (dim/scale/vec_int8) exactly as facts store them, and
# `chunk_sources` records a per-file hash+mtime so re-indexing skips unchanged files.
_CHUNK_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
  id            TEXT PRIMARY KEY,
  project_key   TEXT NOT NULL,
  source_path   TEXT NOT NULL,
  kind          TEXT,
  anchor        TEXT,
  title         TEXT,
  heading_path  TEXT,
  level         INTEGER,
  summary       TEXT,
  body          TEXT,
  byte_start    INTEGER,
  byte_end      INTEGER,
  content_hash  TEXT,
  dim           INTEGER,
  scale         REAL,
  vec_int8      BLOB,
  indexed_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_chunks_project ON chunks(project_key);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(project_key, source_path);

CREATE TABLE IF NOT EXISTS chunk_sources (
  project_key  TEXT NOT NULL,
  source_path  TEXT NOT NULL,
  file_hash    TEXT,
  mtime_ns     INTEGER,
  indexed_at   REAL,
  PRIMARY KEY (project_key, source_path)
);
"""

# External-content FTS5 over the chunk's searchable columns, weighted at query time
# (title > heading_path > summary > body) in the bm25() call. Triggers keep it in sync.
_CHUNK_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  title, heading_path, summary, body, content='chunks', content_rowid='rowid', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
  INSERT INTO chunks_fts(rowid, title, heading_path, summary, body)
  VALUES (new.rowid, COALESCE(new.title,''), COALESCE(new.heading_path,''), COALESCE(new.summary,''), COALESCE(new.body,''));
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, title, heading_path, summary, body)
  VALUES ('delete', old.rowid, COALESCE(old.title,''), COALESCE(old.heading_path,''), COALESCE(old.summary,''), COALESCE(old.body,''));
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, title, heading_path, summary, body)
  VALUES ('delete', old.rowid, COALESCE(old.title,''), COALESCE(old.heading_path,''), COALESCE(old.summary,''), COALESCE(old.body,''));
  INSERT INTO chunks_fts(rowid, title, heading_path, summary, body)
  VALUES (new.rowid, COALESCE(new.title,''), COALESCE(new.heading_path,''), COALESCE(new.summary,''), COALESCE(new.body,''));
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


def _v4_observations(db: sqlite3.Connection) -> None:
    # Group atomic facts into typed observations for display; the fact stays the
    # embedded retrieval unit, observation_id/type are card metadata only.
    _add_columns(db, [("type", "type TEXT"), ("observation_id", "observation_id TEXT")])
    db.execute("CREATE INDEX IF NOT EXISTS idx_facts_observation ON facts(observation_id)")


def _v5_subtitle(db: sqlite3.Connection) -> None:
    _add_columns(db, [("subtitle", "subtitle TEXT")])


def _v6_fts_widen(db: sqlite3.Connection) -> None:
    # FTS5 can't ALTER-add columns, so drop and rebuild the index over the widened
    # column set (now including subtitle + files). Facts (the content table) are
    # untouched; 'rebuild' repopulates the index from them.
    db.executescript(
        "DROP TRIGGER IF EXISTS facts_ai; DROP TRIGGER IF EXISTS facts_ad;"
        "DROP TRIGGER IF EXISTS facts_au; DROP TABLE IF EXISTS facts_fts;"
    )
    db.executescript(_FTS_SCHEMA)
    db.execute("INSERT INTO facts_fts(facts_fts) VALUES ('rebuild')")


def _v7_index(db: sqlite3.Connection) -> None:
    # Code/docs index tables + their FTS. Additive and idempotent; the facts store is
    # untouched. 'rebuild' backfills the FTS from any chunks written before it existed.
    existed = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunks_fts'"
    ).fetchone()
    db.executescript(_CHUNK_SCHEMA)
    db.executescript(_CHUNK_FTS_SCHEMA)
    if not existed:
        db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")


# Ordered schema migrations. user_version marks how many have run; every step is
# also individually idempotent (ADD COLUMN only if missing, CREATE ... IF NOT
# EXISTS, rebuild only on first creation), so a database at any prior version —
# including the legacy FTS flag of 1 — converges by running the rest as no-ops.
_MIGRATIONS = [
    _v1_lifecycle, _v2_structured, _v3_fts, _v4_observations, _v5_subtitle, _v6_fts_widen, _v7_index
]
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
        subtitle: str = "",
        narrative: str = "",
        files: list[str] | None = None,
        type: str = "",
        observation_id: str = "",
    ) -> bool:
        fid = self.fact_id(project["key"], text)
        stamp = created_at if created_at is not None else time.time()
        cur = self.db.execute(
            "INSERT OR IGNORE INTO facts "
            "(id, project_key, project_label, project_path, session_id, kind, text, "
            " title, subtitle, narrative, files, type, observation_id, created_at, last_seen, dim, scale, "
            " vec_int8, vec_bits, importance, frequency, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'active')",
            (
                fid,
                project["key"],
                project["label"],
                project["path"],
                session_id,
                kind,
                text,
                title or None,
                subtitle or None,
                narrative or None,
                json.dumps(files) if files else None,
                type or None,
                observation_id or None,
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

    def list_observations(
        self, project_key: str, limit: int | None = None, offset: int = 0
    ) -> list[list[sqlite3.Row]]:
        """Facts grouped into observation cards, newest group first, paginated by group.

        A group is the facts sharing an observation_id (falling back to the fact's own
        id for ungrouped rows), returned as an ordered list of its fact rows.
        """
        grp = "COALESCE(observation_id, id)"
        sql = (
            f"SELECT {grp} AS grp, MAX(created_at) AS ts, MAX(rowid) AS rid "
            "FROM facts WHERE project_key = ? GROUP BY grp ORDER BY ts DESC, rid DESC"
        )
        params: list = [project_key]
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params += [limit, offset]
        groups = self.db.execute(sql, params).fetchall()
        return [
            self.db.execute(
                f"SELECT * FROM facts WHERE project_key = ? AND {grp} = ? ORDER BY rowid ASC",
                (project_key, row["grp"]),
            ).fetchall()
            for row in groups
        ]

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

    # ---- Code/docs index (chunks) -------------------------------------------------

    @staticmethod
    def chunk_id(project_key: str, source_path: str, anchor: str) -> str:
        basis = f"{project_key}\x00{source_path}\x00{anchor}"
        return hashlib.sha256(basis.encode()).hexdigest()[:24]

    def source_state(self, project_key: str, source_path: str) -> tuple[str, int] | None:
        """(file_hash, mtime_ns) last indexed for a file, or None — drives the re-index short-circuit."""
        row = self.db.execute(
            "SELECT file_hash, mtime_ns FROM chunk_sources WHERE project_key = ? AND source_path = ?",
            (project_key, source_path),
        ).fetchone()
        return (row["file_hash"], row["mtime_ns"]) if row else None

    def indexed_sources(self, project_key: str) -> set[str]:
        rows = self.db.execute(
            "SELECT source_path FROM chunk_sources WHERE project_key = ?", (project_key,)
        ).fetchall()
        return {row[0] for row in rows}

    def replace_source_chunks(
        self, project_key: str, source_path: str, chunks: list[dict], file_hash: str, mtime_ns: int,
        now: float | None = None,
    ) -> int:
        """Atomically swap a file's chunks for a freshly-parsed set and stamp its source state.

        Delete-then-insert keeps the index in step with the file even when sections are
        removed or renamed; the whole swap is one transaction so a crash can't leave a
        half-indexed file. Returns the number of chunks written.
        """
        stamp = now if now is not None else time.time()
        with self.db:
            self.db.execute(
                "DELETE FROM chunks WHERE project_key = ? AND source_path = ?", (project_key, source_path)
            )
            self.db.executemany(
                "INSERT OR REPLACE INTO chunks "
                "(id, project_key, source_path, kind, anchor, title, heading_path, level, "
                " summary, body, byte_start, byte_end, content_hash, dim, scale, vec_int8, indexed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        c["id"], project_key, source_path, c.get("kind", "doc_section"), c["anchor"],
                        c["title"], c["heading_path"], c["level"], c.get("summary") or None, c["body"],
                        c["byte_start"], c["byte_end"], c["content_hash"], c["dim"], c["scale"],
                        c["vec_int8"], stamp,
                    )
                    for c in chunks
                ],
            )
            self.db.execute(
                "INSERT INTO chunk_sources (project_key, source_path, file_hash, mtime_ns, indexed_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(project_key, source_path) DO UPDATE SET "
                "file_hash = excluded.file_hash, mtime_ns = excluded.mtime_ns, indexed_at = excluded.indexed_at",
                (project_key, source_path, file_hash, mtime_ns, stamp),
            )
        return len(chunks)

    def delete_source(self, project_key: str, source_path: str) -> None:
        """Drop a vanished file's chunks and source row (called for files gone since last index)."""
        with self.db:
            self.db.execute(
                "DELETE FROM chunks WHERE project_key = ? AND source_path = ?", (project_key, source_path)
            )
            self.db.execute(
                "DELETE FROM chunk_sources WHERE project_key = ? AND source_path = ?",
                (project_key, source_path),
            )

    def get_chunk(self, project_key: str, ref: str) -> sqlite3.Row | None:
        """Fetch one chunk by its id or its human-readable anchor slug."""
        return self.db.execute(
            "SELECT * FROM chunks WHERE project_key = ? AND (id = ? OR anchor = ?) LIMIT 1",
            (project_key, ref, ref),
        ).fetchone()

    def chunk_outline(
        self, project_key: str, source_path: str | None = None
    ) -> list[sqlite3.Row]:
        """Ordered skeleton (no body): anchor/title/heading_path/level/summary per chunk."""
        sql = (
            "SELECT id, source_path, kind, anchor, title, heading_path, level, summary "
            "FROM chunks WHERE project_key = ?"
        )
        params: list = [project_key]
        if source_path is not None:
            sql += " AND source_path = ?"
            params.append(source_path)
        sql += " ORDER BY source_path, byte_start"
        return self.db.execute(sql, params).fetchall()

    def chunk_rows(self, project_key: str) -> list[sqlite3.Row]:
        """All chunk rows for a project (vector-channel scan input)."""
        return self.db.execute("SELECT * FROM chunks WHERE project_key = ?", (project_key,)).fetchall()

    def chunk_fts_search(self, project_key: str, query: str, limit: int = 50) -> list[str]:
        """Chunk ids matching an FTS5 keyword query, best-ranked first (weighted columns)."""
        match = _fts_match_expr(query)
        if not match:
            return []
        rows = self.db.execute(
            "SELECT c.id FROM chunks_fts JOIN chunks c ON c.rowid = chunks_fts.rowid "
            "WHERE chunks_fts MATCH ? AND c.project_key = ? "
            "ORDER BY bm25(chunks_fts, 3.0, 2.0, 1.5, 1.0) LIMIT ?",
            (match, project_key, limit),
        ).fetchall()
        return [row[0] for row in rows]

    def chunk_projects(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT project_key, COUNT(*) AS c, COUNT(DISTINCT source_path) AS files, "
            "MAX(indexed_at) AS last FROM chunks GROUP BY project_key ORDER BY last DESC"
        ).fetchall()

    def chunk_count(self, project_key: str) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM chunks WHERE project_key = ?", (project_key,)
        ).fetchone()[0]

    def prune_chunks(self, project_key: str) -> int:
        with self.db:
            self.db.execute("DELETE FROM chunk_sources WHERE project_key = ?", (project_key,))
            cur = self.db.execute("DELETE FROM chunks WHERE project_key = ?", (project_key,))
        return cur.rowcount
