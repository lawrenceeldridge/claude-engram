#!/usr/bin/env python3
"""Localhost viewer — browse the cross-project memory store in a browser.

Read-only over the global SQLite store, so it is safe to run alongside live
sessions. Lists every project, and searches within one using the same ranking as
the recall path. Pure stdlib (http.server) — no build step, no dependencies.
"""

from __future__ import annotations

import json
import os
import sys
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
  .empty { color:#8b949e; padding:20px 0; }
</style></head>
<body>
<header>
  <h1>claude-ltm</h1>
  <select id="project"></select>
  <input id="q" placeholder="semantic search within project… (blank = list all)">
</header>
<main><div id="list" class="empty">Loading…</div></main>
<script>
const $ = s => document.querySelector(s);
async function loadProjects() {
  const rows = await (await fetch('/api/projects')).json();
  const sel = $('#project');
  sel.innerHTML = rows.map(r =>
    `<option value="${r.project_key}">${r.label} (${r.count})</option>`).join('');
  if (rows.length) loadFacts();
  else $('#list').textContent = 'No memory captured yet.';
}
async function loadFacts() {
  const pk = $('#project').value, q = $('#q').value.trim();
  const url = `/api/facts?project=${encodeURIComponent(pk)}&q=${encodeURIComponent(q)}`;
  const rows = await (await fetch(url)).json();
  if (!rows.length) { $('#list').innerHTML = '<div class="empty">No facts.</div>'; return; }
  $('#list').innerHTML = rows.map(r => {
    const when = new Date(r.created*1000).toISOString().slice(0,16).replace('T',' ');
    const score = r.score==null ? '' : `<span class="score">${r.score}</span> · `;
    return `<div class="fact">${r.text}<div class="meta">${score}${r.kind} · ${when}</div></div>`;
  }).join('');
}
$('#project').addEventListener('change', loadFacts);
let t; $('#q').addEventListener('input', () => { clearTimeout(t); t=setTimeout(loadFacts,180); });
loadProjects();
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body, ctype: str = "application/json") -> None:
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        cfg = get_config()
        if parsed.path == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")
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
                hits = search(store, get_embedder(cfg), project, query, cfg, k=50, min_sim=-1.0)
                out = [
                    {"text": r["text"], "score": round(s, 3), "kind": r["kind"], "created": r["created_at"]}
                    for s, r in hits
                ]
            else:
                out = [
                    {"text": r["text"], "score": None, "kind": r["kind"], "created": r["created_at"]}
                    for r in store.rows_for_project(project_key)
                ]
            store.close()
            self._send(200, json.dumps(out))
        else:
            self._send(404, "{}")

    def log_message(self, *_args) -> None:  # silence request logging
        pass


def serve(port: int = 7801, open_browser: bool = True) -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
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


if __name__ == "__main__":
    serve()
