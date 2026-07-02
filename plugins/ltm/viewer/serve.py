#!/usr/bin/env python3
"""Localhost viewer — browse the cross-project memory store in a browser.

Read-only over the global SQLite store, so it is safe to run alongside live
sessions. Lists every project, and searches within one using the same ranking as
the recall path. Pure stdlib (http.server) — no build step, no dependencies.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(os.environ.get("CLAUDE_PLUGIN_ROOT") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))

from core.config import get_config  # noqa: E402
from core.embedding import get_embedder  # noqa: E402
from core.recall import search  # noqa: E402
from core.store import Store  # noqa: E402

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>claude-ltm</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;
         background:#0d1117; color:#c9d1d9; }
  header { padding:14px 18px; border-bottom:1px solid #21262d; display:flex;
           gap:12px; align-items:center; flex-wrap:wrap; }
  h1 { font-size:15px; margin:0; color:#58a6ff; }
  select,input { background:#0d1117; color:#c9d1d9; border:1px solid #30363d;
                 border-radius:6px; padding:6px 9px; font:inherit; }
  input { flex:1; min-width:200px; }
  main { padding:14px 18px; }
  .fact { border:1px solid #21262d; border-radius:8px; padding:10px 12px;
          margin-bottom:8px; }
  .meta { color:#8b949e; font-size:12px; margin-top:5px; }
  .score { color:#3fb950; }
  .fact .title { font-weight:600; color:#e6edf3; margin-bottom:2px; }
  .fact .narr { color:#adbac7; margin-top:5px; white-space:pre-wrap; }
  .fact .files { margin-top:6px; display:flex; flex-wrap:wrap; gap:4px; }
  .fact .file { font-size:11px; color:#8b949e; background:#161b22; border:1px solid #21262d;
                border-radius:5px; padding:1px 6px; }
  .fact .kind { font-size:11px; color:#d29922; text-transform:uppercase; letter-spacing:.04em; }
  .empty { color:#8b949e; padding:20px 0; }
  #live { margin-inline-start:auto; font-size:12px; color:#8b949e; display:flex;
          align-items:center; gap:6px; }
  #live .dot { width:8px; height:8px; border-radius:50%; background:#3fb950;
               box-shadow:0 0 6px #3fb950; }
  #live.off .dot { background:#8b949e; box-shadow:none; }
  .fact.flash { animation:flash 1.2s ease-out; }
  @keyframes flash { from { border-color:#3fb950; } to { border-color:#21262d; } }
</style></head>
<body>
<header>
  <h1>claude-ltm</h1>
  <select id="project"></select>
  <input id="q" placeholder="semantic search within project… (blank = list all)">
  <span id="live" class="off"><span class="dot"></span><span id="live-label">connecting…</span></span>
</header>
<main><div id="list" class="empty">Loading…</div></main>
<script>
const $ = s => document.querySelector(s);
const esc = s => (s==null?'':String(s)).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const PAGE = 50;
let offset = 0, loading = false, exhausted = false, mode = 'list';

async function loadProjects() {
  const rows = await (await fetch('/api/projects')).json();
  const sel = $('#project');
  const prev = sel.value;
  sel.innerHTML = rows.map(r =>
    `<option value="${r.project_key}">${r.label} (${r.count})</option>`).join('');
  if (rows.some(r => r.project_key === prev)) sel.value = prev;  // keep selection across live refresh
  return rows.length;
}
function factHTML(r, flash) {
  const when = new Date(r.created*1000).toISOString().slice(0,16).replace('T',' ');
  const score = r.score==null ? '' : `<span class="score">${r.score}</span> · `;
  const kind = (r.kind && r.kind!=='fact') ? `<div class="kind">${esc(r.kind)}</div>` : '';
  const title = r.title ? `<div class="title">${esc(r.title)}</div>` : '';
  const narr = r.narrative ? `<div class="narr">${esc(r.narrative)}</div>` : '';
  const files = (r.files&&r.files.length)
    ? `<div class="files">${r.files.map(f=>`<span class="file">${esc(f)}</span>`).join('')}</div>` : '';
  return `<div class="${flash?'fact flash':'fact'}">${kind}${title}<div class="text">${esc(r.text)}</div>${narr}${files}<div class="meta">${score}${esc(r.kind)} · ${when}</div></div>`;
}
async function fetchFacts(extra='') {
  const pk = $('#project').value, q = $('#q').value.trim();
  const url = `/api/facts?project=${encodeURIComponent(pk)}&q=${encodeURIComponent(q)}${extra}`;
  return await (await fetch(url)).json();
}
// Full re-render from the top: a query shows all ranked search hits; a blank query
// shows the first (newest) page of the browse list, which grows via loadMore().
async function reload(flash) {
  const q = $('#q').value.trim();
  mode = q ? 'search' : 'list';
  offset = 0; exhausted = false;
  const rows = q ? await fetchFacts() : await fetchFacts(`&limit=${PAGE}&offset=0`);
  if (mode === 'list') { offset = rows.length; exhausted = rows.length < PAGE; }
  $('#list').innerHTML = rows.length
    ? rows.map(r => factHTML(r, flash)).join('')
    : '<div class="empty">No facts.</div>';
}
// Infinite scroll: append the next page of the browse list. Inert during search.
async function loadMore() {
  if (mode !== 'list' || loading || exhausted) return;
  loading = true;
  const rows = await fetchFacts(`&limit=${PAGE}&offset=${offset}`);
  offset += rows.length; exhausted = rows.length < PAGE;
  if (rows.length) $('#list').insertAdjacentHTML('beforeend', rows.map(r => factHTML(r, false)).join(''));
  loading = false;
}
$('#project').addEventListener('change', () => reload());
let t; $('#q').addEventListener('input', () => { clearTimeout(t); t=setTimeout(() => reload(),180); });
window.addEventListener('scroll', () => {
  if (window.innerHeight + window.scrollY >= document.body.offsetHeight - 400) loadMore();
});

// Live updates: the server pushes a `change` event whenever the memory store is
// written to (capture from any session). EventSource auto-reconnects on drop.
function connectStream() {
  const es = new EventSource('/events');
  const badge = $('#live'), label = $('#live-label');
  es.onopen = () => { badge.classList.remove('off'); label.textContent = 'live'; };
  es.addEventListener('change', async () => {
    await loadProjects();      // refresh counts + keep current project selected
    await reload(true);        // newest-first: a fresh capture appears at the top with a highlight
  });
  es.onerror = () => { badge.classList.add('off'); label.textContent = 'reconnecting…'; };
}
(async () => {
  const n = await loadProjects();
  if (n) await reload();
  else $('#list').textContent = 'No memory captured yet.';
  connectStream();
})();
</script>
</body></html>
"""


def _int_param(params: dict, name: str) -> int | None:
    try:
        return int(params.get(name, [""])[0])
    except (TypeError, ValueError):
        return None


def _fact_row(row, score) -> dict:
    try:
        files = json.loads(row["files"]) if row["files"] else []
    except (ValueError, TypeError):
        files = []
    return {
        "text": row["text"],
        "score": score,
        "kind": row["kind"],
        "created": row["created_at"],
        "title": row["title"],
        "narrative": row["narrative"],
        "files": files,
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body, ctype: str = "application/json") -> None:
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _stream(self, cfg) -> None:
        """Hold the connection open and push an SSE `change` event whenever the

        store is written to. `PRAGMA data_version` increments on every commit made
        by another connection (the capture process), so it is a cheap, exact change
        signal without polling row counts.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        db = sqlite3.connect(str(cfg.db_path))
        try:
            last = db.execute("PRAGMA data_version").fetchone()[0]
            idle = 0
            while True:
                version = db.execute("PRAGMA data_version").fetchone()[0]
                if version != last:
                    last = version
                    self.wfile.write(b"event: change\ndata: 1\n\n")
                    self.wfile.flush()
                    idle = 0
                else:
                    idle += 1
                    if idle >= 15:  # heartbeat so proxies/browsers hold the connection
                        idle = 0
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                time.sleep(1)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # client navigated away; EventSource will reconnect
        finally:
            db.close()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        cfg = get_config()
        if parsed.path == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif parsed.path == "/events":
            self._stream(cfg)
        elif parsed.path == "/api/projects":
            store = Store(cfg.db_path)
            out = [
                {"project_key": r["project_key"], "label": r["project_label"], "count": r["c"]}
                for r in store.projects()
            ]
            store.close()
            self._send(200, json.dumps(out))
        elif parsed.path == "/api/facts":
            params = parse_qs(parsed.query)
            project_key = params.get("project", [""])[0]
            query = params.get("q", [""])[0].strip()
            store = Store(cfg.db_path)
            if query and project_key:
                project = {"key": project_key, "path": "", "label": ""}
                # Search ranks the whole active collection server-side, so it stays
                # comprehensive regardless of how much the browse list has lazily loaded.
                k = store.active_count(project_key) or 1
                hits = search(store, get_embedder(cfg), project, query, cfg, k=k, min_sim=-1.0)
                out = [_fact_row(r, round(s, 3)) for s, r in hits]
            else:
                limit = _int_param(params, "limit")
                offset = _int_param(params, "offset") or 0
                rows = store.rows_for_project(project_key, limit=limit, offset=offset)
                out = [_fact_row(r, None) for r in rows]
            store.close()
            self._send(200, json.dumps(out))
        else:
            self._send(404, "{}")

    def log_message(self, *_args) -> None:  # silence request logging
        pass


def _viewer_alive(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        return True
    except OSError:
        return False


def ensure_viewer(port: int, plugin_root: str) -> None:
    """Start the viewer as a detached background process if it isn't already up.

    Mirrors ``ensure_daemon``: idempotent (one instance across all sessions via the
    port check) and detached with ``start_new_session=True`` so it outlives the
    Claude Code session that spawned it.
    """
    if _viewer_alive(port):
        return
    ltm = os.path.join(plugin_root, "bin", "ltm")
    try:
        subprocess.Popen(
            [sys.executable, ltm, "viewer", "--no-open", "--port", str(port)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass


def stop_viewer() -> bool:
    """Stop the resident viewer via its PID file. Returns True if one was killed."""
    cfg = get_config()
    try:
        pid = int(cfg.viewer_pid_path.read_text())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 15)
    except OSError:
        pass
    cfg.viewer_pid_path.unlink(missing_ok=True)
    return True


def serve(port: int = 7801, open_browser: bool = True) -> None:
    cfg = get_config()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    cfg.viewer_pid_path.write_text(str(os.getpid()))
    url = f"http://127.0.0.1:{port}/"
    print(f"[ltm] viewer at {url}  (ctrl-c to stop)")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
    finally:
        cfg.viewer_pid_path.unlink(missing_ok=True)


if __name__ == "__main__":
    serve()
