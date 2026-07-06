"""Shared entry-point setup: interpreter pinning + import path.

Hooks run under whatever ``python3`` Claude Code was launched with — which varies
by project venv and rarely has ``fastembed``. To make semantic recall work
everywhere (and keep the embedder consistent across capture/recall), every entry
point re-execs under a pinned interpreter. Resolution order:

  1. ``python`` userConfig (``CLAUDE_PLUGIN_OPTION_python``) or ``ENGRAM_PYTHON`` — explicit override
  2. the plugin's self-provisioned managed venv under its data dir (the seamless default)

With none present it is a no-op and the plugin runs on the ambient Python (hash
embedder). Stdlib-only so it can run under any interpreter before re-exec.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _venv_exe(data_dir: str | os.PathLike) -> str | None:
    venv = os.path.join(str(data_dir), "venv")
    exe = os.path.join(venv, "Scripts", "python.exe") if os.name == "nt" else os.path.join(venv, "bin", "python")
    return exe if os.path.exists(exe) else None


def managed_python(data_dir: str | os.PathLike | None = None) -> str | None:
    if data_dir is not None:
        return _venv_exe(data_dir)
    explicit = os.environ.get("ENGRAM_DATA_DIR") or os.environ.get("CLAUDE_PLUGIN_DATA")
    if explicit:
        return _venv_exe(explicit)
    base = os.path.join(os.path.expanduser("~"), ".claude", "plugins", "data")
    exe = _venv_exe(os.path.join(base, "engram"))
    if exe:
        return exe
    # No default venv — a standalone run (no CLAUDE_PLUGIN_DATA) must still find the
    # venv Claude Code provisioned under a marketplace-qualified sibling, or recall
    # falls back to the ambient python's hash stub and can't read fastembed vectors.
    import glob

    for cand in sorted(glob.glob(os.path.join(base, "engram-*")), reverse=True):
        exe = _venv_exe(cand)
        if exe:
            return exe
    return None


def reexec_if_pinned() -> None:
    if os.environ.get("ENGRAM_REEXECED"):
        return
    target = os.environ.get("CLAUDE_PLUGIN_OPTION_python") or os.environ.get("ENGRAM_PYTHON") or managed_python()
    if not target or not os.path.exists(target):
        return
    # Compare by path, not realpath: two venvs built from the same base interpreter
    # share a realpath but are different environments (one has fastembed, one doesn't).
    if os.path.abspath(target) == os.path.abspath(sys.executable):
        return
    try:
        os.environ["ENGRAM_REEXECED"] = "1"
        os.execv(target, [target, os.path.abspath(sys.argv[0]), *sys.argv[1:]])
    except OSError:
        pass


def hooks_disabled() -> bool:
    """True when engram hooks must no-op — set inside engram-spawned Claude sessions.

    The ``claude`` distiller runs headless ``claude -p`` in the detached capture worker;
    that nested Claude session would otherwise fire engram's own hooks and capture the
    distiller *prompt* as if it were a session (a self-referential loop that pollutes
    memory and stuffs the rescue queue). ``ClaudeCliDistiller`` sets ``ENGRAM_DISABLE=1`` in
    that subprocess's environment; every hook entry point checks this first and exits 0.
    """
    return os.environ.get("ENGRAM_DISABLE") == "1"


def plugin_root() -> Path:
    env = os.environ.get("CLAUDE_PLUGIN_ROOT")
    root = Path(env) if env else Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return root
