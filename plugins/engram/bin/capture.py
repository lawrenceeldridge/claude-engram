#!/usr/bin/env python3
"""SessionEnd / PreCompact hook — capture the session into memory.

Distillation + embedding are heavy, so the hook spawns a detached worker and
returns immediately: zero interactive-token cost and no latency added to the
user's turn. The worker reads the payload from a temp file, distils the
transcript into atomic facts, embeds and persists them, then deletes the file.
Fails open.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from _bootstrap import hooks_disabled, plugin_root, reexec_if_pinned

reexec_if_pinned()
plugin_root()


def _attempt(cfg, step: str, run) -> None:
    """Run one best-effort step of the worker: a failure is recorded in the error log, never
    raised — a capture must never break, but it must not vanish without a trace either."""
    from core import errlog

    try:
        run()
    except Exception as exc:
        errlog.record(cfg.data_dir, "capture", f"{step}: {exc!r}")


def _capture(store, embedder, cfg, project: dict, payload: dict, checkpoint: bool) -> None:
    """Everything that runs under the capture lock: the transcript delta and its follow-ons."""
    import time

    from core.service import capture_transcript_incremental, maybe_capture_antipatterns, maybe_capture_summary

    session_id = payload.get("session_id", "")
    transcript_path = payload["transcript_path"]
    _attempt(
        cfg,
        "transcript",
        lambda: capture_transcript_incremental(store, embedder, cfg, project, session_id, transcript_path),
    )
    # Session summary: forced at SessionEnd/PreCompact (reliable checkpoints where context is
    # about to be lost), throttled-by-growth on Stop so it stays current each turn without a
    # full-transcript LLM call every turn.
    _attempt(
        cfg,
        "summary",
        lambda: maybe_capture_summary(store, embedder, cfg, project, session_id, transcript_path, force=checkpoint),
    )
    # Anti-pattern catalogue: same cadence as the summary (throttle + force on checkpoints), but
    # additionally gated by a cheap admission-marker scan so mistake-free sessions cost nothing.
    # No-op unless enabled and an LLM distiller is configured.
    if cfg.antipatterns:
        _attempt(
            cfg,
            "antipatterns",
            lambda: maybe_capture_antipatterns(
                store, embedder, cfg, project, session_id, transcript_path, force=checkpoint
            ),
        )
    if cfg.ttl_days > 0:
        _attempt(
            cfg,
            "sweep",
            lambda: store.sweep(time.time(), cfg.ttl_days * 86400, cfg.ttl_keep_frequency, project["key"]),
        )

    # Work-queue maintenance (cheap, every capture): re-queue interrupted leases, dead-letter
    # pending items older than queue_dead_after (an item no worker ever pulls, e.g. rescue with
    # no LLM distiller to drain it), then delete dead-letters that have sat unrescued past
    # queue_dead_purge_after so the queue can't accumulate forever.
    def queue_maintenance() -> None:
        store.reclaim_expired()
        store.dead_stale(cfg.queue_dead_after)
        if cfg.queue_dead_purge_after > 0:
            store.purge_dead(cfg.queue_dead_after + cfg.queue_dead_purge_after)

    _attempt(cfg, "work queue", queue_maintenance)

    # Sensory register (visual): promote attended perceptions into the index (the visual
    # long-term-store column), then decay/purge the rest. Embedding happens HERE in the
    # detached worker — never on the intake hook.
    if cfg.sensory_enabled:

        def sensory() -> None:
            from core.service import promote_visual_perceptions

            promote_visual_perceptions(store, embedder, cfg, project, time.time())
            store.sweep_sensory(project["key"], cfg.sensory_capacity, cfg.sensory_ttl_seconds, time.time())

        _attempt(cfg, "sensory", sensory)


def _run_worker(payload_path: str) -> None:
    try:
        with open(payload_path, encoding="utf-8") as fh:
            payload = json.load(fh)
    finally:
        try:
            os.unlink(payload_path)
        except OSError:
            pass

    from core import health, singleflight
    from core.config import get_config
    from core.ports.embedding import get_embedder
    from core.project import resolve_project
    from core.store import Store

    cfg = get_config()
    transcript_path = payload.get("transcript_path")
    if not transcript_path or not Path(transcript_path).exists():
        return
    project = resolve_project(
        payload.get("cwd") or os.getcwd(),
        cfg.markers,
        identity=cfg.identity,
        project_dir=payload.get("project_dir") or cfg.project_dir,
    )
    checkpoint = payload.get("hook_event_name") in ("SessionEnd", "PreCompact")
    data_dir = Path(cfg.data_dir)
    # "A capture was attempted" — before the lock, so a stuck or locked-out worker still marks it;
    # core.health compares it with cursor progress to tell "stalled" from "idle".
    _attempt(cfg, "request marker", lambda: (data_dir / health.CAPTURE_REQUESTED).touch())
    with singleflight.held(data_dir / ".capture.lock") as mine:
        if not mine:
            return  # another capture worker is running; the cursor covers this delta next time
        # Loaded only once the lock is ours: a pile-up of workers must not each load the model.
        embedder = get_embedder(cfg)
        store = Store(cfg.db_path)
        _capture(store, embedder, cfg, project, payload, checkpoint)
    try:
        # Consolidation ("sleep") — at session boundaries, not every turn (like sleep itself) —
        # runs AFTER the capture lock is released, under its own: a slow pass (the 13.5 h refine)
        # can delay the next consolidation, never a capture. Skipped if a pass is already running;
        # the next checkpoint consolidates. Each stage is deadline-bounded inside consolidate().
        if checkpoint:
            with singleflight.held(data_dir / ".consolidate.lock") as mine:
                if mine:
                    from core.consolidation import consolidate

                    _attempt(cfg, "consolidate", lambda: consolidate(store, cfg, project, embedder=embedder))
    finally:
        store.close()


def main() -> int:
    if hooks_disabled():
        return 0  # inside an engram-spawned `claude -p` — don't capture the distiller session
    if "--worker" in sys.argv:
        _run_worker(sys.argv[-1])
        return 0

    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    # Stamp the workspace root (this hook's env) into the payload so the detached worker
    # resolves the right project even if it doesn't inherit CLAUDE_PROJECT_DIR.
    if "project_dir" not in payload and os.environ.get("CLAUDE_PROJECT_DIR"):
        payload["project_dir"] = os.environ["CLAUDE_PROJECT_DIR"]

    try:
        fd, payload_path = tempfile.mkstemp(prefix="engram-cap-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--worker", payload_path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:  # fail-open backstop
        print(f"[engram] capture spawn failed: {exc}", file=sys.stderr)
        try:
            from core import errlog
            from core.config import get_config

            errlog.record(get_config().data_dir, "capture", f"spawn failed: {exc!r}")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
