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


def _now(now: float | None) -> float:
    """Resolve an optional caller-supplied timestamp to a concrete one (test seam)."""
    return now if now is not None else time.time()


def _placeholders(seq) -> str:
    """`?, ?, …` for an IN (...) clause sized to ``seq``."""
    return ",".join("?" for _ in seq)


def _content_id(project_key: str, text: str) -> str:
    """Content-addressed id for a fact (per project, whitespace/case-normalised)."""
    norm = " ".join(text.lower().split())
    return hashlib.sha256(f"{project_key}\x00{norm}".encode()).hexdigest()[:24]


def _sensory_id(project_key: str, modality: str, url: str, text: str) -> str:
    """Content-addressed id for a sensory perception (project/modality/url/text,
    whitespace/case-normalised) — re-perceiving the identical thing is idempotent."""
    norm = " ".join(text.lower().split())
    return hashlib.sha256(f"{project_key}\x00{modality}\x00{url}\x00{norm}".encode()).hexdigest()[:24]


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
  superseded_by TEXT,
  tier          TEXT NOT NULL DEFAULT 'ltm',
  recall_count  INTEGER DEFAULT 0,
  last_recalled REAL
);
CREATE INDEX IF NOT EXISTS idx_facts_project ON facts(project_key, status);
CREATE INDEX IF NOT EXISTS idx_facts_created ON facts(created_at);
-- NOTE: idx_facts_tier is created in migration _v9_stm, not here — the base schema
-- runs before migrations, so an index referencing the migration-added `tier` column
-- must not live here (it would fail opening a pre-v9 database).

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
    existed = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='facts_fts'").fetchone()
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


def _v8_redistill(db: sqlite3.Connection) -> None:
    # Recovery queue: raw deltas whose capture fell back to the heuristic (LLM was
    # unreachable / timed out) are parked here so a later capture with a working LLM
    # can re-distil them and replace the untitled 'discovery' facts. Shared across
    # sessions, so a healthy session drains junk a stale/broken one produced.
    db.executescript(
        "CREATE TABLE IF NOT EXISTS pending_redistill ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  project_key TEXT NOT NULL,"
        "  session_id  TEXT,"
        "  text        TEXT NOT NULL,"
        "  fact_ids    TEXT,"
        "  attempts    INTEGER DEFAULT 0,"
        "  created_at  REAL"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_redistill_project ON pending_redistill(project_key);"
    )


def _v7_index(db: sqlite3.Connection) -> None:
    # Code/docs index tables + their FTS. Additive and idempotent; the facts store is
    # untouched. 'rebuild' backfills the FTS from any chunks written before it existed.
    existed = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunks_fts'").fetchone()
    db.executescript(_CHUNK_SCHEMA)
    db.executescript(_CHUNK_FTS_SCHEMA)
    if not existed:
        db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")


def _v9_stm(db: sqlite3.Connection) -> None:
    # Atkinson-Shiffrin STM/LTM split + retrieval attribution. `tier` marks a fact's
    # store: fresh captures land in 'stm' and promote to 'ltm' on rehearsal (see
    # service.add_records). Existing rows are established memory → 'ltm'. recall_count/
    # last_recalled feed the retention score (design §3A) — the testing/spacing signals.
    # Additive: recall stays tier-agnostic by default, so behaviour is unchanged.
    _add_columns(
        db,
        [
            ("tier", "tier TEXT NOT NULL DEFAULT 'ltm'"),
            ("recall_count", "recall_count INTEGER DEFAULT 0"),
            ("last_recalled", "last_recalled REAL"),
        ],
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_facts_tier ON facts(project_key, tier, status)")


def _v10_work_queue(db: sqlite3.Connection) -> None:
    # Durable Command queue for the MemoryBus inproc adapter — the at-least-once,
    # retry-able form of detached capture (survives dropped connections / distiller
    # outages). msg_id is a content hash → idempotent publish. ack deletes the row;
    # nak reschedules (next_retry_at); a lease (lease_expires) makes an interrupted
    # claim reclaimable (crash recovery); exhausted retries land in status='dead'.
    db.executescript(
        "CREATE TABLE IF NOT EXISTS work_queue ("
        "  msg_id        TEXT PRIMARY KEY,"
        "  stage         TEXT NOT NULL,"
        "  project_key   TEXT NOT NULL,"
        "  session_id    TEXT,"
        "  ref           TEXT,"
        "  payload       TEXT,"
        "  status        TEXT NOT NULL DEFAULT 'pending',"  # pending | in_progress | dead
        "  attempts      INTEGER DEFAULT 0,"
        "  next_retry_at REAL DEFAULT 0,"
        "  lease_owner   TEXT,"
        "  lease_expires REAL DEFAULT 0,"
        "  enqueued_at   REAL"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_work_claim ON work_queue(stage, status, next_retry_at);"
    )


def _v11_rescue_from_redistill(db: sqlite3.Connection) -> None:
    # Cutover: the ad-hoc pending_redistill recovery queue becomes the durable bus
    # 'rescue' stage. Move any parked deltas into work_queue (idempotent on msg_id)
    # so the switch loses nothing, then drain the old table. Runs after _v10 (the
    # work_queue table exists). The old table is left in place (empty, harmless).
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pending_redistill'").fetchone():
        return
    rows = db.execute("SELECT project_key, session_id, text, fact_ids, created_at FROM pending_redistill").fetchall()
    for project_key, session_id, text, fact_ids, created_at in rows:
        payload = json.dumps(
            {
                "text": text,
                "fact_ids": json.loads(fact_ids) if fact_ids else [],
                "session_id": session_id or "",
                "project_key": project_key,
            }
        )
        db.execute(
            "INSERT OR IGNORE INTO work_queue "
            "(msg_id, stage, project_key, session_id, ref, payload, status, attempts, "
            " next_retry_at, lease_owner, lease_expires, enqueued_at) "
            "VALUES (?, 'rescue', ?, ?, '', ?, 'pending', 0, 0, NULL, 0, ?)",
            ("rescue:" + _content_id(project_key, text), project_key, session_id or "", payload, created_at or 0.0),
        )
    db.execute("DELETE FROM pending_redistill")


def _v13_usage(db: sqlite3.Connection) -> None:
    # Usage ledger for the effectiveness dashboard (`engram stats`): the two sides of the
    # token budget. `inject_*` rows record what claude-engram ADDS (bytes injected per
    # prompt / at session start — the cost); `pull_*` rows record what it SAVES (a
    # targeted get_symbol/get_doc_section read instead of the whole file — bytes_saved =
    # file - body). Best-effort, append-only, aggregated by kind.
    db.executescript(
        "CREATE TABLE IF NOT EXISTS usage_events ("
        "  id          INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  ts          REAL,"
        "  project_key TEXT,"
        "  kind        TEXT,"
        "  bytes_in    INTEGER DEFAULT 0,"
        "  bytes_saved INTEGER DEFAULT 0"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_usage_project ON usage_events(project_key);"
    )


def _v14_outcomes(db: sqlite3.Connection) -> None:
    # Use-feedback tallies (Engle/Kane executive attention): how often a fact was injected
    # into the focus vs actually engaged with. Feeds the retention-score inhibition term.
    # Additive, default 0; the inhibition weight stays 0 until a "used" detector is wired,
    # so these accumulate for that follow-up without affecting ranking yet.
    _add_columns(
        db,
        [
            ("injected_count", "injected_count INTEGER NOT NULL DEFAULT 0"),
            ("used_count", "used_count INTEGER NOT NULL DEFAULT 0"),
        ],
    )


def _v15_edges(db: sqlite3.Connection) -> None:
    # Associative graph (ACT-R spreading activation): undirected edges between facts that
    # co-occurred in a capture or share an extracted entity. Recorded only when spreading is
    # enabled (spread_weight > 0), so the table stays empty by default; weight accumulates on
    # repeat. Deleted with their facts by the caller's prune path.
    db.executescript(
        "CREATE TABLE IF NOT EXISTS fact_edges ("
        "  src_id TEXT NOT NULL,"
        "  dst_id TEXT NOT NULL,"
        "  kind   TEXT NOT NULL,"
        "  weight REAL NOT NULL DEFAULT 1.0,"
        "  PRIMARY KEY (src_id, dst_id, kind)"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_edges_src ON fact_edges(src_id);"
        "CREATE INDEX IF NOT EXISTS idx_edges_dst ON fact_edges(dst_id);"
    )


def _v12_index_meta(db: sqlite3.Connection) -> None:
    # Human name for a project's index. The index keys on hash(path) and stores only
    # relative source paths, so a project with chunks but no memory facts had nothing
    # to label it with and rendered as a raw hash in the viewer. Recorded per index
    # run; the viewer falls back to it when a project has no facts-derived label.
    db.executescript(
        "CREATE TABLE IF NOT EXISTS index_meta ("
        "  project_key TEXT PRIMARY KEY,"
        "  label       TEXT,"
        "  path        TEXT,"
        "  updated_at  REAL"
        ");"
    )


def _v16_sensory(db: sqlite3.Connection) -> None:
    # Atkinson-Shiffrin sensory register: the single intake stage all perception enters
    # (visual page snapshots, verbal conversation snippets), holding the raw perceived text
    # briefly. One row per perception, tagged by modality (visual|verbal); it decays unless
    # attended. Promotion to the durable store (index for visual, facts for verbal) is gated
    # by attention (A-S selective read-out, NOT rehearsal). Content-addressed for idempotency;
    # decayed_at soft-tombstones rows that have left the live register (NULL = live).
    db.executescript(
        "CREATE TABLE IF NOT EXISTS sensory ("
        "  id             TEXT PRIMARY KEY,"
        "  project_key    TEXT NOT NULL,"
        "  modality       TEXT NOT NULL,"
        "  observation_id TEXT,"
        "  url            TEXT,"
        "  text           TEXT NOT NULL,"
        "  attended       INTEGER NOT NULL DEFAULT 0,"
        "  created_at     REAL NOT NULL,"
        "  decayed_at     REAL"
        ");"
        "CREATE INDEX IF NOT EXISTS idx_sensory_project ON sensory(project_key);"
        "CREATE INDEX IF NOT EXISTS idx_sensory_modality ON sensory(project_key, modality);"
        "CREATE INDEX IF NOT EXISTS idx_sensory_created ON sensory(created_at);"
    )


# Ordered schema migrations. user_version marks how many have run; every step is
# also individually idempotent (ADD COLUMN only if missing, CREATE ... IF NOT
# EXISTS, rebuild only on first creation), so a database at any prior version —
# including the legacy FTS flag of 1 — converges by running the rest as no-ops.
_MIGRATIONS = [
    _v1_lifecycle,
    _v2_structured,
    _v3_fts,
    _v4_observations,
    _v5_subtitle,
    _v6_fts_widen,
    _v7_index,
    _v8_redistill,
    _v9_stm,
    _v10_work_queue,
    _v11_rescue_from_redistill,
    _v12_index_meta,
    _v13_usage,
    _v14_outcomes,
    _v15_edges,
    _v16_sensory,
]
_SCHEMA_VERSION = len(_MIGRATIONS)


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=5.0)
        self.db.row_factory = sqlite3.Row
        # WAL lets concurrent hook processes (capture, per-edit reindex, recall, the
        # viewer) read while one writes; busy_timeout waits out a brief write lock
        # instead of raising; NORMAL sync is durable enough under WAL and much faster.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("PRAGMA synchronous=NORMAL")
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
        return _content_id(project_key, text)

    def exists(self, fact_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM facts WHERE id = ?", (fact_id,)).fetchone() is not None

    def reinforce(self, fact_id: str, now: float | None = None) -> int:
        """Consolidation — strengthen a fact seen again and refresh its recency.

        Returns the fact's new frequency so the caller can decide promotion
        (STM→LTM on rehearsal); 0 if the fact is absent.
        """
        self.db.execute(
            "UPDATE facts SET frequency = frequency + 1, last_seen = ?, status = 'active' WHERE id = ?",
            (_now(now), fact_id),
        )
        self.db.commit()
        row = self.db.execute("SELECT frequency FROM facts WHERE id = ?", (fact_id,)).fetchone()
        return int(row[0]) if row else 0

    def promote(self, fact_id: str, now: float | None = None) -> None:
        """Rehearsal transfer — move a short-term fact into the long-term store."""
        self.db.execute(
            "UPDATE facts SET tier = 'ltm', last_seen = ? WHERE id = ? AND tier = 'stm'",
            (_now(now), fact_id),
        )
        self.db.commit()

    def stm_rows(self, project_key: str, limit: int | None = None) -> list[sqlite3.Row]:
        """Active short-term facts for a project, weakest first (frequency, then oldest seen)."""
        sql = (
            "SELECT * FROM facts WHERE project_key = ? AND status = 'active' AND tier = 'stm' "
            "ORDER BY frequency ASC, last_seen ASC"
        )
        params: list = [project_key]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return self.db.execute(sql, params).fetchall()

    def merge_candidates(self, project_key: str, limit: int) -> list[sqlite3.Row]:
        """Recent active short-term facts — the integrate stage's dedup pool (newest first).

        Bounded by ``limit`` so clustering stays O(limit²) off the hot path regardless of
        store size. STM only: the fresh, not-yet-consolidated set is where near-duplicates
        collect (cross-tier dupes are already caught by supersession at capture)."""
        return self.db.execute(
            "SELECT * FROM facts WHERE project_key = ? AND status = 'active' AND tier = 'stm' "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (project_key, limit),
        ).fetchall()

    def displace_stm(self, project_key: str, capacity: int) -> int:
        """Short-term displacement — archive the weakest active STM facts beyond ``capacity``.

        ``capacity <= 0`` disables displacement (the default). Archival is reversible
        (``status='displaced'``), never a delete; recall already scans ``status='active'``
        so displaced facts simply leave the search set. Returns the number archived.
        """
        if capacity <= 0:
            return 0
        rows = self.stm_rows(project_key)  # weakest-first
        overflow = rows[: max(0, len(rows) - capacity)]  # keep the strongest ``capacity``
        ids = [r["id"] for r in overflow]
        if not ids:
            return 0
        placeholders = _placeholders(ids)
        cur = self.db.execute(
            f"UPDATE facts SET status = 'displaced' WHERE id IN ({placeholders})",
            ids,
        )
        self.db.commit()
        return cur.rowcount

    def mark_recalled(self, fact_ids: list[str], now: float | None = None) -> int:
        """Retrieval attribution — record that facts were recalled (testing/spacing signal).

        Increments ``recall_count`` and refreshes ``last_recalled`` — the retention-score
        inputs (design §3A). Called off the interactive hot path (from the on-demand
        ``recall_structured``, not the per-prompt hook). Returns rows updated.
        """
        if not fact_ids:
            return 0
        stamp = _now(now)
        placeholders = _placeholders(fact_ids)
        cur = self.db.execute(
            f"UPDATE facts SET recall_count = recall_count + 1, last_recalled = ? WHERE id IN ({placeholders})",
            (stamp, *fact_ids),
        )
        self.db.commit()
        return cur.rowcount

    def mark_injected(self, fact_ids: list[str]) -> int:
        """Use-feedback: record that facts were injected into the focus (Engle/Kane).

        The denominator of the inhibition signal. Off the interactive hot path in the
        current design (wiring the injection tally into recall is a follow-up); safe to call
        wherever injected ids are known. Returns rows updated.
        """
        if not fact_ids:
            return 0
        cur = self.db.execute(
            f"UPDATE facts SET injected_count = injected_count + 1 WHERE id IN ({_placeholders(fact_ids)})",
            tuple(fact_ids),
        )
        self.db.commit()
        return cur.rowcount

    def mark_used(self, fact_ids: list[str]) -> int:
        """Use-feedback: record that injected facts were actually engaged with.

        The numerator of the inhibition signal — driven by a "used" detector
        (token-reappearance / edit-content / correction-turn), which is a follow-up.
        Returns rows updated.
        """
        if not fact_ids:
            return 0
        cur = self.db.execute(
            f"UPDATE facts SET used_count = used_count + 1 WHERE id IN ({_placeholders(fact_ids)})",
            tuple(fact_ids),
        )
        self.db.commit()
        return cur.rowcount

    def add_edges(self, edges: list[tuple[str, str, str, float]]) -> int:
        """Upsert undirected association edges ``(src_id, dst_id, kind, weight)``.

        The caller normalises pair order (src < dst) so an undirected link is one row.
        Weight accumulates on repeat (co-occurring again strengthens the link). Returns the
        number of edge rows submitted.
        """
        if not edges:
            return 0
        self.db.executemany(
            "INSERT INTO fact_edges (src_id, dst_id, kind, weight) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(src_id, dst_id, kind) DO UPDATE SET weight = weight + excluded.weight",
            edges,
        )
        self.db.commit()
        return len(edges)

    def neighbours(self, fact_ids: list[str], limit: int = 512) -> list[tuple[str, str, float]]:
        """Edges incident to any of ``fact_ids`` (bounded by ``limit``). Off by default —
        only queried when spreading activation is enabled. Returns ``(src, dst, weight)``."""
        if not fact_ids:
            return []
        ph = _placeholders(fact_ids)
        rows = self.db.execute(
            f"SELECT src_id, dst_id, weight FROM fact_edges WHERE src_id IN ({ph}) OR dst_id IN ({ph}) LIMIT ?",
            (*fact_ids, *fact_ids, limit),
        ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    def supersede_count(self, fact_id: str) -> int:
        """How many facts this one superseded — the retention 'surprise' signal (§3A)."""
        return self.db.execute("SELECT COUNT(*) FROM facts WHERE superseded_by = ?", (fact_id,)).fetchone()[0]

    def set_status(self, fact_ids: list[str], status: str) -> int:
        """Archive a set of facts under ``status`` (reversible; recall scans 'active' only)."""
        if not fact_ids:
            return 0
        cur = self.db.execute(
            f"UPDATE facts SET status = ? WHERE id IN ({_placeholders(fact_ids)})",
            (status, *fact_ids),
        )
        self.db.commit()
        return cur.rowcount

    def purge(self, horizon_seconds: float, now: float | None = None) -> int:
        """Two-stage lifecycle backstop — hard-delete long-archived facts, then VACUUM.

        The ONLY true delete: rows already archived (superseded/displaced/merged/pruned/
        expired) and untouched for longer than ``horizon_seconds``. Opt-in (disabled at 0).
        The FTS index stays in sync via the delete trigger.
        """
        cutoff = _now(now) - horizon_seconds
        cur = self.db.execute(
            "DELETE FROM facts WHERE status IN ('superseded', 'displaced', 'merged', 'pruned', 'expired') "
            "AND COALESCE(last_seen, created_at) < ?",
            (cutoff,),
        )
        self.db.commit()
        deleted = cur.rowcount
        if deleted:
            try:
                self.db.execute("VACUUM")
            except sqlite3.OperationalError:
                pass  # a concurrent reader can block VACUUM; space reclaim is best-effort
        return deleted

    def supersede(self, fact_ids: list[str], by_id: str) -> int:
        """Retroactive interference — archive facts replaced by a newer one."""
        if not fact_ids:
            return 0
        placeholders = _placeholders(fact_ids)
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
        tier: str = "stm",
    ) -> bool:
        fid = self.fact_id(project["key"], text)
        stamp = created_at if created_at is not None else time.time()
        cur = self.db.execute(
            "INSERT OR IGNORE INTO facts "
            "(id, project_key, project_label, project_path, session_id, kind, text, "
            " title, subtitle, narrative, files, type, observation_id, created_at, last_seen, dim, scale, "
            " vec_int8, vec_bits, importance, frequency, status, tier) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'active', ?)",
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
                tier,
            ),
        )
        self.db.commit()
        return cur.rowcount > 0

    def get(self, fact_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()

    def rows_for_project(self, project_key: str, limit: int | None = None, offset: int = 0) -> list[sqlite3.Row]:
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

    def active_antipatterns(self, project_key: str) -> list[sqlite3.Row]:
        """Active anti-pattern facts for a key — the recall union (global key) and the
        'existing anti-patterns' fed to the extraction prompt both read this."""
        return self.db.execute(
            "SELECT * FROM facts WHERE project_key = ? AND status = 'active' AND kind = 'antipattern'",
            (project_key,),
        ).fetchall()

    def list_observations(
        self,
        project_key: str,
        limit: int | None = None,
        offset: int = 0,
        tier: str | None = None,
        active: bool | None = None,
    ) -> list[list[sqlite3.Row]]:
        """Facts grouped into observation cards, newest group first, paginated by group.

        A group is the facts sharing an observation_id (falling back to the fact's own
        id for ungrouped rows), returned as an ordered list of its fact rows. Optional
        filters: ``tier`` ('stm'/'ltm') and ``active`` (True = status='active' only,
        False = archived only) — used by the viewer's STM / LTM / Consolidation tabs.
        """
        grp = "COALESCE(observation_id, id)"
        cond = "project_key = ?"
        cparams: list = [project_key]
        if tier is not None:
            cond += " AND tier = ?"
            cparams.append(tier)
        if active is True:
            cond += " AND status = 'active'"
        elif active is False:
            cond += " AND status != 'active'"
        sql = f"SELECT {grp} AS grp, MAX(created_at) AS ts, MAX(rowid) AS rid FROM facts WHERE {cond} GROUP BY grp ORDER BY ts DESC, rid DESC"
        params = list(cparams)
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params += [limit, offset]
        groups = self.db.execute(sql, params).fetchall()
        return [
            self.db.execute(
                f"SELECT * FROM facts WHERE {cond} AND {grp} = ? ORDER BY rowid ASC",
                (*cparams, row["grp"]),
            ).fetchall()
            for row in groups
        ]

    def work_items(self, project_key: str, limit: int = 200) -> list[sqlite3.Row]:
        """Work-queue rows for a project (all stages/statuses), newest first — the Consolidation view."""
        return self.db.execute(
            "SELECT * FROM work_queue WHERE project_key = ? ORDER BY enqueued_at DESC, rowid DESC LIMIT ?",
            (project_key, limit),
        ).fetchall()

    def recent_work(self, limit: int = 50) -> list[sqlite3.Row]:
        """Work-queue rows across all projects, newest first — the `engram queue` inspection view."""
        return self.db.execute(
            "SELECT * FROM work_queue ORDER BY enqueued_at DESC, rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def purge_work(self, status: str | None = None, stage: str | None = None) -> int:
        """Delete work-queue rows, optionally filtered by status and/or stage. Returns count.

        The maintenance op behind `engram queue purge` — clears a backlog/DLQ that can't or
        shouldn't be retried. With no filter it empties the queue entirely."""
        sql = "DELETE FROM work_queue WHERE 1=1"
        params: list = []
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        if stage is not None:
            sql += " AND stage = ?"
            params.append(stage)
        cur = self.db.execute(sql, params)
        self.db.commit()
        return cur.rowcount

    def active_rows(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM facts WHERE status = 'active'").fetchall()

    def stored_dims(self, project_key: str) -> set[int]:
        """Distinct embedding dimensions of a project's active facts.

        Lets recall detect a write/read embedding-space divergence: if the query
        embedder's dimension is absent here, every candidate was silently skipped
        by the dim gate and the result would otherwise masquerade as 'no memory'.
        """
        rows = self.db.execute(
            "SELECT DISTINCT dim FROM facts WHERE project_key = ? AND status = 'active' AND dim IS NOT NULL",
            (project_key,),
        ).fetchall()
        return {row[0] for row in rows}

    def recent(self, project_key: str, limit: int) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM facts WHERE project_key = ? AND status = 'active' "
            "ORDER BY frequency DESC, last_seen DESC LIMIT ?",
            (project_key, limit),
        ).fetchall()

    def latest_summary(self, project_key: str) -> sqlite3.Row | None:
        """The newest session summary for a project — the SessionStart orientation snapshot."""
        return self.db.execute(
            "SELECT * FROM facts WHERE project_key = ? AND kind = 'session_summary' AND status = 'active' "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (project_key,),
        ).fetchone()

    def projects(self) -> list[sqlite3.Row]:
        """Active-fact projects with total (``c``) and per-tier (``stm``/``ltm``) counts,
        newest first. The tier counts back the viewer's per-panel dropdown totals."""
        return self.db.execute(
            "SELECT project_key, project_label, project_path, "
            "COUNT(*) AS c, "
            "SUM(tier = 'stm') AS stm, "
            "SUM(tier = 'ltm') AS ltm, "
            "MAX(created_at) AS last "
            "FROM facts WHERE status = 'active' GROUP BY project_key ORDER BY last DESC"
        ).fetchall()

    # --- Sensory register (A-S: one modality-tagged intake for all perception) ---
    # A separate table, never read by recall or index search. Promotion (later phases) copies
    # attended perceptions into the durable store; decay soft-tombstones the rest via decayed_at.

    def add_sensory(
        self,
        project_key: str,
        modality: str,
        text: str,
        *,
        url: str | None = None,
        observation_id: str | None = None,
        now: float | None = None,
    ) -> str:
        """Record a perception in the sensory register (the A-S intake stage), holding its raw
        text. Content-addressed per (project, modality, url, text): re-perceiving the identical
        thing is idempotent — it refreshes recency and revives a decayed tombstone rather than
        duplicating. Attention (which gates promotion) is set separately via ``mark_attended``.
        Returns the sensory id."""
        sid = _sensory_id(project_key, modality, url or "", text)
        self.db.execute(
            "INSERT INTO sensory (id, project_key, modality, observation_id, url, text, attended, created_at, decayed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, ?, NULL) "
            "ON CONFLICT(id) DO UPDATE SET created_at = excluded.created_at, text = excluded.text, decayed_at = NULL",
            (sid, project_key, modality, observation_id, url, text, _now(now)),
        )
        self.db.commit()
        return sid

    def sensory_rows(
        self, project_key: str, limit: int | None = None, *, include_decayed: bool = False
    ) -> list[sqlite3.Row]:
        """Perceptions for a project, newest first (the viewer's Sensory panel). By default only
        the live register (``decayed_at IS NULL``); ``include_decayed`` also returns rows that
        have left the register."""
        sql = "SELECT * FROM sensory WHERE project_key = ?"
        if not include_decayed:
            sql += " AND decayed_at IS NULL"
        sql += " ORDER BY created_at DESC, rowid DESC"
        params: list = [project_key]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return self.db.execute(sql, tuple(params)).fetchall()

    def mark_attended(self, sensory_id: str) -> None:
        """Flag a perception as attended — the A-S selective read-out that gates promotion into
        the durable store. Set by the intake shell (visual: re-perception of the same page;
        verbal: distillation-worthiness), never by a rehearsal/frequency count."""
        self.db.execute("UPDATE sensory SET attended = 1 WHERE id = ?", (sensory_id,))
        self.db.commit()

    def sweep_sensory(self, project_key: str, capacity: int, ttl_seconds: float, now: float | None = None) -> int:
        """Decay the register (A-S 'lost from SR'). Soft-tombstones (sets ``decayed_at``)
        UNATTENDED perceptions older than ``ttl_seconds`` and unattended live perceptions beyond
        ``capacity`` (newest kept), then hard-purges tombstones older than ``ttl_seconds`` so the
        table stays bounded. Attended perceptions are left for the promotion pass. Returns the
        number newly tombstoned. A 0/None limit disables that limb."""
        t = _now(now)
        decayed = 0
        if ttl_seconds and ttl_seconds > 0:
            cur = self.db.execute(
                "UPDATE sensory SET decayed_at = ? WHERE project_key = ? AND decayed_at IS NULL "
                "AND attended = 0 AND created_at < ?",
                (t, project_key, t - ttl_seconds),
            )
            decayed += cur.rowcount
        if capacity and capacity > 0:
            cur = self.db.execute(
                "UPDATE sensory SET decayed_at = ? WHERE project_key = ? AND decayed_at IS NULL "
                "AND attended = 0 AND id NOT IN ("
                "SELECT id FROM sensory WHERE project_key = ? AND decayed_at IS NULL "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?)",
                (t, project_key, project_key, capacity),
            )
            decayed += cur.rowcount
        if ttl_seconds and ttl_seconds > 0:
            self.db.execute(
                "DELETE FROM sensory WHERE project_key = ? AND decayed_at IS NOT NULL AND decayed_at < ?",
                (project_key, t - ttl_seconds),
            )
        self.db.commit()
        return decayed

    def sensory_counts(self) -> dict[str, int]:
        """Per-project LIVE sensory-perception count for the viewer's Sensory panel."""
        return {
            r["project_key"]: r["c"]
            for r in self.db.execute(
                "SELECT project_key, COUNT(*) AS c FROM sensory WHERE decayed_at IS NULL GROUP BY project_key"
            )
        }

    def sensory_stats(self, project_key: str) -> dict[str, int]:
        """Live-register stats for one project (the viewer Sensory header + `doctor`): the number
        of live perceptions, how many are attended (awaiting/eligible for promotion — the signal
        that tells whether attention is firing), and the visual/verbal split."""
        row = self.db.execute(
            "SELECT COUNT(*) AS live, COALESCE(SUM(attended), 0) AS attended, "
            "COALESCE(SUM(modality = 'visual'), 0) AS visual, COALESCE(SUM(modality = 'verbal'), 0) AS verbal "
            "FROM sensory WHERE project_key = ? AND decayed_at IS NULL",
            (project_key,),
        ).fetchone()
        return {"live": row["live"], "attended": row["attended"], "visual": row["visual"], "verbal": row["verbal"]}

    def sensory_counts_by_status(self, project_key: str) -> dict[str, int]:
        """Per-project sensory promotion-rate counters for the viewer.

        - perceived: alive perceptions (decayed_at IS NULL)
        - attended: attended alive perceptions
        - promoted: perceptions linked to facts/index (observation_id IS NOT NULL)
        - decayed: departed perceptions (decayed_at IS NOT NULL)
        """
        rows = self.db.execute(
            "SELECT "
            "SUM(CASE WHEN decayed_at IS NULL THEN 1 ELSE 0 END) as perceived, "
            "SUM(CASE WHEN decayed_at IS NULL AND attended = 1 THEN 1 ELSE 0 END) as attended, "
            "SUM(CASE WHEN observation_id IS NOT NULL THEN 1 ELSE 0 END) as promoted, "
            "SUM(CASE WHEN decayed_at IS NOT NULL THEN 1 ELSE 0 END) as decayed "
            "FROM sensory WHERE project_key = ?",
            (project_key,),
        ).fetchone()
        return {
            "perceived": rows["perceived"] or 0,
            "attended": rows["attended"] or 0,
            "promoted": rows["promoted"] or 0,
            "decayed": rows["decayed"] or 0,
        }

    def delete_sensory(self, sensory_id: str) -> int:
        """Hard-delete one perception by id (the viewer's Sensory-card trash). Returns rows removed."""
        if not sensory_id:
            return 0
        cur = self.db.execute("DELETE FROM sensory WHERE id = ?", (sensory_id,))
        self.db.commit()
        return cur.rowcount

    @staticmethod
    def sensory_id(project_key: str, modality: str, url: str, text: str) -> str:
        """Content-addressed id for a sensory perception — the same id ``add_sensory`` assigns,
        so the intake shell can ask "is this exact perception already registered?" before insert."""
        return _sensory_id(project_key, modality, url or "", text)

    def sensory_get(self, sensory_id: str) -> sqlite3.Row | None:
        """One sensory row by id, or None."""
        return self.db.execute("SELECT * FROM sensory WHERE id = ?", (sensory_id,)).fetchone()

    def mark_sensory_decayed(self, sensory_id: str, now: float | None = None) -> None:
        """Mark a perception as having left the live register — decayed OR promoted into the
        durable store. Sets ``decayed_at`` (once; a no-op on an already-departed row)."""
        self.db.execute(
            "UPDATE sensory SET decayed_at = ? WHERE id = ? AND decayed_at IS NULL",
            (_now(now), sensory_id),
        )
        self.db.commit()

    def consolidation_counts(self) -> dict[str, int]:
        """Per-project count for the viewer's Consolidation panel: archived ('forgotten')
        facts plus pending work-queue items — the two populations that panel shows."""
        counts: dict[str, int] = {}
        for r in self.db.execute(
            "SELECT project_key, COUNT(*) AS c FROM facts WHERE status != 'active' GROUP BY project_key"
        ):
            counts[r["project_key"]] = r["c"]
        for r in self.db.execute("SELECT project_key, COUNT(*) AS c FROM work_queue GROUP BY project_key"):
            counts[r["project_key"]] = counts.get(r["project_key"], 0) + r["c"]
        return counts

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
        # Anti-patterns never expire by dormancy — they are standing rules (see refine()).
        sql = (
            "UPDATE facts SET status = 'expired' WHERE status = 'active' "
            "AND kind != 'antipattern' AND last_seen < ? AND frequency < ?"
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

    def delete_project(self, project_key: str) -> dict[str, int]:
        """Erase every trace of a project across all tables, in one transaction.

        A project appears in the viewer if it has memory facts *or* index chunks, so a
        clean removal must wipe both plus the cross-cutting tables (work queue, telemetry,
        cursors, index label). Returns per-table row counts for reporting. Orphaned
        ``fact_edges`` (edges whose endpoints are gone) are swept globally after the
        facts delete. Destructive and irreversible — the caller confirms intent.
        """
        counts: dict[str, int] = {}
        with self.db:
            counts["facts"] = self.db.execute("DELETE FROM facts WHERE project_key = ?", (project_key,)).rowcount
            counts["chunks"] = self.db.execute("DELETE FROM chunks WHERE project_key = ?", (project_key,)).rowcount
            self.db.execute("DELETE FROM chunk_sources WHERE project_key = ?", (project_key,))
            counts["work_queue"] = self.db.execute(
                "DELETE FROM work_queue WHERE project_key = ?", (project_key,)
            ).rowcount
            self.db.execute("DELETE FROM pending_redistill WHERE project_key = ?", (project_key,))
            self.db.execute("DELETE FROM recall_events WHERE project_key = ?", (project_key,))
            self.db.execute("DELETE FROM usage_events WHERE project_key = ?", (project_key,))
            self.db.execute("DELETE FROM index_meta WHERE project_key = ?", (project_key,))
            # cursor_key is "{project_key}:{session}" — match this project's cursors by prefix.
            self.db.execute("DELETE FROM capture_cursors WHERE cursor_key LIKE ? || ':%'", (project_key,))
            # Sweep edges left dangling by the facts delete (edges are keyed by fact id, not project).
            self.db.execute(
                "DELETE FROM fact_edges WHERE src_id NOT IN (SELECT id FROM facts) "
                "OR dst_id NOT IN (SELECT id FROM facts)"
            )
        return counts

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
                (_now(now), project_key, query, returned, top_sim, confidence, verdict),
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

    def record_usage(
        self, project_key: str, kind: str, *, bytes_in: int = 0, bytes_saved: int = 0, now: float | None = None
    ) -> None:
        """Append one usage-ledger row (cost=bytes_in / saving=bytes_saved). Best-effort —
        a telemetry failure must never break recall, capture, or a pull."""
        try:
            self.db.execute(
                "INSERT INTO usage_events (ts, project_key, kind, bytes_in, bytes_saved) VALUES (?, ?, ?, ?, ?)",
                (_now(now), project_key, kind, bytes_in, bytes_saved),
            )
            self.db.commit()
        except sqlite3.Error:
            pass

    def usage_stats(self, project_key: str | None = None) -> dict:
        """Aggregate the usage ledger by kind: {kind: {n, bytes_in, bytes_saved}}."""
        where = "WHERE project_key = ?" if project_key else ""
        params = (project_key,) if project_key else ()
        rows = self.db.execute(
            f"SELECT kind, COUNT(*) AS n, SUM(bytes_in) AS bi, SUM(bytes_saved) AS bs "
            f"FROM usage_events {where} GROUP BY kind",
            params,
        ).fetchall()
        return {r["kind"]: {"n": r["n"], "bytes_in": r["bi"] or 0, "bytes_saved": r["bs"] or 0} for r in rows}

    def data_version(self) -> int:
        """SQLite change counter — bumps on every commit by another connection (cache-invalidation signal)."""
        return self.db.execute("PRAGMA data_version").fetchone()[0]

    def get_capture_cursor(self, cursor_key: str) -> int:
        """Byte offset already distilled for this session, so incremental capture reads only new turns."""
        row = self.db.execute("SELECT offset FROM capture_cursors WHERE cursor_key = ?", (cursor_key,)).fetchone()
        return row["offset"] if row else 0

    def set_capture_cursor(self, cursor_key: str, offset: int, now: float | None = None) -> None:
        self.db.execute(
            "INSERT INTO capture_cursors (cursor_key, offset, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(cursor_key) DO UPDATE SET offset = excluded.offset, updated_at = excluded.updated_at",
            (cursor_key, offset, _now(now)),
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
        rows = self.db.execute("SELECT source_path FROM chunk_sources WHERE project_key = ?", (project_key,)).fetchall()
        return {row[0] for row in rows}

    def replace_source_chunks(
        self,
        project_key: str,
        source_path: str,
        chunks: list[dict],
        file_hash: str,
        mtime_ns: int,
        now: float | None = None,
    ) -> int:
        """Atomically swap a file's chunks for a freshly-parsed set and stamp its source state.

        Delete-then-insert keeps the index in step with the file even when sections are
        removed or renamed; the whole swap is one transaction so a crash can't leave a
        half-indexed file. Returns the number of chunks written.
        """
        stamp = _now(now)
        with self.db:
            self.db.execute("DELETE FROM chunks WHERE project_key = ? AND source_path = ?", (project_key, source_path))
            self._insert_chunk_rows(project_key, source_path, chunks, stamp)
            self.db.execute(
                "INSERT INTO chunk_sources (project_key, source_path, file_hash, mtime_ns, indexed_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(project_key, source_path) DO UPDATE SET "
                "file_hash = excluded.file_hash, mtime_ns = excluded.mtime_ns, indexed_at = excluded.indexed_at",
                (project_key, source_path, file_hash, mtime_ns, stamp),
            )
        return len(chunks)

    def _insert_chunk_rows(self, project_key: str, source_path: str, chunks: list[dict], stamp: float) -> None:
        """The shared chunk INSERT for both the file writer (replace_source_chunks) and the
        snapshot writer (replace_snapshot_chunks) — the column list lives in exactly one place."""
        self.db.executemany(
            "INSERT OR REPLACE INTO chunks "
            "(id, project_key, source_path, kind, anchor, title, heading_path, level, "
            " summary, body, byte_start, byte_end, content_hash, dim, scale, vec_int8, indexed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    c["id"],
                    project_key,
                    source_path,
                    c.get("kind", "doc_section"),
                    c["anchor"],
                    c["title"],
                    c["heading_path"],
                    c["level"],
                    c.get("summary") or None,
                    c["body"],
                    c["byte_start"],
                    c["byte_end"],
                    c["content_hash"],
                    c["dim"],
                    c["scale"],
                    c["vec_int8"],
                    stamp,
                )
                for c in chunks
            ],
        )

    def replace_snapshot_chunks(self, project_key: str, url: str, chunks: list[dict], now: float | None = None) -> int:
        """Swap the snapshot chunk(s) for a URL — the index's visual long-term-store column. Unlike
        replace_source_chunks this writes NO chunk_sources row: a snapshot's source is a URL, not a
        file on disk, so it is exempt from file drift-reconciliation (index_project reconciles the
        filesystem against chunk_sources) and gets age-based freshness at read time. Delete-then-insert
        scoped to kind='snapshot'. Returns the number of chunks written."""
        stamp = _now(now)
        with self.db:
            self.db.execute(
                "DELETE FROM chunks WHERE project_key = ? AND source_path = ? AND kind = 'snapshot'",
                (project_key, url),
            )
            self._insert_chunk_rows(project_key, url, chunks, stamp)
        return len(chunks)

    def delete_source(self, project_key: str, source_path: str) -> None:
        """Drop a vanished file's chunks and source row (called for files gone since last index)."""
        with self.db:
            self.db.execute("DELETE FROM chunks WHERE project_key = ? AND source_path = ?", (project_key, source_path))
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
        self, project_key: str, source_path: str | None = None, kind: str | None = None
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
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY source_path, byte_start"
        return self.db.execute(sql, params).fetchall()

    def chunk_rows(self, project_key: str, kind: str | None = None) -> list[sqlite3.Row]:
        """All chunk rows for a project (vector-channel scan input), optionally one kind."""
        sql = "SELECT * FROM chunks WHERE project_key = ?"
        params: list = [project_key]
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        return self.db.execute(sql, params).fetchall()

    def chunk_fts_search(self, project_key: str, query: str, limit: int = 50, kind: str | None = None) -> list[str]:
        """Chunk ids matching an FTS5 keyword query, best-ranked first (weighted columns)."""
        match = _fts_match_expr(query)
        if not match:
            return []
        sql = (
            "SELECT c.id FROM chunks_fts JOIN chunks c ON c.rowid = chunks_fts.rowid "
            "WHERE chunks_fts MATCH ? AND c.project_key = ?"
        )
        params: list = [match, project_key]
        if kind is not None:
            sql += " AND c.kind = ?"
            params.append(kind)
        sql += " ORDER BY bm25(chunks_fts, 3.0, 2.0, 1.5, 1.0) LIMIT ?"
        params.append(limit)
        return [row[0] for row in self.db.execute(sql, params).fetchall()]

    def set_index_meta(self, project: Project) -> None:
        """Record a project's human label/path for the index, so an index-only project
        (chunks but no memory facts) still shows a real name instead of its raw key."""
        self.db.execute(
            "INSERT INTO index_meta (project_key, label, path, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(project_key) DO UPDATE SET "
            "label = excluded.label, path = excluded.path, updated_at = excluded.updated_at",
            (project["key"], project.get("label") or project["key"], project.get("path") or "", _now(None)),
        )
        self.db.commit()

    def chunk_projects(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT c.project_key AS project_key, COUNT(*) AS c, "
            "COUNT(DISTINCT c.source_path) AS files, MAX(c.indexed_at) AS last, "
            "m.label AS label, m.path AS path "
            "FROM chunks c LEFT JOIN index_meta m ON m.project_key = c.project_key "
            "GROUP BY c.project_key ORDER BY last DESC"
        ).fetchall()

    def chunk_count(self, project_key: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM chunks WHERE project_key = ?", (project_key,)).fetchone()[0]

    def prune_chunks(self, project_key: str) -> int:
        with self.db:
            self.db.execute("DELETE FROM chunk_sources WHERE project_key = ?", (project_key,))
            cur = self.db.execute("DELETE FROM chunks WHERE project_key = ?", (project_key,))
        return cur.rowcount

    def project_meta(self, project_key: str) -> Project:
        """Reconstruct a project's {key, label, path} from any of its facts.

        Lets the global rescue drain re-add re-distilled facts under the right project
        without the caller passing a Project (the work item carries only the key).
        """
        row = self.db.execute(
            "SELECT project_label, project_path FROM facts WHERE project_key = ? LIMIT 1",
            (project_key,),
        ).fetchone()
        return {
            "key": project_key,
            "label": row["project_label"] if row and row["project_label"] else project_key,
            "path": row["project_path"] if row and row["project_path"] else "",
        }

    # ---- Durable work queue (MemoryBus inproc adapter) ----------------------------

    def enqueue_work(
        self,
        *,
        msg_id: str,
        stage: str,
        project_key: str,
        session_id: str = "",
        ref: str = "",
        payload: str = "",
        now: float | None = None,
    ) -> bool:
        """Publish a work item; idempotent on ``msg_id`` (INSERT OR IGNORE). True if new."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO work_queue "
            "(msg_id, stage, project_key, session_id, ref, payload, status, attempts, "
            " next_retry_at, lease_owner, lease_expires, enqueued_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, 0, NULL, 0, ?)",
            (msg_id, stage, project_key, session_id, ref, payload, _now(now)),
        )
        self.db.commit()
        return cur.rowcount > 0

    def claim_work(
        self, stage: str, limit: int, now: float | None = None, lease_ttl: float = 300.0, owner: str = "worker"
    ) -> list[sqlite3.Row]:
        """Lease up to ``limit`` due items for ``stage``, FIFO. Increments delivery count.

        Claimable = pending-and-due, or in_progress whose lease has expired (an
        interrupted worker's items — crash recovery). Sets a fresh lease and bumps
        ``attempts`` so the delivery count survives across workers.
        """
        now = _now(now)
        rows = self.db.execute(
            "SELECT * FROM work_queue WHERE stage = ? AND next_retry_at <= ? AND "
            "(status = 'pending' OR (status = 'in_progress' AND lease_expires < ?)) "
            "ORDER BY enqueued_at ASC, rowid ASC LIMIT ?",
            (stage, now, now, limit),
        ).fetchall()
        for row in rows:
            self.db.execute(
                "UPDATE work_queue SET status = 'in_progress', lease_owner = ?, lease_expires = ?, "
                "attempts = attempts + 1 WHERE msg_id = ?",
                (owner, now + lease_ttl, row["msg_id"]),
            )
        self.db.commit()
        return rows

    def ack_work(self, msg_id: str) -> None:
        """Work done — remove it from the queue."""
        self.db.execute("DELETE FROM work_queue WHERE msg_id = ?", (msg_id,))
        self.db.commit()

    def nak_work(self, msg_id: str, delay: float = 0.0, now: float | None = None) -> None:
        """Return work for retry after ``delay`` seconds; clears the lease."""
        now = _now(now)
        self.db.execute(
            "UPDATE work_queue SET status = 'pending', next_retry_at = ?, lease_owner = NULL, lease_expires = 0 "
            "WHERE msg_id = ?",
            (now + delay, msg_id),
        )
        self.db.commit()

    def dead_work(self, msg_id: str) -> None:
        """Dead-letter — retries exhausted or terminally unprocessable. Kept for inspection."""
        self.db.execute(
            "UPDATE work_queue SET status = 'dead', lease_owner = NULL, lease_expires = 0 WHERE msg_id = ?",
            (msg_id,),
        )
        self.db.commit()

    def reclaim_expired(self, now: float | None = None) -> int:
        """Return interrupted (expired-lease) in_progress items to pending. Crash recovery."""
        now = _now(now)
        cur = self.db.execute(
            "UPDATE work_queue SET status = 'pending', lease_owner = NULL, lease_expires = 0 "
            "WHERE status = 'in_progress' AND lease_expires < ?",
            (now,),
        )
        self.db.commit()
        return cur.rowcount

    def pending_work(self, limit: int = 500) -> list[sqlite3.Row]:
        """Pending/interrupted items across all stages+projects — the set to migrate on a
        bus-backend switch (dead rows are left; they're the DLQ). Oldest first."""
        return self.db.execute(
            "SELECT * FROM work_queue WHERE status IN ('pending', 'in_progress') "
            "ORDER BY enqueued_at ASC, rowid ASC LIMIT ?",
            (limit,),
        ).fetchall()

    def dead_stale(self, horizon_seconds: float, now: float | None = None) -> int:
        """Dead-letter pending items older than the horizon — a backstop so an item no active
        backend ever pulls (e.g. parked on inproc after a switch to nats) can't live forever.
        Kept for inspection (``status='dead'``), not deleted. Disabled at ``horizon<=0``."""
        if horizon_seconds <= 0:
            return 0
        cutoff = _now(now) - horizon_seconds
        cur = self.db.execute(
            "UPDATE work_queue SET status = 'dead', lease_owner = NULL, lease_expires = 0 "
            "WHERE status = 'pending' AND enqueued_at < ?",
            (cutoff,),
        )
        self.db.commit()
        return cur.rowcount

    def count_work(self, stage: str | None = None, status: str | None = None) -> int:
        """Count work items, optionally filtered by stage/status (inspection, tests)."""
        sql = "SELECT COUNT(*) FROM work_queue WHERE 1=1"
        params: list = []
        if stage is not None:
            sql += " AND stage = ?"
            params.append(stage)
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        return self.db.execute(sql, params).fetchone()[0]

    def delete_facts(self, fact_ids: list[str]) -> int:
        """Hard-delete facts by id (FTS stays in sync via the delete trigger). Used by recovery."""
        if not fact_ids:
            return 0
        placeholders = _placeholders(fact_ids)
        cur = self.db.execute(f"DELETE FROM facts WHERE id IN ({placeholders})", tuple(fact_ids))
        self.db.commit()
        return cur.rowcount

    def delete_memory(self, key: str) -> int:
        """Hard-delete one viewer 'memory' (card) by its key.

        A card's key is its ``observation_id`` (a group of atomic facts distilled together)
        or, for an ungrouped fact, its ``id`` — so this removes the whole observation group
        or the single fact. FTS stays in sync via the delete trigger; orphaned ``fact_edges``
        (spreading-activation links whose endpoints are gone) are swept afterwards. Returns
        the number of fact rows removed.
        """
        if not key:
            return 0
        with self.db:
            cur = self.db.execute("DELETE FROM facts WHERE observation_id = ? OR id = ?", (key, key))
            self.db.execute(
                "DELETE FROM fact_edges WHERE src_id NOT IN (SELECT id FROM facts) "
                "OR dst_id NOT IN (SELECT id FROM facts)"
            )
        return cur.rowcount
