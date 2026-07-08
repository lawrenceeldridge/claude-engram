"""Configuration — resolved from plugin userConfig, env overrides, then defaults.

Claude Code exposes a plugin's `userConfig` values to hook/MCP processes as
`CLAUDE_PLUGIN_OPTION_<key>` environment variables (this is how cost-guard reads
its settings). We honour those first, then an `ENGRAM_<KEY>` override for standalone
use, then a safe default. Writable state lives under CLAUDE_PLUGIN_DATA so it
survives plugin updates; never write inside the plugin root.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_MARKERS = ".git,pyproject.toml,package.json,go.mod,Cargo.toml,pom.xml"


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
    # memory.db rather than silently opening an empty default.
    if not (home_default / "memory.db").exists():
        siblings = [p for p in base.glob("engram-*") if (p / "memory.db").is_file()]
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
    stm_capacity: int
    promote_after_freq: int
    stm_recall_weight: float
    spread_weight: float
    bus: str
    bus_max_deliver: int
    bus_backoff: tuple[float, ...]
    lease_ttl: float
    bus_dead_after: float
    nats_url: str
    nats_stream: str
    nats_provision: str
    nats_version: str
    integrate_threshold: float
    refine_keep_max: int
    refine_prune_percentile: float
    purge_horizon_days: float
    distiller: str
    distiller_cmd: str
    distiller_model: str
    distiller_base_url: str
    distiller_api_key: str
    antipatterns: bool
    ttl_days: float
    ttl_keep_frequency: int
    recall_min_confidence: float
    recall_max_chars: int
    viewer_port: int
    viewer_autostart: bool
    snapshotter: str
    visual_max_chars: int
    markers: tuple[str, ...]
    identity: str
    project_dir: str | None
    data_dir: Path
    db_path: Path
    sock_path: Path
    viewer_pid_path: Path


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
        dim=int(_num(_opt("dim", "256"), 256)),
        top_k=top_k,
        activated_k=max(activated_k, top_k),
        min_sim=_num(_opt("min_sim", "0.12"), 0.12),
        core_size=int(_num(_opt("core_size", "5"), 5)),
        # LT-WM retrieval structure: group the session core into a titled scaffold instead of
        # a flat list. Default off preserves the current flat core block.
        core_scaffold=_opt("core_scaffold", "false").lower() in ("1", "true", "yes", "on"),
        max_chars=int(_num(_opt("max_chars", "800"), 800)),
        # Passive index injection at UserPromptSubmit (0 = off). FTS-prefiltered then
        # cosine-reranked, so it stays hot-path-cheap regardless of index size.
        index_top_k=int(_num(_opt("index_top_k", "2"), 2)),
        index_min_sim=_num(_opt("index_min_sim", "0.18"), 0.18),
        index_max_chars=int(_num(_opt("index_max_chars", "400"), 400)),
        cross_project=_opt("cross_project", "false").lower() in ("1", "true", "yes", "on"),
        half_life_days=_num(_opt("half_life_days", "30"), 30),
        w_sim=_num(_opt("w_sim", "1.0"), 1.0),
        w_recency=_num(_opt("w_recency", "0.3"), 0.3),
        w_freq=_num(_opt("w_freq", "0.2"), 0.2),
        supersede_threshold=_num(_opt("supersede_threshold", "0.85"), 0.85),
        # STM/LTM tier (Atkinson-Shiffrin). stm_capacity ships as a generous, non-destructive
        # backstop against runaway STM growth — displacement is a reversible status flip and
        # idempotent, so it only acts far out in the tail. Gentle promotion, no recall penalty.
        stm_capacity=int(_num(_opt("stm_capacity", "2000"), 2000)),
        promote_after_freq=int(_num(_opt("promote_after_freq", "2"), 2)),
        stm_recall_weight=_num(_opt("stm_recall_weight", "1.0"), 1.0),
        # Associative spreading activation (ACT-R). Single gate for Idea #4: 0 = off (no edges
        # recorded at capture, no spread at recall, hot path + store untouched). >0 both records
        # co-occurrence/shared-entity edges and boosts co-activated candidates at recall.
        spread_weight=_num(_opt("spread_weight", "0"), 0),
        # Durable work queue (MemoryBus). inproc = stdlib SQLite queue (default);
        # nats = opt-in JetStream adapter (Phase 5), fail-open to inproc.
        bus=_opt("bus", "inproc"),
        bus_max_deliver=int(_num(_opt("bus_max_deliver", "5"), 5)),
        bus_backoff=tuple(float(x) for x in _opt("bus_backoff", "5,30,120,600").split(",") if x.strip()),
        lease_ttl=_num(_opt("lease_ttl", "300"), 300),
        # A pending work item no active backend ever pulls (e.g. parked on inproc after a
        # switch to nats) dead-letters past this age, so it can't accumulate silently forever.
        bus_dead_after=_num(_opt("bus_dead_after", str(7 * 86400)), 7 * 86400),
        nats_url=_opt("nats_url", "nats://localhost:4222"),
        nats_stream=_opt("nats_stream", "ENGRAM_WORK"),
        # How to auto-provision a NATS server when bus=nats and none is reachable:
        # binary = download + run the nats-server binary (default, no Docker needed);
        # docker = run the official image; off = never auto-start (bring your own).
        nats_provision=_opt("nats_provision", "binary"),
        nats_version=_opt("nats_version", "2.10.22"),
        # Consolidation (sleep pass), split by blast radius. ON by default as non-destructive,
        # reversible backstops: integrate is a high-threshold near-identical mop-up sitting
        # above supersede (0.85), and refine_keep_max is a generous idempotent ceiling that
        # only fires on runaway growth. OFF by default because they forget/destroy and a good
        # value is store-dependent: refine_prune_percentile (compounds every pass) and purge
        # (irreversible hard-delete). All retrieval-affecting knobs stay `engram eval`-gated.
        integrate_threshold=_num(_opt("integrate_threshold", "0.92"), 0.92),
        refine_keep_max=int(_num(_opt("refine_keep_max", "20000"), 20000)),
        refine_prune_percentile=_num(_opt("refine_prune_percentile", "0"), 0),
        purge_horizon_days=_num(_opt("purge_horizon_days", "0"), 0),
        distiller=_opt("distiller", "claude"),
        distiller_cmd=_opt("distiller_cmd", "claude"),
        distiller_model=_opt("distiller_model", ""),
        distiller_base_url=_opt("distiller_base_url", "http://localhost:11434/v1"),
        distiller_api_key=_opt("distiller_api_key", ""),
        # Anti-pattern catalogue: mine admitted mistakes into durable 'antipattern' memories.
        # On by default, but a no-op unless an LLM distiller is configured (heuristic returns []).
        antipatterns=_opt("antipatterns", "true").lower() in ("1", "true", "yes", "on"),
        ttl_days=_num(_opt("ttl_days", "0"), 0),
        ttl_keep_frequency=int(_num(_opt("ttl_keep_frequency", "3"), 3)),
        recall_min_confidence=_num(_opt("recall_min_confidence", "0.35"), 0.35),
        recall_max_chars=int(_num(_opt("recall_max_chars", "1200"), 1200)),
        viewer_port=int(_num(_opt("viewer_port", "7801"), 7801)),
        viewer_autostart=_opt("viewer_autostart", "true").lower() in ("1", "true", "yes", "on"),
        # Visual snapshot compaction (model-invoked MCP tool). snapshotter selects the
        # page-source backend: 'stub' (zero-dep default), 'chrome-devtools', or 'playwright'
        # (fail-open to stub). visual_max_chars caps the a11y-text tool response, mirroring
        # the *_max_chars family. Text-only in v1; the pixel path is deferred to v2.
        snapshotter=_opt("snapshotter", "stub"),
        visual_max_chars=int(_num(_opt("visual_max_chars", "2000"), 2000)),
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
    )
