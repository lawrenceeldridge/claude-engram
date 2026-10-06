"""Service health — one list of checks, rendered by ``engram doctor``, the viewer's
``/api/health`` and ``engram import``.

Each :class:`Check` says whether a configured backend is live (``ok``) or whether a stdlib
fallback is in force or recall is degrading (``warn``). Never an error: health is read-only, off
the hot path, and every probe fails open.
"""

from __future__ import annotations

import shutil
import socket
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from core import errlog, singleflight
from core.config import Config
from core.ports.scorer import PurePythonScorer, get_scorer
from core.store import Store

# Pure-Python cosine cost per vector element (rows × dim), measured 2026-10-06 on the
# ``PurePythonScorer`` at 256 / 384 / 768 dims (106–107 ns, linear); it predicts the 11.9 s the
# latency harness measured for 144k × 768 on the hook path to within 3%.
PY_SCAN_NS_PER_ELEMENT = 106
# Warn once the estimated pure-Python scan reaches this: the UserPromptSubmit hook's 5 s ceiling
# must also cover query embedding, row loading and the index block, and a scan past it injects
# nothing. ~24k facts at 768 dims, ~73k at 256.
SCAN_WARN_SECONDS = 2.0

_HOOK_CEILING_SECONDS = 5
_HTTP_DISTILLERS = frozenset({"ollama", "http", "openai"})


@dataclass(frozen=True)
class Check:
    """One subsystem's state: the backend in use, ``ok`` | ``warn``, and why."""

    name: str
    backend: str
    state: str
    detail: str


def tcp_ok(url: str, timeout: float = 0.6) -> bool:
    """Best-effort TCP reachability for an http(s) URL; any parse or socket error is "unreachable"."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port or {"https": 443, "http": 80}.get(parsed.scheme, 0)
        if not host or not port:
            return False
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def queue_check() -> Check:
    """The WorkQueue has one always-available stdlib backend."""
    return Check("queue", "inproc", "ok", "sqlite work_queue")


def embedding_check(cfg: Config) -> Check:
    """fastembed needs its provisioned venv; ``hash`` is the stdlib default."""
    if cfg.embedding != "fastembed":
        return Check("embedding", "hash", "ok", "lexical (stdlib)")
    try:
        from core.provision import is_provisioned

        provisioned = is_provisioned(cfg.data_dir)
    except Exception:
        provisioned = False
    if provisioned:
        return Check("embedding", "fastembed", "ok", cfg.embedding_model or "bge-base")
    return Check("embedding", "fastembed", "warn", "venv not provisioned — falling open to hash")


def distiller_check(cfg: Config) -> Check:
    """The distiller's transport: the ``claude`` CLI must be on PATH; an HTTP backend must answer
    at its base URL; the heuristic is stdlib."""
    if cfg.distiller == "heuristic":
        return Check("distiller", "heuristic", "ok", "line extraction (stdlib)")
    label = cfg.distiller + (f" · {cfg.distiller_model}" if cfg.distiller_model else "")
    if cfg.distiller in _HTTP_DISTILLERS:
        host = urlparse(cfg.distiller_base_url).netloc or cfg.distiller_base_url
        if tcp_ok(cfg.distiller_base_url):
            return Check("distiller", label, "ok", host)
        return Check("distiller", label, "warn", f"{host} unreachable — falling open to heuristic")
    found = shutil.which(cfg.distiller_cmd)
    if found:
        return Check("distiller", label, "ok", found)
    return Check("distiller", label, "warn", f"{cfg.distiller_cmd!r} not on PATH — falling open to heuristic")


def scan_seconds(facts: int, dim: int) -> float:
    """Estimated pure-Python cosine scan of ``facts`` vectors of ``dim`` elements."""
    return facts * dim * PY_SCAN_NS_PER_ELEMENT / 1e9


def scan_check(cfg: Config, store: Store, project_key: str | None = None) -> Check:
    """Can recall's similarity scan keep up? numpy vectorises it; without numpy (a ``hash``
    install, or ``scorer=python``) a large project runs it in pure Python — seconds per prompt.
    Judged on ``project_key``, else on the largest project."""
    if not isinstance(get_scorer(cfg), PurePythonScorer):
        return Check("scan", "numpy", "ok", "vectorised")
    projects = [row for row in store.projects() if project_key in (None, row["project_key"])]
    if not projects:
        return Check("scan", "python", "ok", "no facts yet")
    largest = max(projects, key=lambda row: row["c"])
    dim = max(store.stored_dims(largest["project_key"]), default=cfg.dim)
    seconds = scan_seconds(largest["c"], dim)
    where = f"{largest['project_label'] or largest['project_key']}: {largest['c']:,} facts × {dim} dims"
    if seconds < SCAN_WARN_SECONDS:
        return Check("scan", "python", "ok", f"no numpy — fine at this size ({where}, ~{seconds:.1f} s/recall est.)")
    return Check(
        "scan",
        "python",
        "warn",
        f"no numpy — {where} ≈ {seconds:.0f} s per recall (est.); past ~{_HOOK_CEILING_SECONDS} s the prompt hook "
        "times out and injects nothing. Install numpy, or point the `python` option at an interpreter that has it",
    )


# Operational thresholds — when a detached job's silence means trouble rather than "nothing to do".
CAPTURE_STALL_SECONDS = 2 * 3600  # captures requested this long past the newest progress
CAPTURE_STUCK_SECONDS = 30 * 60  # one capture worker holding its lock this long
CONSOLIDATION_STUCK_SECONDS = 15 * 60  # 8 deadline-bounded stages take minutes at worst
ERROR_WINDOW_SECONDS = 24 * 3600
WAL_WARN_BYTES = 512 * 1024 * 1024
LOCK_PROBE_SECONDS = 0.1
# The marker the capture worker touches on every start (before its lock): "a capture was attempted".
CAPTURE_REQUESTED = ".capture-requested"


def _ago(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


def store_check(cfg: Config) -> Check:
    """Can a writer take the store's write lock right now? A held lock stalls every capture."""
    try:
        conn = sqlite3.connect(cfg.db_path, timeout=LOCK_PROBE_SECONDS)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.rollback()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc) or "busy" in str(exc):
            return Check("store", "sqlite", "warn", "write-locked by another connection — captures cannot land")
        return Check("store", "sqlite", "warn", f"unwritable: {exc}")
    return Check("store", "sqlite", "ok", "writable")


def capture_check(cfg: Config, store: Store, now: float | None = None) -> Check:
    """Are captures landing? Requests (the worker's start marker) outrunning the newest cursor
    progress by hours means captures are failing or blocked; a worker holding its lock for long
    is stuck (e.g. suspended)."""
    now = time.time() if now is None else now
    data_dir = Path(cfg.data_dir)
    held = singleflight.holder(data_dir / ".capture.lock", now)
    if held and held[1] >= CAPTURE_STUCK_SECONDS:
        return Check("capture", "worker", "warn", f"pid {held[0]} has held the capture lock for {_ago(held[1])}")
    progress = store.newest_capture_progress()
    try:
        requested = (data_dir / CAPTURE_REQUESTED).stat().st_mtime
    except OSError:
        requested = None
    if requested is None or progress is None:
        return Check(
            "capture",
            "worker",
            "ok",
            "no captures yet" if progress is None else f"last progress {_ago(now - progress)} ago",
        )
    if requested - progress >= CAPTURE_STALL_SECONDS:
        return Check(
            "capture",
            "worker",
            "warn",
            f"captures stalled — requested for {_ago(requested - progress)} without progress",
        )
    return Check("capture", "worker", "ok", f"last progress {_ago(now - progress)} ago")


def consolidation_check(cfg: Config, now: float | None = None) -> Check:
    held = singleflight.holder(Path(cfg.data_dir) / ".consolidate.lock", now)
    if held and held[1] >= CONSOLIDATION_STUCK_SECONDS:
        return Check("consolidation", "sleep pass", "warn", f"pid {held[0]} has been consolidating for {_ago(held[1])}")
    return Check("consolidation", "sleep pass", "ok", f"running for {_ago(held[1])}" if held else "idle")


def errors_check(cfg: Config, now: float | None = None) -> Check:
    """The newest entry in the error log (``core.errlog``), if it is recent."""
    now = time.time() if now is None else now
    event = errlog.last(cfg.data_dir)
    if event and now - event.get("ts", 0) < ERROR_WINDOW_SECONDS:
        when = _ago(now - event["ts"])
        return Check("errors", "errors.log", "warn", f"{when} ago — {event.get('source')}: {event.get('message')}")
    return Check("errors", "errors.log", "ok", f"none in the last {ERROR_WINDOW_SECONDS // 3600} h")


def wal_check(cfg: Config) -> Check:
    try:
        size = Path(str(cfg.db_path) + "-wal").stat().st_size
    except OSError:
        size = 0
    state = "warn" if size >= WAL_WARN_BYTES else "ok"
    detail = f"{size / 1e6:.0f} MB" + (" — checkpoints are not keeping up" if state == "warn" else "")
    return Check("wal", "sqlite", state, detail)


def session_warnings(cfg: Config, store: Store) -> list[str]:
    """What the SessionStart hook tells the user (a ``systemMessage``, never model context): the
    store is write-locked, captures have stalled, or a worker failed in the last day. Empty when
    healthy — so a healthy session starts exactly as before."""
    out = []
    for check in (store_check, lambda c: capture_check(c, store), errors_check):
        try:
            result = check(cfg)
        except Exception:
            continue  # fail open: a probe that breaks says nothing
        if result.state != "ok":
            out.append(f"{result.name} — {result.detail}")
    return out


def checks(cfg: Config, store: Store | None = None) -> list[Check]:
    """Every subsystem check — the service backends, then (with a store) the recall scan and the
    health of the detached capture / consolidation side."""
    out = [queue_check(), embedding_check(cfg), distiller_check(cfg)]
    if store is not None:
        out += [
            scan_check(cfg, store),
            store_check(cfg),
            capture_check(cfg, store),
            consolidation_check(cfg),
            errors_check(cfg),
            wal_check(cfg),
        ]
    return out
