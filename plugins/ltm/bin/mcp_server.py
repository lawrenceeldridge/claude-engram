#!/usr/bin/env python3
"""ltm memory MCP server — expose recall as an on-demand tool (Remote Facade).

A pure-stdlib JSON-RPC 2.0 server speaking MCP over newline-delimited stdio: no
framework, no new dependencies, preserving the plugin's local-first/zero-install
ethos. It is a thin facade over the Service Layer (``core.service``) — it never
reimplements ranking; it calls ``recall_structured`` / ``list_projects`` and
formats the result.

The point is the *pull* path: passive hooks push memory at the model; this lets
the model deliberately consult memory before an expensive Grep/Glob/Task search,
and read a calibrated confidence + verdict to decide whether to trust it.

Read-only by design (recall + list_projects). Writes (save_memory) are a later,
opt-in tier. Fails soft: a handler error returns a JSON-RPC error, never crashes
the loop.
"""

from __future__ import annotations

import json
import sys
from collections import OrderedDict

from _bootstrap import plugin_root, reexec_if_pinned

reexec_if_pinned()
ROOT = plugin_root()

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "ltm-memory", "version": "0.6.0"}

TOOLS = [
    {
        "name": "recall",
        "description": (
            "Search this project's long-term memory for distilled facts relevant to a query. "
            "Call this BEFORE a broad Grep/Glob/Task code search: it is a cheap vector lookup "
            "over a compact store, not a file scan. Returns facts plus a calibrated `confidence` "
            "(0-1) and a `verdict`: `ok` (trust the facts, skip the wider search), "
            "`low_confidence` (hints only — widen if they don't answer), or `no_memory` "
            "(nothing stored — do not assume prior context)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What you want to recall."},
                "project": {
                    "type": "string",
                    "description": "Optional project label/path to search; defaults to the current project.",
                },
                "k": {"type": "integer", "description": "Max facts to return (default from config)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_projects",
        "description": "List every project in the global memory store with its active-fact count. Use to discover project labels for `recall`.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "search_docs",
        "description": (
            "Search this project's INDEXED DOCUMENTATION (markdown) for sections relevant to a "
            "query. Call this BEFORE Grep/Glob/Read over docs: it returns ranked section outlines "
            "(heading breadcrumb + one-line summary + anchor + freshness), not file contents, so it "
            "is a cheap lookup instead of reading whole files. Then call `get_doc_section` on the "
            "anchor you want to read its full text. `freshness` is fresh|edited|gone per file."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What you want to find in the docs."},
                "project": {"type": "string", "description": "Optional project label/path; defaults to current."},
                "k": {"type": "integer", "description": "Max sections to return (default 10)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_doc_section",
        "description": (
            "Fetch one documentation section's full text by its `anchor` (or id) from `search_docs` "
            "/ `doc_outline`. Returns the section body plus a section-precise `freshness` "
            "(fresh|edited|stale|gone) verified against the live file. Cheaper than Read — one "
            "section, not the whole document."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "The section anchor (e.g. 'installation/prerequisites') or id.",
                },
                "project": {"type": "string", "description": "Optional project label/path; defaults to current."},
            },
            "required": ["ref"],
        },
    },
    {
        "name": "doc_outline",
        "description": (
            "List the section skeleton of this project's indexed docs — heading breadcrumbs, anchors "
            "and one-line summaries, with NO bodies. Use to understand what documentation exists "
            "before searching or reading. Optionally scope to one file via `source_path`."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Optional project label/path; defaults to current."},
                "source_path": {
                    "type": "string",
                    "description": "Optional repo-relative file to scope the outline to.",
                },
            },
        },
    },
    {
        "name": "search_code",
        "description": (
            "Search this project's INDEXED CODE for symbols (functions, classes, methods, and — for "
            "TypeScript/JavaScript — interfaces, types, enums, arrow-function consts) relevant to a "
            "query. Covers Python plus TS/TSX/JS/JSX/MJS/CJS. Call this BEFORE Grep/Glob/Read over "
            "source: it returns ranked symbol outlines (qualified name + signature/docstring summary "
            "+ anchor + freshness), not file contents. Then call `get_symbol` on the anchor to read "
            "its full source. The anchor is the dotted qualname (e.g. 'Store.chunk_id' or "
            "'PartiesCard')."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What you want to find in the code."},
                "project": {"type": "string", "description": "Optional project label/path; defaults to current."},
                "k": {"type": "integer", "description": "Max symbols to return (default 10)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_symbol",
        "description": (
            "Fetch one code symbol's full source by its `anchor` (dotted qualname, e.g. "
            "'Store.chunk_id') or id, from `search_code` / `code_outline`. Works for any indexed "
            "language (Python, TS/JS/TSX/JSX). Returns the symbol body "
            "plus a symbol-precise `freshness` (fresh|edited|stale|gone) verified against the live "
            "file. Cheaper than Read — one symbol, not the whole file."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "The symbol anchor (dotted qualname) or id."},
                "project": {"type": "string", "description": "Optional project label/path; defaults to current."},
            },
            "required": ["ref"],
        },
    },
    {
        "name": "code_outline",
        "description": (
            "List the symbol skeleton of this project's indexed code (Python + TS/JS/TSX/JSX) — "
            "qualified names, signatures and docstring summaries, with NO bodies. Use to understand a "
            "module's public surface before searching or reading. Optionally scope to one file via "
            "`source_path`."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Optional project label/path; defaults to current."},
                "source_path": {
                    "type": "string",
                    "description": "Optional repo-relative source file (.py/.ts/.tsx/.js/.jsx) to scope to.",
                },
            },
        },
    },
    {
        "name": "index_docs",
        "description": (
            "Build or refresh the code/docs index (markdown + Python/TS/JS). Incremental: unchanged "
            "files are skipped via a content-hash short-circuit, so re-running is cheap. Auto-index on "
            "session start skips very large trees (e.g. a whole monorepo); pass `path` to index a "
            "specific subtree unbounded (e.g. one app inside a monorepo)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Optional project label/path; defaults to current."},
                "path": {
                    "type": "string",
                    "description": "Optional absolute subtree to index (unbounded); defaults to the project root.",
                },
            },
        },
    },
]


_CACHE_MAX = 128


class _Engine:
    """Lazily-initialised, process-lifetime store + embedder (warm across calls).

    Recall results are cached by (project, query, k) for the server's lifetime, so
    repeated in-session recalls skip re-embedding and re-ranking. The cache is
    invalidated whenever the store's SQLite ``data_version`` changes — i.e. when a
    capture in another process writes new facts — so it never serves stale memory.
    """

    def __init__(self) -> None:
        self._ready = False
        self._cache: OrderedDict[tuple, dict] = OrderedDict()
        self._cache_version = -1

    def _init(self) -> None:
        if self._ready:
            return
        from core.config import get_config
        from core.ports.embedding import get_embedder
        from core.store import Store

        self.cfg = get_config()
        self.store = Store(self.cfg.db_path)
        self.embedder = get_embedder(self.cfg)
        self._ready = True

    def _cache_get(self, key: tuple) -> dict | None:
        version = self.store.data_version()
        if version != self._cache_version:
            self._cache.clear()
            self._cache_version = version
        hit = self._cache.get(key)
        if hit is not None:
            self._cache.move_to_end(key)
        return hit

    def _cache_put(self, key: tuple, value: dict) -> None:
        self._cache[key] = value
        self._cache.move_to_end(key)
        while len(self._cache) > _CACHE_MAX:
            self._cache.popitem(last=False)

    def _project(self, ref: str | None):
        from core.project import resolve_project

        current = resolve_project(None, self.cfg.markers)
        if not ref:
            return current
        needle = ref.lower()
        for row in self.store.projects():
            if needle in (row["project_label"] or "").lower() or needle in (row["project_path"] or "").lower():
                return {"key": row["project_key"], "path": row["project_path"], "label": row["project_label"]}
        return current

    def recall(self, args: dict) -> dict:
        self._init()
        query = (args.get("query") or "").strip()
        if not query:
            return {"verdict": "no_memory", "facts": [], "error": "empty query"}
        project = self._project(args.get("project"))
        k = args.get("k")
        key = (project["key"], query, k)
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        result = self._recall_via_daemon(project, query, k)
        if result is None:
            from core.service import recall_structured

            result = recall_structured(self.store, self.embedder, self.cfg, project, query, k=k)
        self._cache_put(key, result)
        return result

    def _recall_via_daemon(self, project, query: str, k) -> dict | None:
        """Delegate recall to the resident daemon's warm embedder, or None if unreachable.

        This is the primary path: the daemon holds the same embedder capture writes
        through, so query and stored vectors share one space. Falling back to the
        MCP process's own embedder (which may resolve a different backend from a
        thinner env) is what silently returns 'no memory' against a full store; the
        dim guard in recall_structured turns that fallback failure loud.
        """
        from core.daemon_client import request

        resp = request(
            self.cfg.sock_path,
            {"op": "recall_structured", "project": dict(project), "query": query, "k": k},
            timeout=5,
        )
        if not isinstance(resp, dict) or "error" in resp or "verdict" not in resp:
            return None
        return resp

    def list_projects(self, _args: dict) -> dict:
        self._init()
        projects = [
            {"label": row["project_label"], "path": row["project_path"], "facts": row["c"]}
            for row in self.store.projects()
        ]
        return {"projects": projects, "count": len(projects)}

    def search_docs(self, args: dict) -> dict:
        self._init()
        from core.index.index_recall import search_index

        project = self._project(args.get("project"))
        return search_index(
            self.store,
            self.embedder,
            self.cfg,
            project,
            args.get("query") or "",
            k=args.get("k"),
            kind="doc_section",
        )

    def search_code(self, args: dict) -> dict:
        self._init()
        from core.index.index_recall import search_index

        project = self._project(args.get("project"))
        return search_index(
            self.store,
            self.embedder,
            self.cfg,
            project,
            args.get("query") or "",
            k=args.get("k"),
            kind="code_symbol",
        )

    def get_doc_section(self, args: dict) -> dict:
        self._init()
        from core.index.index_recall import get_chunk

        project = self._project(args.get("project"))
        return get_chunk(self.store, project, args.get("ref") or "")

    def get_symbol(self, args: dict) -> dict:
        self._init()
        from core.index.index_recall import get_chunk

        project = self._project(args.get("project"))
        return get_chunk(self.store, project, args.get("ref") or "")

    def doc_outline(self, args: dict) -> dict:
        self._init()
        from core.index.index_recall import get_outline

        project = self._project(args.get("project"))
        return get_outline(self.store, project, args.get("source_path"), kind="doc_section")

    def code_outline(self, args: dict) -> dict:
        self._init()
        from core.index.index_recall import get_outline

        project = self._project(args.get("project"))
        return get_outline(self.store, project, args.get("source_path"), kind="code_symbol")

    def index_docs(self, args: dict) -> dict:
        self._init()
        from core.index.indexer import index_project

        project = self._project(args.get("project"))
        root = args.get("path") or project["path"]
        stats = index_project(self.store, self.embedder, self.cfg, project, root)
        return {"project": project["label"], "root": root, **stats}


ENGINE = _Engine()


def _tool_call(name: str, args: dict) -> dict:
    if name == "recall":
        payload = ENGINE.recall(args)
    elif name == "list_projects":
        payload = ENGINE.list_projects(args)
    elif name == "search_docs":
        payload = ENGINE.search_docs(args)
    elif name == "search_code":
        payload = ENGINE.search_code(args)
    elif name == "get_doc_section":
        payload = ENGINE.get_doc_section(args)
    elif name == "get_symbol":
        payload = ENGINE.get_symbol(args)
    elif name == "doc_outline":
        payload = ENGINE.doc_outline(args)
    elif name == "code_outline":
        payload = ENGINE.code_outline(args)
    elif name == "index_docs":
        payload = ENGINE.index_docs(args)
    else:
        raise ValueError(f"unknown tool {name!r}")
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}


def _handle(request: dict) -> dict | None:
    """Dispatch one JSON-RPC request. Returns a response, or None for notifications."""
    method = request.get("method")
    req_id = request.get("id")

    if method == "initialize":
        result = {
            "protocolVersion": (request.get("params") or {}).get("protocolVersion", PROTOCOL_VERSION),
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        }
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = request.get("params") or {}
        result = _tool_call(params.get("name"), params.get("arguments") or {})
    elif method == "ping":
        result = {}
    elif method is not None and method.startswith("notifications/"):
        return None  # notifications get no response
    else:
        if req_id is None:
            return None
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"method not found: {method}"}}

    if req_id is None:
        return None
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _write(message: dict) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        try:
            response = _handle(request)
        except Exception as exc:  # fail-soft: report, keep serving
            response = {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "error": {"code": -32603, "message": str(exc)},
            }
            if request.get("id") is None:
                response = None
        if response is not None:
            _write(response)
    return 0


if __name__ == "__main__":
    sys.exit(main())
