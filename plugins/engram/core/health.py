"""Service health — one list of checks, rendered by ``engram doctor``, the viewer's
``/api/health`` and ``engram import``.

Each :class:`Check` says whether a configured backend is live (``ok``) or whether a stdlib
fallback is in force or recall is degrading (``warn``). Never an error: health is read-only, off
the hot path, and every probe fails open.
"""

from __future__ import annotations

import shutil
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

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


def checks(cfg: Config, store: Store | None = None) -> list[Check]:
    """Every subsystem check (the scan check needs a store)."""
    out = [queue_check(), embedding_check(cfg), distiller_check(cfg)]
    if store is not None:
        out.append(scan_check(cfg, store))
    return out
