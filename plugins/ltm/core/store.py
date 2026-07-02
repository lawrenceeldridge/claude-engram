"""SQLite repository for memory facts (Data Mapper — rows never persist themselves).

One global database under CLAUDE_PLUGIN_DATA holds every project's memory, each
row tagged with its project key. Facts are content-addressed per project
(``id = hash(project_key + normalised_text)``). Re-encountering the same fact
reinforces it (frequency++, last_seen refreshed) rather than duplicating it;
a semantically near-identical newer fact can supersede older ones.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path

from core.project import Project

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
  id            TEXT PRIMARY KEY,
  project_key   TEXT NOT NULL,
  project_label TEXT,
  project_path  TEXT,
  session_id    TEXT,
  kind          TEXT,
  text          TEXT NOT NULL,
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
"""

_MIGRATIONS = [
    ("last_seen", "last_seen REAL"),
    ("frequency", "frequency INTEGER DEFAULT 1"),
    ("status", "status TEXT DEFAULT 'active'"),
    ("superseded_by", "superseded_by TEXT"),
]


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        existing = {row[1] for row in self.db.execute("PRAGMA table_info(facts)")}
        for name, ddl in _MIGRATIONS:
            if name not in existing:
                self.db.execute(f"ALTER TABLE facts ADD COLUMN {ddl}")
        self.db.execute("UPDATE facts SET last_seen = created_at WHERE last_seen IS NULL")
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
    ) -> bool:
        fid = self.fact_id(project["key"], text)
        stamp = created_at if created_at is not None else time.time()
        cur = self.db.execute(
            "INSERT OR IGNORE INTO facts "
            "(id, project_key, project_label, project_path, session_id, kind, text, "
            " created_at, last_seen, dim, scale, vec_int8, vec_bits, importance, frequency, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'active')",
            (
                fid,
                project["key"],
                project["label"],
                project["path"],
                session_id,
                kind,
                text,
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

    def rows_for_project(self, project_key: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM facts WHERE project_key = ?", (project_key,)
        ).fetchall()

    def active_rows_for_project(self, project_key: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM facts WHERE project_key = ? AND status = 'active'", (project_key,)
        ).fetchall()

    def active_rows(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM facts WHERE status = 'active'").fetchall()

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
