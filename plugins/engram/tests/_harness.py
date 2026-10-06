"""Test harness — every test module imports it first (``import _harness  # noqa: F401``,
or ``from _harness import …``); ``test_harness`` fails any module that doesn't.

Installed once per process, before any test runs:

1. **Import paths.** The plugin root (``core``) and ``bin/`` (the hook modules) go on
   ``sys.path`` — the one copy of the bootstrap every module used to repeat.
2. **A hermetic environment.** The developer's own engram settings (``ENGRAM_*``,
   ``CLAUDE_PLUGIN_*``, ``CLAUDE_PROJECT_DIR``, ``CLAUDE_MEM_DATA_DIR``) are cleared, so tests run on the shipped
   defaults whatever the shell exports. Then ``ENGRAM_DISTILLER=heuristic``,
   ``ENGRAM_VIEWER_AUTOSTART=false`` and ``ENGRAM_DATA_DIR`` = a per-process temp dir are set;
   spawned hooks and servers inherit them (no LLM, no live store, no stray viewer process).
3. **No real LLM.** Spawning ``claude`` or calling ``urllib.request.urlopen`` (the HTTP
   distiller's transport — a local Ollama is an LLM too, and tests make no network calls)
   raises :class:`LLMCallInTest`. It is a ``BaseException`` so the distillers' fail-open
   ``except Exception`` can't swallow it: a missing pin errors the test instead of passing on
   the heuristic fallback after a real call. :func:`allow_llm_transport` opts one block back in.
4. **No live store.** A read-write SQLite open under the real plugin data dir raises
   :class:`LiveStoreInTest`; read-only (``mode=ro``) probes still work.

Per-test env changes go through :func:`scoped_env` / :func:`temp_data_dir` (or
``mock.patch.dict(os.environ, …)``), which restore the prior value — never set-then-``pop``,
which would delete the harness's own settings for every later test.
"""

from __future__ import annotations

import atexit
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent

# The program names whose spawn is a real LLM call (``ClaudeCliDistiller`` runs ``claude -p``).
LLM_PROGRAMS = frozenset({"claude"})
# Environment the developer's shell may carry that changes engram's behaviour under test.
_AMBIENT_PREFIXES = ("ENGRAM_", "CLAUDE_PLUGIN_")
_AMBIENT_NAMES = frozenset({"CLAUDE_PROJECT_DIR", "CLAUDE_MEM_DATA_DIR"})


class HarnessViolation(BaseException):
    """A test reached something the harness forbids. ``BaseException``: fail-open can't hide it."""


class LLMCallInTest(HarnessViolation):
    """A test tried to reach a real LLM (a ``claude`` spawn or an HTTP request)."""


class LiveStoreInTest(HarnessViolation):
    """A test tried to open the developer's real plugin data store read-write."""


_llm_allowed = 0  # > 0 inside allow_llm_transport()
_protected: tuple[Path, ...] = ()  # the real plugin data roots, resolved at install time


@contextmanager
def allow_llm_transport() -> Iterator[None]:
    """Let a deliberate transport test (e.g. a dead-port ``HTTPDistiller``) through the guard."""
    global _llm_allowed
    _llm_allowed += 1
    try:
        yield
    finally:
        _llm_allowed -= 1


def scoped_env(case: unittest.TestCase, **values: str | None) -> None:
    """Set env vars for one test (``None`` unsets one); the whole environment is restored at cleanup."""
    patcher = mock.patch.dict(os.environ)
    patcher.start()
    case.addCleanup(patcher.stop)
    for name, value in values.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def temp_data_dir(case: unittest.TestCase) -> tempfile.TemporaryDirectory:
    """A fresh data dir exported as ``ENGRAM_DATA_DIR`` for one test; removed at cleanup."""
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    scoped_env(case, ENGRAM_DATA_DIR=tmp.name)
    return tmp


def _program(args, executable, shell: bool) -> str:
    """The basename (no extension, lower-case) of the program a Popen call would run."""
    if executable:
        first = os.fsdecode(executable)
    elif isinstance(args, (str, bytes, os.PathLike)):
        text = os.fsdecode(args)
        first = (shlex.split(text) or [""])[0] if shell else text
    else:
        first = os.fsdecode(args[0]) if args else ""
    return os.path.splitext(os.path.basename(first))[0].lower()


def _db_target(database, uri: bool) -> tuple[Path | None, bool]:
    """(resolved path or None for in-memory, read-only?) for a ``sqlite3.connect`` target."""
    raw = os.fsdecode(database)
    read_only = False
    if uri and raw.startswith("file:"):
        parsed = urllib.parse.urlsplit(raw)
        read_only = urllib.parse.parse_qs(parsed.query).get("mode", [""])[0] == "ro"
        raw = urllib.parse.unquote(parsed.path)
    if raw in ("", ":memory:"):
        return None, read_only
    return Path(raw).expanduser().resolve(), read_only


def _is_protected(path: Path) -> bool:
    return any(path == root or path.is_relative_to(root) for root in _protected)


def _install() -> None:
    global _protected
    if getattr(sqlite3, "_engram_harness_installed", False):
        return  # already installed (imported under a second module name, or re-called)
    sqlite3._engram_harness_installed = True

    for path in (ROOT, ROOT / "bin"):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))

    # Protect the real data roots — including any the shell pointed at — before clearing them.
    roots = {Path.home() / ".claude" / "plugins" / "data"}
    roots.update(Path(os.environ[n]) for n in ("CLAUDE_PLUGIN_DATA", "ENGRAM_DATA_DIR") if os.environ.get(n))
    _protected = tuple(root.expanduser().resolve() for root in roots)

    for name in [n for n in os.environ if n.startswith(_AMBIENT_PREFIXES) or n in _AMBIENT_NAMES]:
        del os.environ[name]
    data_dir = tempfile.mkdtemp(prefix="engram-test-")
    atexit.register(shutil.rmtree, data_dir, ignore_errors=True)
    os.environ["ENGRAM_DATA_DIR"] = data_dir
    os.environ["ENGRAM_DISTILLER"] = "heuristic"
    os.environ["ENGRAM_VIEWER_AUTOSTART"] = "false"  # a SessionStart hook under test must not spawn a viewer

    real_popen_init = subprocess.Popen.__init__

    def guarded_popen_init(self, args, *rest, **kwargs):
        program = _program(args, kwargs.get("executable"), kwargs.get("shell", False))
        if program in LLM_PROGRAMS and not _llm_allowed:
            raise LLMCallInTest(f"test spawned {program!r} — pin distiller='heuristic' or stub the transport")
        real_popen_init(self, args, *rest, **kwargs)

    real_urlopen = urllib.request.urlopen

    def guarded_urlopen(url, *rest, **kwargs):
        if not _llm_allowed:
            target = getattr(url, "full_url", url)
            raise LLMCallInTest(f"test made an HTTP request to {target!r} — stub it or use allow_llm_transport()")
        return real_urlopen(url, *rest, **kwargs)

    real_connect = sqlite3.connect

    def guarded_connect(database, *rest, **kwargs):
        uri = kwargs.get("uri", rest[6] if len(rest) > 6 else False)
        path, read_only = _db_target(database, uri)
        if path is not None and not read_only and _is_protected(path):
            raise LiveStoreInTest(f"test opened the live store read-write: {path}")
        return real_connect(database, *rest, **kwargs)

    subprocess.Popen.__init__ = guarded_popen_init
    urllib.request.urlopen = guarded_urlopen
    sqlite3.connect = guarded_connect


_install()
