"""Configuration — resolved from plugin userConfig, env overrides, then defaults.

Claude Code exposes a plugin's `userConfig` values to hook/MCP processes as
`CLAUDE_PLUGIN_OPTION_<key>` environment variables (this is how cost-guard reads
its settings). We honour those first, then an `ENGRAM_<KEY>` override for standalone
use, then a safe default. Writable state lives under CLAUDE_PLUGIN_DATA so it
survives plugin updates; never write inside the plugin root.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_MARKERS = ".git,pyproject.toml,package.json,go.mod,Cargo.toml,pom.xml"


def _has_memories(db: Path) -> bool:
    """True only if ``db`` is a memory.db that actually holds captured facts.

    A standalone CLI/viewer run with no CLAUDE_PLUGIN_DATA creates an *empty*
    ``engram/memory.db`` as a side effect; on the next run its mere existence used to
    shadow the live store. Treating an empty default as "no real db" lets the non-empty
    sibling win instead. Read-only + fail-closed so a locked/corrupt file never throws
    here (it just isn't adopted)."""
    if not db.is_file():
        return False
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            return con.execute("select 1 from facts limit 1").fetchone() is not None
        finally:
            con.close()
    except sqlite3.Error:
        return False


def _opt(key: str, default: str) -> str:
    upper = key.upper()
    for name in (
        f"CLAUDE_PLUGIN_OPTION_{key}",
        f"CLAUDE_PLUGIN_OPTION_{upper}",
        f"ENGRAM_{upper}",
    ):
        val = os.environ.get(name)
        if val not in (None, ""):
            return val
    return default


_TRUTHY = ("1", "true", "yes", "on")


def _flag(key: str, default: bool) -> bool:
    """A boolean option: truthy strings (1/true/yes/on, any case) are True; the default applies when unset."""
    return _opt(key, "true" if default else "false").lower() in _TRUTHY


def _num(val: str, fallback: float) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return fallback


def _data_dir() -> Path:
    for name in ("ENGRAM_DATA_DIR", "CLAUDE_PLUGIN_DATA"):
        val = os.environ.get(name)
        if val:
            return Path(val)
    # Match Claude Code's documented per-plugin data dir; fall back to tmp so the
    # tool still runs (and tests pass) outside a plugin context.
    base = Path.home() / ".claude" / "plugins" / "data"
    home_default = base / "engram"
    # Without CLAUDE_PLUGIN_DATA (a standalone CLI/viewer run), the bare default can
    # miss the live store that Claude Code keeps under a marketplace-qualified
    # sibling (data/engram-<marketplace>). Adopt the newest sibling that holds a real
    # (non-empty) memory.db rather than silently opening an empty default — and note the
    # default is "empty" not just "absent", since a prior standalone run leaves an empty
    # memory.db behind that would otherwise shadow the live store on every later run.
    if not _has_memories(home_default / "memory.db"):
        siblings = [p for p in base.glob("engram-*") if _has_memories(p / "memory.db")]
        if siblings:
            return max(siblings, key=lambda p: (p / "memory.db").stat().st_mtime)
    try:
        home_default.mkdir(parents=True, exist_ok=True)
        return home_default
    except OSError:
        return Path(tempfile.gettempdir()) / "claude-engram"


@dataclass(frozen=True)
class Config:
    embedding: str
    embedding_model: str
    embedding_truncate_dim: int
    scorer: str  # recall similarity scan backend: auto (numpy if present) | python | numpy
    dim: int
    top_k: int
    activated_k: int
    min_sim: float
    core_size: int
    core_scaffold: bool
    max_chars: int
    index_top_k: int
    index_min_sim: float
    index_max_chars: int
    cross_project: bool
    half_life_days: float
    w_sim: float
    w_recency: float
    w_freq: float
    supersede_threshold: float
    supersede_candidates: int
    supersede_candidate_min_sim: float
    stm_capacity: int
    stm_max_age_days: float
    promote_after_freq: int
    stm_recall_weight: float
    spread_weight: float
    queue_max_deliver: int
    queue_backoff: tuple[float, ...]
    lease_ttl: float
    queue_dead_after: float
    queue_dead_purge_after: float
    integrate_threshold: float
    refine_keep_max: int
    refine_prune_percentile: float
    refine_min_retention: float
    purge_horizon_days: float
    distiller: str
    distiller_cmd: str
    distiller_model: str
    distiller_base_url: str
    distiller_api_key: str
    ingest_min_prompt_len: int
    antipatterns: bool
    review_enabled: bool
    ttl_days: float
    ttl_keep_frequency: int
    recall_min_confidence: float
    recall_max_chars: int
    viewer_port: int
    viewer_autostart: bool
    markers: tuple[str, ...]
    identity: str
    project_dir: str | None
    data_dir: Path
    db_path: Path
    sock_path: Path
    viewer_pid_path: Path
    attention_window_seconds: float
    sensory_enabled: bool
    sensory_capacity: int
    sensory_ttl_seconds: float
    episodic_enabled: bool
    episodic_min_chars: int
    episodic_ttl_days: float
    episodic_max_chunks: int


def get_config() -> Config:
    data_dir = _data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    markers = tuple(m.strip() for m in _opt("markers", _DEFAULT_MARKERS).split(",") if m.strip())
    # Cowan's focus of attention (~4) vs activated LTM: top_k is the small injected focus;
    # activated_k is the broader breadth the on-demand `recall` MCP tool searches. Defaults
    # to top_k, so the two-level split is inert until activated_k is raised deliberately.
    top_k = int(_num(_opt("top_k", "3"), 3))
    activated_k = int(_num(_opt("activated_k", str(top_k)), top_k))
    return Config(
        embedding=_opt("embedding", "hash"),
        embedding_model=_opt("embedding_model", ""),
        embedding_truncate_dim=int(_num(_opt("embedding_truncate_dim", "0"), 0)),
        scorer=_opt("scorer", "auto"),
        dim=int(_num(_opt("dim", "256"), 256)),
        top_k=top_k,
        activated_k=max(activated_k, top_k),
        min_sim=_num(_opt("min_sim", "0.12"), 0.12),
        core_size=int(_num(_opt("core_size", "5"), 5)),
        # LT-WM retrieval structure: group the session core into a titled scaffold instead of
        # a flat list. Default off preserves the current flat core block.
        core_scaffold=_flag("core_scaffold", False),
        max_chars=int(_num(_opt("max_chars", "800"), 800)),
        # Passive index injection at UserPromptSubmit (0 = off). FTS-prefiltered then
        # cosine-reranked, so it stays hot-path-cheap regardless of index size.
        index_top_k=int(_num(_opt("index_top_k", "2"), 2)),
        index_min_sim=_num(_opt("index_min_sim", "0.18"), 0.18),
        index_max_chars=int(_num(_opt("index_max_chars", "400"), 400)),
        cross_project=_flag("cross_project", False),
        half_life_days=_num(_opt("half_life_days", "30"), 30),
        w_sim=_num(_opt("w_sim", "1.0"), 1.0),
        # 0.05, not 0.3: at 0.3 the recency term (up to 0.3) dwarfs typical cosine gaps (~0.03),
        # so the per-prompt hook almost never injected an old relevant fact (engram eval --aged).
        w_recency=_num(_opt("w_recency", "0.05"), 0.05),
        w_freq=_num(_opt("w_freq", "0.2"), 0.2),
        supersede_threshold=_num(_opt("supersede_threshold", "0.85"), 0.85),
        # Supersession candidate window for the LLM distiller. The distiller can only emit a
        # `supersedes` link against a fact it is *shown*; recency alone (the old recent-N window)
        # never surfaces an OLD fact a change contradicts, so a stale fact could never be retired
        # by a vocabulary-disjoint update. `supersede_candidates` is the total budget offered
        # (similarity-ranked first — "a highly correlated memory" — then topped up by recency);
        # `supersede_candidate_min_sim` is a low candidacy gate (the LLM still makes the final
        # conflict call). Capture-path only (detached); no hot-path or token-budget impact.
        supersede_candidates=int(_num(_opt("supersede_candidates", "60"), 60)),
        supersede_candidate_min_sim=_num(_opt("supersede_candidate_min_sim", "0.3"), 0.3),
        # STM/LTM tier (Atkinson-Shiffrin). stm_capacity ships as a generous, non-destructive
        # backstop against runaway STM growth — displacement is a reversible status flip and
        # idempotent, so it only acts far out in the tail. Gentle promotion, no recall penalty.
        stm_capacity=int(_num(_opt("stm_capacity", "2000"), 2000)),
        # Age-based STM→LTM maturation (the time path, alongside rehearsal/replay). A short-term
        # fact older than this transfers to LTM regardless of activity, so STM stays a genuinely
        # short-term buffer instead of accumulating one-off facts forever. Default 1.0 day, ON —
        # recall-neutral at the default stm_recall_weight (STM and LTM score identically), so it
        # only drains STM; 0 disables. See core/consolidation/mature.py.
        stm_max_age_days=_num(_opt("stm_max_age_days", "1"), 1),
        promote_after_freq=int(_num(_opt("promote_after_freq", "2"), 2)),
        stm_recall_weight=_num(_opt("stm_recall_weight", "1.0"), 1.0),
        # Associative spreading activation (ACT-R). Single gate for Idea #4: 0 = off (no edges
        # recorded at capture, no spread at recall, hot path + store untouched). >0 both records
        # co-occurrence/shared-entity edges and boosts co-activated candidates at recall.
        spread_weight=_num(_opt("spread_weight", "0"), 0),
        # Durable work queue (WorkQueue): a stdlib SQLite Command queue, off the hot path.
        queue_max_deliver=int(_num(_opt("queue_max_deliver", "5"), 5)),
        queue_backoff=tuple(float(x) for x in _opt("queue_backoff", "5,30,120,600").split(",") if x.strip()),
        lease_ttl=_num(_opt("lease_ttl", "300"), 300),
        # A pending work item no worker ever pulls (e.g. rescue with no LLM distiller to drain it)
        # dead-letters past this age, so the queue can't accumulate silently forever.
        queue_dead_after=_num(_opt("queue_dead_after", str(7 * 86400)), 7 * 86400),
        # A dead-letter is deleted this long after it went dead (measured from enqueue +
        # queue_dead_after), so items that can't be rescued don't linger forever. Default 3 days; 0 disables.
        queue_dead_purge_after=_num(_opt("queue_dead_purge_after", str(3 * 86400)), 3 * 86400),
        # Consolidation (sleep pass), split by blast radius. ON by default as non-destructive,
        # reversible backstops: integrate is a high-threshold near-identical mop-up sitting
        # above supersede (0.85), and refine_keep_max is a generous idempotent ceiling that
        # only fires on runaway growth. OFF by default because they forget/destroy and a good
        # value is store-dependent: refine_prune_percentile (compounds every pass) and purge
        # (irreversible hard-delete). All retrieval-affecting knobs stay `engram eval`-gated.
        integrate_threshold=_num(_opt("integrate_threshold", "0.92"), 0.92),
        refine_keep_max=int(_num(_opt("refine_keep_max", "20000"), 20000)),
        refine_prune_percentile=_num(_opt("refine_prune_percentile", "0"), 0),
        # The forgetting curve's absolute retention floor: prune LTM facts whose retention score
        # (recency + recall + frequency + salience/importance) has decayed below this, so a fact
        # "fades over time unless recalled, reinforced, or important". Idempotent + reversible
        # (status='pruned'). Retrieval-affecting AND destructive, so default 0 (off): a safe floor
        # is store-size-dependent and `engram eval` cannot measure consolidation (recall-only
        # benchmark). See core/consolidation/refine.py + DESIGN § Memory lifecycle.
        refine_min_retention=_num(_opt("refine_min_retention", "0"), 0),
        purge_horizon_days=_num(_opt("purge_horizon_days", "0"), 0),
        distiller=_opt("distiller", "claude"),
        distiller_cmd=_opt("distiller_cmd", "claude"),
        distiller_model=_opt("distiller_model", ""),
        distiller_base_url=_opt("distiller_base_url", "http://localhost:11434/v1"),
        distiller_api_key=_opt("distiller_api_key", ""),
        # Ingestion quality gate: drop a trivial verbatim user prompt below this many chars
        # (bare confirmations, "Option C", pasted slash-commands) so LTM stays real facts.
        # 0 disables the prompt gate (capture every prompt, as before). See core/domain/ingest.py.
        ingest_min_prompt_len=int(_num(_opt("ingest_min_prompt_len", "12"), 12)),
        # Anti-pattern catalogue: mine admitted mistakes into durable 'antipattern' memories.
        # On by default, but a no-op unless an LLM distiller is configured (heuristic returns []).
        antipatterns=_flag("antipatterns", True),
        # Distiller-assisted memory review (`engram review` / the review_memory MCP tool): the
        # LLM proposes stale/contradicted entries to retire. On by default, but a no-op unless an
        # LLM distiller is configured (the heuristic returns no proposals). Direct invalidation
        # (invalidate_memory / `engram forget`) is always available regardless of this gate.
        review_enabled=_flag("review_enabled", True),
        ttl_days=_num(_opt("ttl_days", "0"), 0),
        ttl_keep_frequency=int(_num(_opt("ttl_keep_frequency", "3"), 3)),
        recall_min_confidence=_num(_opt("recall_min_confidence", "0.40"), 0.40),
        recall_max_chars=int(_num(_opt("recall_max_chars", "1200"), 1200)),
        viewer_port=int(_num(_opt("viewer_port", "7801"), 7801)),
        viewer_autostart=_flag("viewer_autostart", True),
        markers=markers,
        # Project identity: 'workspace' (default) keys memory on the folder Claude was
        # started in (CLAUDE_PROJECT_DIR, else cwd) — matching the human's chosen workspace,
        # so a monorepo subfolder opened as a workspace stays its own project. 'marker' is
        # the legacy behaviour: walk up to the nearest project marker. `.engram-root`
        # overrides both. See DESIGN.md § Project identity.
        identity=_opt("identity", "workspace").strip().lower(),
        # The dir Claude Code was started in (stable across terminal `cd`); the workspace
        # anchor for identity='workspace'. Provided to hooks/MCP by Claude Code.
        project_dir=os.environ.get("CLAUDE_PROJECT_DIR") or None,
        data_dir=data_dir,
        db_path=data_dir / "memory.db",
        sock_path=data_dir / "engram.sock",
        viewer_pid_path=data_dir / "viewer.pid",
        attention_window_seconds=_num(_opt("attention_window_seconds", "300"), 300),
        sensory_enabled=_flag("sensory_enabled", True),
        sensory_capacity=int(_num(_opt("sensory_capacity", "64"), 64)),
        sensory_ttl_seconds=_num(_opt("sensory_ttl_seconds", "900"), 900),
        # Episodic memory: verbatim conversation exchanges indexed (redacted) for on-demand recall —
        # the semantic facts stay the injected layer. Measured on LongMemEval (plan
        # verbatim-episodic-layer): exchanges lift session R_any@5 0.891 → 0.983 over facts alone.
        episodic_enabled=_flag("episodic_enabled", True),
        episodic_min_chars=int(_num(_opt("episodic_min_chars", "24"), 24)),
        episodic_ttl_days=_num(_opt("episodic_ttl_days", "180"), 180),
        episodic_max_chunks=int(_num(_opt("episodic_max_chunks", "20000"), 20000)),
    )
