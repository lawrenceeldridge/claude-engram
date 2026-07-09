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


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _acquire_lock(lock: Path) -> bool:
    """Single-flight: at most one capture worker runs at a time (a dead holder is stolen).

    Without this, a slow/unreachable distiller lets a Stop-per-turn across several windows
    pile up dozens of workers that each load the embedder and hang on LLM calls — enough to
    freeze the machine. Capture is cursor-based, so a skipped run's delta is picked up by the
    next one; serialising is free of data loss.
    """
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            holder = int(lock.read_text().strip() or 0)
        except (OSError, ValueError):
            holder = 0
        if holder and _alive(holder):
            return False
        try:
            lock.unlink()
        except OSError:
            return False
        return _acquire_lock(lock)


def _run_worker(payload_path: str) -> None:
    try:
        with open(payload_path, encoding="utf-8") as fh:
            payload = json.load(fh)
    finally:
        try:
            os.unlink(payload_path)
        except OSError:
            pass

    from core.config import get_config
    from core.ports.embedding import get_embedder
    from core.project import resolve_project
    from core.service import capture_transcript_incremental, maybe_capture_antipatterns, maybe_capture_summary
    from core.store import Store

    cfg = get_config()
    lock = Path(cfg.data_dir) / ".capture.lock"
    if not _acquire_lock(lock):
        return  # another capture worker is running; the cursor covers this delta next time
    try:
        project = resolve_project(
            payload.get("cwd") or os.getcwd(),
            cfg.markers,
            identity=cfg.identity,
            project_dir=payload.get("project_dir") or cfg.project_dir,
        )
        transcript_path = payload.get("transcript_path")
        if not transcript_path or not Path(transcript_path).exists():
            return
        session_id = payload.get("session_id", "")
        embedder = get_embedder(cfg)
        store = Store(cfg.db_path)
        capture_transcript_incremental(store, embedder, cfg, project, session_id, transcript_path)
        # Session summary: forced at SessionEnd/PreCompact (reliable checkpoints where
        # context is about to be lost), throttled-by-growth on Stop so it stays current
        # each turn without a full-transcript LLM call every turn.
        checkpoint = payload.get("hook_event_name") in ("SessionEnd", "PreCompact")
        maybe_capture_summary(store, embedder, cfg, project, session_id, transcript_path, force=checkpoint)
        # Anti-pattern catalogue: same cadence as the summary (throttle + force on checkpoints),
        # but additionally gated by a cheap admission-marker scan so mistake-free sessions cost
        # nothing. No-op unless enabled and an LLM distiller is configured.
        if cfg.antipatterns:
            maybe_capture_antipatterns(store, embedder, cfg, project, session_id, transcript_path, force=checkpoint)
        if cfg.ttl_days > 0:
            import time

            store.sweep(time.time(), cfg.ttl_days * 86400, cfg.ttl_keep_frequency, project["key"])
        # Work-queue maintenance (cheap, every capture): re-queue interrupted leases, and
        # dead-letter pending items older than queue_dead_after so an item no worker ever
        # pulls (e.g. rescue with no LLM distiller to drain it) can't accumulate forever.
        try:
            store.reclaim_expired()
            store.dead_stale(cfg.queue_dead_after)
        except Exception:
            pass
        # Sensory register (visual): promote attended perceptions into the index (the visual
        # long-term-store column), then decay/purge the rest. Embedding happens HERE in the
        # detached worker — never on the intake hook. Best-effort: never lose a capture over it.
        if cfg.sensory_enabled:
            try:
                import time

                from core.service import promote_visual_perceptions

                promote_visual_perceptions(store, embedder, cfg, project, time.time())
                store.sweep_sensory(project["key"], cfg.sensory_capacity, cfg.sensory_ttl_seconds, time.time())
            except Exception:
                pass
        # Consolidation ("sleep") — run at session boundaries, not every turn (like sleep
        # itself): replay promotes recalled short-term facts, displacement caps STM, and
        # refine/purge prune only when enabled (default no-op). Best-effort: never lose a
        # capture over consolidation.
        if checkpoint:
            try:
                from core.consolidation import consolidate

                consolidate(store, cfg, project, embedder=embedder)
            except Exception:
                pass
        store.close()
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


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
    return 0


if __name__ == "__main__":
    sys.exit(main())
