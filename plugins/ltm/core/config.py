"""Configuration — resolved from plugin userConfig, env overrides, then defaults.

Claude Code exposes a plugin's `userConfig` values to hook/MCP processes as
`CLAUDE_PLUGIN_OPTION_<key>` environment variables (this is how cost-guard reads
its settings). We honour those first, then an `LTM_<KEY>` override for standalone
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
        f"LTM_{upper}",
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
    for name in ("LTM_DATA_DIR", "CLAUDE_PLUGIN_DATA"):
        val = os.environ.get(name)
        if val:
            return Path(val)
    # Match Claude Code's documented per-plugin data dir; fall back to tmp so the
    # tool still runs (and tests pass) outside a plugin context.
    base = Path.home() / ".claude" / "plugins" / "data"
    home_default = base / "ltm"
    # Without CLAUDE_PLUGIN_DATA (a standalone CLI/viewer run), the bare default can
    # miss the live store that Claude Code keeps under a marketplace-qualified
    # sibling (data/ltm-<marketplace>). Adopt the newest sibling that holds a real
    # memory.db rather than silently opening an empty default.
    if not (home_default / "memory.db").exists():
        siblings = [p for p in base.glob("ltm-*") if (p / "memory.db").is_file()]
        if siblings:
            return max(siblings, key=lambda p: (p / "memory.db").stat().st_mtime)
    try:
        home_default.mkdir(parents=True, exist_ok=True)
        return home_default
    except OSError:
        return Path(tempfile.gettempdir()) / "claude-ltm"


@dataclass(frozen=True)
class Config:
    embedding: str
    embedding_model: str
    dim: int
    top_k: int
    min_sim: float
    core_size: int
    max_chars: int
    cross_project: bool
    half_life_days: float
    w_sim: float
    w_recency: float
    w_freq: float
    supersede_threshold: float
    stm_capacity: int
    promote_after_freq: int
    stm_recall_weight: float
    distiller: str
    distiller_cmd: str
    distiller_model: str
    distiller_base_url: str
    distiller_api_key: str
    ttl_days: float
    ttl_keep_frequency: int
    recall_min_confidence: float
    recall_max_chars: int
    viewer_port: int
    viewer_autostart: bool
    markers: tuple[str, ...]
    data_dir: Path
    db_path: Path
    sock_path: Path
    viewer_pid_path: Path


def get_config() -> Config:
    data_dir = _data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    markers = tuple(m.strip() for m in _opt("markers", _DEFAULT_MARKERS).split(",") if m.strip())
    return Config(
        embedding=_opt("embedding", "hash"),
        embedding_model=_opt("embedding_model", ""),
        dim=int(_num(_opt("dim", "256"), 256)),
        top_k=int(_num(_opt("top_k", "3"), 3)),
        min_sim=_num(_opt("min_sim", "0.12"), 0.12),
        core_size=int(_num(_opt("core_size", "5"), 5)),
        max_chars=int(_num(_opt("max_chars", "800"), 800)),
        cross_project=_opt("cross_project", "false").lower() in ("1", "true", "yes", "on"),
        half_life_days=_num(_opt("half_life_days", "30"), 30),
        w_sim=_num(_opt("w_sim", "1.0"), 1.0),
        w_recency=_num(_opt("w_recency", "0.3"), 0.3),
        w_freq=_num(_opt("w_freq", "0.2"), 0.2),
        supersede_threshold=_num(_opt("supersede_threshold", "0.85"), 0.85),
        # STM/LTM tier (Atkinson-Shiffrin). Defaults are behaviour-neutral:
        # no displacement (0 = unbounded STM), gentle promotion, no recall penalty.
        stm_capacity=int(_num(_opt("stm_capacity", "0"), 0)),
        promote_after_freq=int(_num(_opt("promote_after_freq", "2"), 2)),
        stm_recall_weight=_num(_opt("stm_recall_weight", "1.0"), 1.0),
        distiller=_opt("distiller", "claude"),
        distiller_cmd=_opt("distiller_cmd", "claude"),
        distiller_model=_opt("distiller_model", ""),
        distiller_base_url=_opt("distiller_base_url", "http://localhost:11434/v1"),
        distiller_api_key=_opt("distiller_api_key", ""),
        ttl_days=_num(_opt("ttl_days", "0"), 0),
        ttl_keep_frequency=int(_num(_opt("ttl_keep_frequency", "3"), 3)),
        recall_min_confidence=_num(_opt("recall_min_confidence", "0.35"), 0.35),
        recall_max_chars=int(_num(_opt("recall_max_chars", "1200"), 1200)),
        viewer_port=int(_num(_opt("viewer_port", "7801"), 7801)),
        viewer_autostart=_opt("viewer_autostart", "true").lower() in ("1", "true", "yes", "on"),
        markers=markers,
        data_dir=data_dir,
        db_path=data_dir / "memory.db",
        sock_path=data_dir / "ltm.sock",
        viewer_pid_path=data_dir / "viewer.pid",
    )
