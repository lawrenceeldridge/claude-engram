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
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from core.domain.ingest import ACTION_PREFIXES
from core.ports.workqueue import EXCHANGE_FORMAT
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


# What recall's scan and ranking read from a fact row (Store.scan_rows) — the rest stays on disk.
_SCAN_COLUMNS = "id, dim, scale, vec_int8, created_at, last_seen, frequency, tier"


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
# Run only by _v7_index (a released slot, so left as written); _v21 adds the (project_key, kind) and
# (project_key, anchor) lookup indexes.
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


def _chunk_scope(project_key: str, kind: str | None, source_path: str | None) -> tuple[str, list]:
    """The WHERE clause (and its params) scoping chunk queries to a project, and optionally one
    kind and/or one source — shared by the outline, vector-scan and FTS reads."""
    clauses, params = ["project_key = ?"], [project_key]
    for name, value in (("source_path", source_path), ("kind", kind)):
        if value is not None:
            clauses.append(f"{name} = ?")
            params.append(value)
    return " AND ".join(clauses), params


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
    # Durable Command queue for the WorkQueue inproc adapter — the at-least-once,
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
    # Cutover: the ad-hoc pending_redistill recovery queue becomes the durable queue's
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


# The one INSERT a new work item takes — Store.enqueue_work and the migrations that publish
# Commands share it (_v11, a released step, keeps its own literal copy unchanged).
_ENQUEUE_WORK = (
    "INSERT OR IGNORE INTO work_queue "
    "(msg_id, stage, project_key, session_id, ref, payload, status, attempts, "
    " next_retry_at, lease_owner, lease_expires, enqueued_at) "
    "VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, 0, NULL, 0, ?)"
)


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
    #
    # Self-healing: an earlier, later-reverted build shipped a *different* sensory schema in this
    # same slot. The migration ladder re-runs every step on any version mismatch, so this must
    # tolerate a pre-existing table of that old shape — drop it (the register is transient, so
    # stale rows are throwaway) before recreating, or the modality index below fails on it.
    cols = {r[1] for r in db.execute("PRAGMA table_info(sensory)")}
    if cols and "decayed_at" not in cols:
        db.execute("DROP TABLE IF EXISTS sensory")
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


def _v17_sensory_schema(db: sqlite3.Connection) -> None:
    # Version-bump migration: its purpose is to advance _SCHEMA_VERSION past 16 so a database the
    # buggy build stamped at user_version=16 (with the old sensory schema) is NOT fast-pathed on
    # the next open — it re-runs the ladder, where the now self-healing _v16_sensory reconciles the
    # table. The body just re-applies _v16_sensory, which is idempotent (a no-op once current).
    _v16_sensory(db)


def _v18_facts_browse_index(db: sqlite3.Connection) -> None:
    # Composite index for the viewer's grouped browse (STM / LTM / archived via
    # list_observations). Without it, paging a large tier means a full scan of every active
    # fact in the project to GROUP BY observation and sort by MAX(created_at) before LIMIT —
    # ~9s cold at 135k STM rows. Ordering the index by created_at turns a page into a compact,
    # created_at-ordered range read (~0.2s). idx_facts_tier stays for status-only lookups.
    db.execute("CREATE INDEX IF NOT EXISTS idx_facts_browse ON facts(project_key, tier, status, created_at)")


def _v19_fact_episode(db: sqlite3.Connection) -> None:
    # Provenance link from a fact to the episode (transcript delta) it was captured from — the
    # source key of that delta's verbatim `exchange` chunks, so on-demand recall can point at the
    # conversation behind a fact. Additive and nullable (facts not captured from a transcript, and
    # every fact written before this, stay NULL); self-healing via _add_columns.
    _add_columns(db, [("episode", "episode TEXT")])


def _v20_exchange_format(db: sqlite3.Connection) -> None:
    # Exchanges stored before tool actions were folded into one footer hold bare action lines —
    # whole continuation parts of "Ran: …". Publish one durable `exchange_format` Command per such
    # episode; the detached capture worker re-folds and re-embeds it (service.reformat_exchanges),
    # keeping its episode key, title and age. Self-limiting: only bodies with an action line at a
    # line start (or right after the "Assistant: " label) match — a footer's entries never start a
    # line — so replaying this step once the rewrite has run enqueues nothing new.
    # The predicate follows the current action vocabulary (ACTION_PREFIXES) by design: the
    # handler folds with the same vocabulary, so a replay after it changes stays consistent.
    clause = " OR ".join("instr(char(10) || body, ?) > 0 OR instr(body, ?) > 0" for _ in ACTION_PREFIXES)
    params = [pattern for prefix in ACTION_PREFIXES for pattern in ("\n" + prefix, "Assistant: " + prefix)]
    episodes = db.execute(
        f"SELECT DISTINCT project_key, source_path FROM chunks WHERE kind = 'exchange' AND ({clause})", params
    ).fetchall()
    stamp = time.time()
    db.executemany(
        _ENQUEUE_WORK,
        [
            (f"{EXCHANGE_FORMAT}:{key}:{episode}", EXCHANGE_FORMAT, key, "", episode, "", stamp)
            for key, episode in episodes
        ],
    )


def _v21_chunk_lookup_indexes(db: sqlite3.Connection) -> None:
    # Covering indexes for the chunk reads that filter past the project: the kind-scoped index
    # searches (search_code / search_docs prefilter their FTS match and load their rows by
    # (project_key, kind)) and get_chunk's anchor fallback (get_symbol by name). Measured at 40k
    # chunks: search_code / search_docs ~160 → ~125 ms, an anchor lookup 12 → 0.03 ms; they build
    # in ~0.2 s. idx_chunks_project (_v7) stays although both lead with project_key: the hook's
    # unscoped index prefilter reads it, and without it the planner picks the wider anchor index
    # (+4 ms a prompt at 40k chunks). Idempotent.
    db.executescript(
        "CREATE INDEX IF NOT EXISTS idx_chunks_kind ON chunks(project_key, kind);"
        "CREATE INDEX IF NOT EXISTS idx_chunks_anchor ON chunks(project_key, anchor);"
    )


# Ordered schema migrations. user_version marks how many have run — it is stamped only after the
# whole ladder has, so a store stamped N has run steps 1…N and an upgrade runs only the rest. Every
# step is also individually idempotent (ADD COLUMN only if missing, CREATE … IF NOT EXISTS, rebuild
# only on first creation), so a store below _LADDER_FLOOR — fresh, or the legacy FTS flag of 1 that
# predates the ladder — converges by replaying every step. A step that must re-run on stores already
# stamped past it gets a new slot of its own (see _v17_sensory_schema); a released slot never changes.
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
    _v17_sensory_schema,
    _v18_facts_browse_index,
    _v19_fact_episode,
    _v20_exchange_format,
    _v21_chunk_lookup_indexes,
]
_SCHEMA_VERSION = len(_MIGRATIONS)
_LADDER_FLOOR = 2  # below this, user_version doesn't say which steps ran (0 = fresh, 1 = legacy FTS flag)
# Every table / index / trigger the base schema declares — a store missing one isn't current.
_SCHEMA_OBJECTS = frozenset(
    re.findall(r"CREATE\s+(?:VIRTUAL\s+)?(?:TABLE|INDEX|TRIGGER)\s+IF\s+NOT\s+EXISTS\s+(\w+)", _SCHEMA, re.I)
)
# How long a write waits out another writer's lock. Detached work (capture, consolidation,
# indexing) can afford to wait; a hook on the interactive path cannot — its writes are telemetry
# (the ledger, recall attribution), so it fails them fast rather than spend the 5 s hook ceiling.
DEFAULT_BUSY_MS = 5000
INTERACTIVE_BUSY_MS = 250


class Store:
    def __init__(self, path: Path | str, *, busy_timeout_ms: int = DEFAULT_BUSY_MS) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=busy_timeout_ms / 1000)
        self.db.row_factory = sqlite3.Row
        # WAL lets concurrent hook processes (capture, per-edit reindex, recall, the
        # viewer) read while one writes; busy_timeout waits out a brief write lock
        # instead of raising; NORMAL sync is durable enough under WAL and much faster.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        self.db.execute("PRAGMA synchronous=NORMAL")
        # The DDL (`CREATE … IF NOT EXISTS`) takes the write lock even when everything exists, so
        # every open used to queue behind any writer — the prompt hook timed out on it. A current
        # store (stamped, every object present — both read-only checks) skips it; anything else
        # still converges through the schema + migration ladder.
        if not self._schema_current():
            self.db.executescript(_SCHEMA)
            self._migrate()

    def _schema_current(self) -> bool:
        if self.db.execute("PRAGMA user_version").fetchone()[0] != _SCHEMA_VERSION:
            return False
        present = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master")}
        return _SCHEMA_OBJECTS <= present

    def _migrate(self) -> None:
        """Run the schema-migration ladder up to _SCHEMA_VERSION, then stamp it.

        user_version is the fast path: once stamped, opens are a single PRAGMA read. A store stamped
        below the head runs only the steps after its stamp, so a schema bump costs its own step —
        replaying the whole ladder took ~8 s at 10⁵ facts (_v6 rebuilds facts_fts on every run), on
        whichever process opened the store first after an upgrade, often the prompt hook. A store
        below _LADDER_FLOOR, or stamped past the head by newer code, replays every step instead —
        each is idempotent — so fresh, legacy and downgraded databases converge to the same schema.
        """
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version == _SCHEMA_VERSION:
            return
        steps = _MIGRATIONS[version:] if _LADDER_FLOOR <= version < _SCHEMA_VERSION else _MIGRATIONS
        for step in steps:
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

    def reinforce(self, fact_id: str, now: float | None = None, *, episode: str | None = None) -> int:
        """Consolidation — strengthen a fact seen again and refresh its recency.

        A given ``episode`` re-points the fact's provenance at the conversation that just restated it
        (the newest trace is the one retention keeps longest); None leaves the link as it was.
        Returns the fact's new frequency so the caller can decide promotion
        (STM→LTM on rehearsal); 0 if the fact is absent.
        """
        with self.db:
            self.db.execute(
                "UPDATE facts SET frequency = frequency + 1, last_seen = ?, status = 'active', "
                "episode = COALESCE(?, episode) WHERE id = ?",
                (_now(now), episode, fact_id),
            )
        row = self.db.execute("SELECT frequency FROM facts WHERE id = ?", (fact_id,)).fetchone()
        return int(row[0]) if row else 0

    def promote(self, fact_id: str, now: float | None = None) -> None:
        """Rehearsal transfer — move a short-term fact into the long-term store."""
        with self.db:
            self.db.execute(
                "UPDATE facts SET tier = 'ltm', last_seen = ? WHERE id = ? AND tier = 'stm'",
                (_now(now), fact_id),
            )

    def mature_aged_stm(self, project_key: str, cutoff: float) -> int:
        """Age-based STM→LTM maturation — transfer active short-term facts captured before
        ``cutoff`` into the long-term store, regardless of rehearsal or recall.

        The time-based sibling of ``promote`` (rehearsal) and ``replay`` (recalled): it keeps
        STM a genuinely short-term buffer instead of letting one-off facts accumulate forever.
        Unlike ``promote`` it does **not** refresh ``last_seen`` — the fact was not re-seen, so
        its recency/decay must be preserved for the forgetting curve. Age is measured from
        ``created_at`` (capture time), not ``last_seen``. Idempotent (a matured row is no longer
        ``tier='stm'``) and reversible in spirit (a tier flip, never a delete). Returns the count.
        """
        with self.db:
            cur = self.db.execute(
                "UPDATE facts SET tier = 'ltm' WHERE project_key = ? AND status = 'active' "
                "AND tier = 'stm' AND created_at < ?",
                (project_key, cutoff),
            )
        return cur.rowcount

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
        with self.db:
            cur = self.db.execute(
                f"UPDATE facts SET status = 'displaced' WHERE id IN ({placeholders}) AND status = 'active'",
                ids,
            )
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
        with self.db:
            cur = self.db.execute(
                f"UPDATE facts SET recall_count = recall_count + 1, last_recalled = ? WHERE id IN ({placeholders})",
                (stamp, *fact_ids),
            )
        return cur.rowcount

    def mark_injected(self, fact_ids: list[str]) -> int:
        """Use-feedback: record that facts were injected into the focus (Engle/Kane).

        The denominator of the inhibition signal. Off the interactive hot path in the
        current design (wiring the injection tally into recall is a follow-up); safe to call
        wherever injected ids are known. Returns rows updated.
        """
        if not fact_ids:
            return 0
        with self.db:
            cur = self.db.execute(
                f"UPDATE facts SET injected_count = injected_count + 1 WHERE id IN ({_placeholders(fact_ids)})",
                tuple(fact_ids),
            )
        return cur.rowcount

    def mark_used(self, fact_ids: list[str]) -> int:
        """Use-feedback: record that injected facts were actually engaged with.

        The numerator of the inhibition signal — driven by a "used" detector
        (token-reappearance / edit-content / correction-turn), which is a follow-up.
        Returns rows updated.
        """
        if not fact_ids:
            return 0
        with self.db:
            cur = self.db.execute(
                f"UPDATE facts SET used_count = used_count + 1 WHERE id IN ({_placeholders(fact_ids)})",
                tuple(fact_ids),
            )
        return cur.rowcount

    def add_edges(self, edges: list[tuple[str, str, str, float]]) -> int:
        """Upsert undirected association edges ``(src_id, dst_id, kind, weight)``.

        The caller normalises pair order (src < dst) so an undirected link is one row.
        Weight accumulates on repeat (co-occurring again strengthens the link). Returns the
        number of edge rows submitted.
        """
        if not edges:
            return 0
        with self.db:
            self.db.executemany(
                "INSERT INTO fact_edges (src_id, dst_id, kind, weight) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(src_id, dst_id, kind) DO UPDATE SET weight = weight + excluded.weight",
                edges,
            )
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

    def supersede_counts(self) -> dict[str, int]:
        """How many facts each fact superseded — the retention 'surprise' signal (§3A) — for every
        fact at once. One grouped scan: ``superseded_by`` is unindexed, so a per-fact ``COUNT(*)``
        is a full-table scan each, which made a refine pass over a 144k-fact store take ~13.5 h."""
        rows = self.db.execute(
            "SELECT superseded_by, COUNT(*) FROM facts WHERE superseded_by IS NOT NULL GROUP BY superseded_by"
        ).fetchall()
        return {fact_id: count for fact_id, count in rows}

    def set_status(self, fact_ids: list[str], status: str) -> int:
        """Archive the still-active facts of a set under ``status`` (reversible; recall scans 'active'
        only). A fact already archived — superseded by a capture mid-consolidation, say — keeps
        its status: capture and consolidation write concurrently, and the guard (the ``supersede``
        idiom) stops one overwriting the other's verdict. Returns how many changed."""
        if not fact_ids:
            return 0
        with self.db:
            cur = self.db.execute(
                f"UPDATE facts SET status = ? WHERE id IN ({_placeholders(fact_ids)}) AND status = 'active'",
                (status, *fact_ids),
            )
        return cur.rowcount

    def purge(self, horizon_seconds: float, now: float | None = None) -> int:
        """Two-stage lifecycle backstop — hard-delete long-archived facts, then VACUUM.

        The ONLY true delete: rows already archived (superseded/displaced/merged/pruned/
        expired) and untouched for longer than ``horizon_seconds``. Opt-in (disabled at 0).
        The FTS index stays in sync via the delete trigger.
        """
        cutoff = _now(now) - horizon_seconds
        with self.db:
            cur = self.db.execute(
                "DELETE FROM facts WHERE status IN ('superseded', 'displaced', 'merged', 'pruned', 'expired') "
                "AND COALESCE(last_seen, created_at) < ?",
                (cutoff,),
            )
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
        with self.db:
            cur = self.db.execute(
                f"UPDATE facts SET status = 'superseded', superseded_by = ? "
                f"WHERE id IN ({placeholders}) AND status = 'active'",
                (by_id, *fact_ids),
            )
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
        episode: str | None = None,
    ) -> bool:
        fid = self.fact_id(project["key"], text)
        stamp = created_at if created_at is not None else time.time()
        with self.db:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO facts "
                "(id, project_key, project_label, project_path, session_id, kind, text, "
                " title, subtitle, narrative, files, type, observation_id, created_at, last_seen, dim, scale, "
                " vec_int8, vec_bits, importance, frequency, status, tier, episode) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'active', ?, ?)",
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
                    episode,
                ),
            )
        return cur.rowcount > 0

    @contextmanager
    def deadline(self, seconds: float) -> Iterator[None]:
        """Interrupt any statement still running ``seconds`` from now — it raises
        ``sqlite3.OperationalError`` ("interrupted") and its write rolls back; the connection stays
        usable. SQLite polls the clock every few thousand VM steps, so a stage that keeps issuing
        statements is stopped at its next one past the deadline, and a single long one mid-run."""
        end = time.monotonic() + seconds
        self.db.set_progress_handler(lambda: time.monotonic() > end, 10_000)
        try:
            yield
        finally:
            self.db.set_progress_handler(None, 0)

    def end_stray_transaction(self) -> bool:
        """Roll back a transaction something left open; True when there was one.

        Every write method commits or rolls back on its own (``with self.db``). This is the
        backstop for the long-lived processes (the MCP server, the daemon): called after each
        request, it means a connection can never sit on the write lock — or pin a WAL snapshot
        that blocks checkpoints — between requests, as one MCP server did for 13 h."""
        if not self.db.in_transaction:
            return False
        self.db.rollback()
        return True

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

    def scan_rows(self, project_key: str, *, kind: str | None = None, text: bool = False) -> list[sqlite3.Row]:
        """A project's active facts with only the columns recall's scan and ranking read (plus
        ``text`` for the lexical channel), in rowid order — the order the full-row query returns.
        Recall re-reads the few rows it returns in full with ``get``; ``SELECT *`` over 10⁵ rows
        (~2 KB each, the narrative and bit vector included) was a third of a recall."""
        columns = _SCAN_COLUMNS + (", text" if text else "")
        kind_clause, params = (" AND kind = ?", (project_key, kind)) if kind else ("", (project_key,))
        return self.db.execute(
            f"SELECT {columns} FROM facts WHERE project_key = ? AND status = 'active'{kind_clause} ORDER BY rowid",
            params,
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
        group_ids = [row["grp"] for row in self.db.execute(sql, params).fetchall()]
        if not group_ids:
            return []
        # Fetch every selected group's facts in ONE query, then bucket in Python — not a
        # per-group query (N+1). The N+1 was the real cost at scale: each per-group lookup
        # filters on COALESCE(observation_id, id), which no index can serve, so it re-scanned
        # the whole tier — 50 scans of 135k rows ≈ 11s. One IN query is a single scan (~0.2s).
        placeholders = ",".join("?" * len(group_ids))
        rows = self.db.execute(
            f"SELECT * FROM facts WHERE {cond} AND {grp} IN ({placeholders}) ORDER BY rowid ASC",
            (*cparams, *group_ids),
        ).fetchall()
        by_group: dict[object, list[sqlite3.Row]] = {}
        for row in rows:
            key = row["observation_id"] if row["observation_id"] is not None else row["id"]
            by_group.setdefault(key, []).append(row)
        # Preserve the group query's newest-first order; every group_id has ≥1 row.
        return [by_group[g] for g in group_ids if g in by_group]

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
        with self.db:
            cur = self.db.execute(sql, params)
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
        with self.db:
            self.db.execute(
                "INSERT INTO sensory (id, project_key, modality, observation_id, url, text, attended, created_at, decayed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 0, ?, NULL) "
                "ON CONFLICT(id) DO UPDATE SET created_at = excluded.created_at, text = excluded.text, decayed_at = NULL",
                (sid, project_key, modality, observation_id, url, text, _now(now)),
            )
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
        with self.db:
            self.db.execute("UPDATE sensory SET attended = 1 WHERE id = ?", (sensory_id,))

    def sweep_sensory(self, project_key: str, capacity: int, ttl_seconds: float, now: float | None = None) -> int:
        """Decay the register (A-S 'lost from SR'). Soft-tombstones (sets ``decayed_at``)
        UNATTENDED perceptions older than ``ttl_seconds`` and unattended live perceptions beyond
        ``capacity`` (newest kept), then hard-purges tombstones older than ``ttl_seconds`` so the
        table stays bounded. Attended perceptions are left for the promotion pass. Returns the
        number newly tombstoned. A 0/None limit disables that limb."""
        t = _now(now)
        decayed = 0
        with self.db:
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

    def delete_sensory(self, sensory_id: str) -> int:
        """Hard-delete one perception by id (the viewer's Sensory-card trash). Returns rows removed."""
        if not sensory_id:
            return 0
        with self.db:
            cur = self.db.execute("DELETE FROM sensory WHERE id = ?", (sensory_id,))
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
        with self.db:
            self.db.execute(
                "UPDATE sensory SET decayed_at = ? WHERE id = ? AND decayed_at IS NULL",
                (_now(now), sensory_id),
            )

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

    def archived_count(self, project_key: str) -> int:
        """Number of archived ('forgotten') observation *groups* for the viewer's
        Consolidation header — counts distinct observation groups (as
        ``list_observations(active=False)`` yields cards), not raw facts, so the header
        total matches the paginated card list."""
        return self.db.execute(
            "SELECT COUNT(DISTINCT COALESCE(observation_id, id)) FROM facts "
            "WHERE project_key = ? AND status != 'active'",
            (project_key,),
        ).fetchone()[0]

    def clear_session_kind(self, project_key: str, session_id: str, kind: str) -> int:
        """Delete a session's facts of a given kind (used to replace its session summary)."""
        with self.db:
            cur = self.db.execute(
                "DELETE FROM facts WHERE project_key = ? AND session_id = ? AND kind = ?",
                (project_key, session_id, kind),
            )
        return cur.rowcount

    def _scoped_fts_ids(
        self,
        fts_table: str,
        content_table: str,
        rank: str,
        scope: tuple[str, list],
        query: str,
        limit: int,
    ) -> list[str]:
        """Ids of ``content_table`` rows in ``scope`` (a WHERE clause and its params) matching an
        FTS5 keyword query, best-ranked first — the one query shape both keyword channels use.

        The FTS tables span the whole store, so the scope is applied *before* ranking: the MATCH is
        filtered against the scope's rowids (a list built once per query from the scope's index),
        bm25 is computed only for in-scope matches, and a content row — wide: vectors, bodies — is
        read only for the ``limit`` survivors. Joining every store-wide match to its row just to
        drop it cost 0.2–0.4 s per query whatever the project's size.

        The unary ``+`` on ``rowid`` is load-bearing: it keeps the ``IN`` term away from FTS5's query
        planner, which would otherwise seek every doclist once per in-scope rowid (~10× slower).
        Ranking is unchanged — bm25's statistics are the same table's, and ties keep rowid order.
        """
        match = _fts_match_expr(query)
        if not match:
            return []
        where, params = scope
        sql = (
            f"WITH hit AS (SELECT rowid AS r, {rank} AS s FROM {fts_table} WHERE {fts_table} MATCH ? "
            f"AND +rowid IN (SELECT rowid FROM {content_table} WHERE {where}) ORDER BY s, r LIMIT ?) "
            f"SELECT t.id FROM hit JOIN {content_table} t ON t.rowid = hit.r ORDER BY hit.s, hit.r"
        )
        return [row[0] for row in self.db.execute(sql, [match, *params, limit])]

    def fts_search(self, project_key: str, query: str, limit: int = 50) -> list[str]:
        """Active fact ids for a project matching an FTS5 keyword query, best-ranked first."""
        scope = ("project_key = ? AND status = 'active'", [project_key])
        return self._scoped_fts_ids("facts_fts", "facts", "bm25(facts_fts)", scope, query, limit)

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
        with self.db:
            cur = self.db.execute(sql, params)
        return cur.rowcount

    def prune_project(self, project_key: str) -> int:
        with self.db:
            cur = self.db.execute("DELETE FROM facts WHERE project_key = ?", (project_key,))
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
            with self.db:
                self.db.execute(
                    "INSERT INTO recall_events (ts, project_key, query, returned, top_sim, confidence, verdict) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (_now(now), project_key, query, returned, top_sim, confidence, verdict),
                )
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

    def recent_recall_queries(
        self,
        limit: int,
        verdicts: tuple[str, ...] = ("ok", "low_confidence"),
        *,
        project_key: str | None = None,
    ) -> list[tuple[str, str]]:
        """Distinct ``(project_key, query)`` pairs from the ledger, most recently asked first.

        Restricted to recalls that returned facts (``verdicts``), so a replay re-asks questions
        memory could answer rather than empty-store or misconfigured ones; ``project_key``
        narrows it to one project.
        """
        marks = ",".join("?" * len(verdicts))
        scope, params = ("AND project_key = ? ", (project_key,)) if project_key else ("", ())
        rows = self.db.execute(
            f"SELECT project_key, query FROM recall_events WHERE verdict IN ({marks}) {scope}"
            "GROUP BY project_key, query ORDER BY MAX(ts) DESC LIMIT ?",
            (*verdicts, *params, limit),
        ).fetchall()
        return [(row["project_key"], row["query"]) for row in rows]

    def record_usage(
        self, project_key: str, kind: str, *, bytes_in: int = 0, bytes_saved: int = 0, now: float | None = None
    ) -> None:
        """Append one usage-ledger row (cost=bytes_in / saving=bytes_saved). Best-effort —
        a telemetry failure must never break recall, capture, or a pull."""
        try:
            with self.db:
                self.db.execute(
                    "INSERT INTO usage_events (ts, project_key, kind, bytes_in, bytes_saved) VALUES (?, ?, ?, ?, ?)",
                    (_now(now), project_key, kind, bytes_in, bytes_saved),
                )
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

    def newest_capture_progress(self) -> float | None:
        """When any session's capture cursor last advanced (the indexer's ``idxsig:`` keys aside)."""
        row = self.db.execute(
            "SELECT MAX(updated_at) FROM capture_cursors WHERE cursor_key NOT LIKE 'idxsig:%'"
        ).fetchone()
        return row[0]

    def get_capture_cursor(self, cursor_key: str) -> int:
        """Byte offset already distilled for this session, so incremental capture reads only new turns."""
        row = self.db.execute("SELECT offset FROM capture_cursors WHERE cursor_key = ?", (cursor_key,)).fetchone()
        return row["offset"] if row else 0

    def set_capture_cursor(self, cursor_key: str, offset: int, now: float | None = None) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO capture_cursors (cursor_key, offset, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(cursor_key) DO UPDATE SET offset = excluded.offset, updated_at = excluded.updated_at",
                (cursor_key, offset, _now(now)),
            )

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
        non-file writer (replace_nonfile_chunks) — the column list lives in exactly one place."""
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

    def replace_nonfile_chunks(
        self, project_key: str, kind: str, source: str, chunks: list[dict], now: float | None = None
    ) -> int:
        """Swap a non-file source's ``kind`` chunks — a snapshot's URL (the index's visual column) or
        a conversation session (episodic exchanges). Unlike replace_source_chunks this writes NO
        chunk_sources row: the source is not a file on disk, so it is exempt from file
        drift-reconciliation (index_project reconciles the filesystem against chunk_sources) and its
        freshness is decided at read time by kind. Delete-then-insert scoped to (source, kind).
        Returns the number of chunks written."""
        stamp = _now(now)
        with self.db:
            self.db.execute(
                "DELETE FROM chunks WHERE project_key = ? AND source_path = ? AND kind = ?",
                (project_key, source, kind),
            )
            self._insert_chunk_rows(project_key, source, chunks, stamp)
        return len(chunks)

    def prune_nonfile_chunks(
        self, project_key: str, kind: str, *, max_age_seconds: float, keep_max: int, now: float | None = None
    ) -> int:
        """Forget a non-file kind's oldest chunks — those indexed more than ``max_age_seconds`` ago
        (0 = no age limit), then any beyond the newest ``keep_max`` (0 = no cap). The FTS index
        follows via the delete trigger. Returns the number of chunks deleted."""
        stamp = _now(now)
        deleted = 0
        with self.db:
            if max_age_seconds > 0:
                deleted += self.db.execute(
                    "DELETE FROM chunks WHERE project_key = ? AND kind = ? AND indexed_at < ?",
                    (project_key, kind, stamp - max_age_seconds),
                ).rowcount
            if keep_max > 0:
                deleted += self.db.execute(
                    "DELETE FROM chunks WHERE id IN (SELECT id FROM chunks WHERE project_key = ? AND kind = ? "
                    "ORDER BY indexed_at DESC LIMIT -1 OFFSET ?)",
                    (project_key, kind, keep_max),
                ).rowcount
        return deleted

    def chunk_stats(self, project_key: str, kind: str) -> dict:
        """``{"count", "bytes"}`` of one chunk kind's stored bodies for a project (doctor)."""
        count, size = self.db.execute(
            "SELECT COUNT(*), COALESCE(SUM(LENGTH(body)), 0) FROM chunks WHERE project_key = ? AND kind = ?",
            (project_key, kind),
        ).fetchone()
        return {"count": count, "bytes": size}

    def delete_source(self, project_key: str, source_path: str) -> None:
        """Drop a vanished file's chunks and source row (called for files gone since last index)."""
        with self.db:
            self.db.execute("DELETE FROM chunks WHERE project_key = ? AND source_path = ?", (project_key, source_path))
            self.db.execute(
                "DELETE FROM chunk_sources WHERE project_key = ? AND source_path = ?",
                (project_key, source_path),
            )

    def get_chunk(self, project_key: str, ref: str) -> sqlite3.Row | None:
        """Fetch one chunk by its id or, failing that, its human-readable anchor slug.

        The id goes through the primary key first: a single ``id = ? OR anchor = ?`` filter can't
        use it and walked the project's chunks on every call — the hook's index block fetches each
        candidate this way (~280 ms a prompt at 40k chunks). Among chunks sharing an anchor (one
        name in several files), the first indexed — the lowest rowid — wins."""
        row = self.db.execute("SELECT * FROM chunks WHERE id = ? AND project_key = ?", (ref, project_key)).fetchone()
        if row is not None:
            return row
        return self.db.execute(
            "SELECT * FROM chunks WHERE project_key = ? AND anchor = ? ORDER BY rowid LIMIT 1", (project_key, ref)
        ).fetchone()

    def chunk_outline(
        self, project_key: str, source_path: str | None = None, kind: str | None = None
    ) -> list[sqlite3.Row]:
        """Ordered skeleton (no body): anchor/title/heading_path/level/summary per chunk."""
        where, params = _chunk_scope(project_key, kind, source_path)
        return self.db.execute(
            "SELECT id, source_path, kind, anchor, title, heading_path, level, summary "
            f"FROM chunks WHERE {where} ORDER BY source_path, byte_start",
            params,
        ).fetchall()

    def chunk_rows(
        self, project_key: str, kind: str | None = None, source_path: str | None = None
    ) -> list[sqlite3.Row]:
        """All chunk rows for a project (vector-channel scan input), optionally one kind / source."""
        where, params = _chunk_scope(project_key, kind, source_path)
        return self.db.execute(f"SELECT * FROM chunks WHERE {where}", params).fetchall()

    def chunk_fts_search(
        self,
        project_key: str,
        query: str,
        limit: int = 50,
        kind: str | None = None,
        source_path: str | None = None,
    ) -> list[str]:
        """Chunk ids matching an FTS5 keyword query, best-ranked first (weighted columns)."""
        scope = _chunk_scope(project_key, kind, source_path)
        return self._scoped_fts_ids("chunks_fts", "chunks", "bm25(chunks_fts, 3.0, 2.0, 1.5, 1.0)", scope, query, limit)

    def unlink_forgotten_episodes(self, project_key: str) -> int:
        """Clear the ``episode`` link on facts whose exchanges have all been forgotten, so on-demand
        recall never points at a conversation that is gone. Returns the number of facts unlinked."""
        with self.db:
            return self.db.execute(
                "UPDATE facts SET episode = NULL WHERE project_key = ? AND episode IS NOT NULL "
                "AND episode NOT IN (SELECT DISTINCT source_path FROM chunks WHERE project_key = ? AND kind = 'exchange')",
                (project_key, project_key),
            ).rowcount

    def set_index_meta(self, project: Project) -> None:
        """Record a project's human label/path for the index, so an index-only project
        (chunks but no memory facts) still shows a real name instead of its raw key."""
        with self.db:
            self.db.execute(
                "INSERT INTO index_meta (project_key, label, path, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(project_key) DO UPDATE SET "
                "label = excluded.label, path = excluded.path, updated_at = excluded.updated_at",
                (project["key"], project.get("label") or project["key"], project.get("path") or "", _now(None)),
            )

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

    # ---- Durable work queue (WorkQueue inproc adapter) ----------------------------

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
        with self.db:
            cur = self.db.execute(_ENQUEUE_WORK, (msg_id, stage, project_key, session_id, ref, payload, _now(now)))
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
        with self.db:
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
        return rows

    def ack_work(self, msg_id: str) -> None:
        """Work done — remove it from the queue."""
        with self.db:
            self.db.execute("DELETE FROM work_queue WHERE msg_id = ?", (msg_id,))

    def nak_work(self, msg_id: str, delay: float = 0.0, now: float | None = None) -> None:
        """Return work for retry after ``delay`` seconds; clears the lease."""
        now = _now(now)
        with self.db:
            self.db.execute(
                "UPDATE work_queue SET status = 'pending', next_retry_at = ?, lease_owner = NULL, lease_expires = 0 "
                "WHERE msg_id = ?",
                (now + delay, msg_id),
            )

    def dead_work(self, msg_id: str) -> None:
        """Dead-letter — retries exhausted or terminally unprocessable. Kept for inspection."""
        with self.db:
            self.db.execute(
                "UPDATE work_queue SET status = 'dead', lease_owner = NULL, lease_expires = 0 WHERE msg_id = ?",
                (msg_id,),
            )

    def reclaim_expired(self, now: float | None = None) -> int:
        """Return interrupted (expired-lease) in_progress items to pending. Crash recovery."""
        now = _now(now)
        with self.db:
            cur = self.db.execute(
                "UPDATE work_queue SET status = 'pending', lease_owner = NULL, lease_expires = 0 "
                "WHERE status = 'in_progress' AND lease_expires < ?",
                (now,),
            )
        return cur.rowcount

    def dead_stale(self, horizon_seconds: float, now: float | None = None) -> int:
        """Dead-letter pending items older than the horizon — a backstop so an item no worker
        ever pulls (e.g. a rescue item with no LLM distiller to drain it) can't live forever.
        Kept for inspection (``status='dead'``), not deleted. Disabled at ``horizon<=0``."""
        if horizon_seconds <= 0:
            return 0
        cutoff = _now(now) - horizon_seconds
        with self.db:
            cur = self.db.execute(
                "UPDATE work_queue SET status = 'dead', lease_owner = NULL, lease_expires = 0 "
                "WHERE status = 'pending' AND enqueued_at < ?",
                (cutoff,),
            )
        return cur.rowcount

    def purge_dead(self, horizon_seconds: float, now: float | None = None) -> int:
        """Delete dead-letters whose ``enqueued_at`` is older than the horizon — the automatic
        cleanup so items that can't be rescued don't linger in the queue forever. Callers pass
        ``queue_dead_after + queue_dead_purge_after`` so the window is measured from roughly when
        an item went dead. Complements ``dead_stale`` (which only marks); disabled at ``horizon<=0``."""
        if horizon_seconds <= 0:
            return 0
        cutoff = _now(now) - horizon_seconds
        with self.db:
            cur = self.db.execute(
                "DELETE FROM work_queue WHERE status = 'dead' AND enqueued_at < ?",
                (cutoff,),
            )
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
        with self.db:
            cur = self.db.execute(f"DELETE FROM facts WHERE id IN ({placeholders})", tuple(fact_ids))
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
