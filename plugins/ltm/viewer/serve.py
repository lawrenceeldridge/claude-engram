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
from core.recall import search_fused  # noqa: E402
from core.store import Store  # noqa: E402

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>claude-ltm</title>
<style>
  :root { color-scheme: dark;
    --bg:#0d1117; --fg:#c9d1d9; --muted:#8b949e; --card:#11161d; --border:#21262d;
    --border2:#30363d; --title:#e6edf3; --radius:10px; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,system-ui,sans-serif; }
  header { position:sticky; top:0; z-index:5; background:rgba(13,17,23,.85);
           backdrop-filter:blur(8px); border-bottom:1px solid var(--border);
           display:flex; gap:10px; align-items:center; padding:12px 18px; }
  h1 { font-size:15px; margin:0; color:#58a6ff; font-weight:700; letter-spacing:-.01em; }
  select,input { background:var(--bg); color:var(--fg); border:1px solid var(--border2);
                 border-radius:8px; padding:7px 10px; font:inherit; }
  input { flex:1; min-width:180px; }
  main { padding:22px 16px 80px; }             /* cards span the full width */
  .card { position:relative; background:var(--card); border:1px solid var(--border);
          border-radius:var(--radius); padding:16px 20px; margin-bottom:12px; transition:border-color .15s; }
  .card:hover { border-color:var(--border2); }
  .cinner { max-width:820px; }                 /* left-aligned, capped reading column */
  .chead { display:flex; align-items:center; gap:8px; margin-bottom:8px; }
  .badge { font:600 11px/1 ui-monospace,Menlo,monospace; text-transform:uppercase; letter-spacing:.05em;
           padding:4px 7px; border-radius:6px; color:#fff; background:var(--muted); }
  .badge[data-type=feature]{background:#8957e5} .badge[data-type=change]{background:#238636}
  .badge[data-type=bugfix]{background:#da3633}  .badge[data-type=refactor]{background:#1f6feb}
  .badge[data-type=decision]{background:#9e6a03} .badge[data-type=discovery]{background:#57606a}
  .badge[data-type=session_summary]{background:#bb8009} .badge[data-type=prompt]{background:#8250df}
  .toggles { margin-inline-start:auto; display:flex; gap:4px; }
  .toggle { font:11px ui-monospace,Menlo,monospace; color:var(--muted); background:transparent;
            border:1px solid var(--border2); border-radius:6px; padding:3px 8px; cursor:pointer; }
  .toggle.active { color:var(--title); border-color:#58a6ff; }
  .card .title { font-weight:650; color:var(--title); font-size:15px; margin-bottom:2px; }
  .subtitle { color:#adbac7; margin-bottom:2px; }
  .facts { margin:0; padding-inline-start:18px; }
  .facts li { margin:3px 0; }
  .narr { color:#adbac7; white-space:pre-wrap; }
  .facts, .narr { display:none; }            /* collapsed by default — title/text first */
  .facts.open, .narr.open { display:block; margin-top:12px; }  /* gap so stacked sections read clearly */
  .files { margin-top:10px; display:flex; flex-wrap:wrap; gap:5px; }
  .file { font:11px ui-monospace,Menlo,monospace; color:var(--muted); background:#161b22;
          border:1px solid var(--border); border-radius:5px; padding:2px 7px; }
  .meta { color:var(--muted); font:12px ui-monospace,Menlo,monospace; margin-top:10px; }
  .score { color:#3fb950; }
  .card[data-type=session_summary]{ border-inline-start:3px solid #bb8009; background:#15130c; }
  .card[data-type=prompt]{ border-inline-start:3px solid #8250df; background:#131022; }
  .prompt { color:#c9d1d9; white-space:pre-wrap; }
  .summary-sec { margin-top:10px; }
  .summary-sec h4 { margin:0 0 2px; font:600 12px ui-monospace,Menlo,monospace;
                    text-transform:uppercase; letter-spacing:.04em; color:#e3b341; }
  .summary-sec p { margin:0; color:#adbac7; white-space:pre-wrap; }
  .empty { color:var(--muted); padding:24px 0; }
  #live { margin-inline-start:auto; font:12px ui-monospace,Menlo,monospace; color:var(--muted);
          display:flex; align-items:center; gap:6px; }
  #live .dot { width:8px; height:8px; border-radius:50%; background:#3fb950; box-shadow:0 0 6px #3fb950; }
  #live.off .dot { background:var(--muted); box-shadow:none; }
  .card.flash { animation:flash 1.2s ease-out; }
  @keyframes flash { from { border-color:#3fb950; } to { border-color:var(--border); } }
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
const pad2 = n => String(n).padStart(2,'0');
// created_at is a UTC epoch; render it in the viewer's LOCAL time (not toISOString/UTC).
const fmtWhen = ts => { const d = new Date(ts*1000);
  return `${d.getFullYear()}-${pad2(d.getMonth()+1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`; };
const PAGE = 50;
let offset = 0, loading = false, exhausted = false, mode = 'list';
let seen = new Set();  // card keys currently rendered — used to flash only new arrivals

async function loadProjects() {
  const rows = await (await fetch('/api/projects')).json();
  const sel = $('#project');
  const prev = sel.value;
  sel.innerHTML = rows.map(r =>
    `<option value="${r.project_key}">${r.label} (${r.count})</option>`).join('');
  if (rows.some(r => r.project_key === prev)) sel.value = prev;  // keep selection across live refresh
  return rows.length;
}
function badge(c) {
  const t = c.type || (c.kind==='session_summary'?'session_summary':'discovery');
  return `<span class="badge" data-type="${esc(t)}">${esc(t.replace('_',' '))}</span>`;
}
function filesHTML(c) {
  return (c.files&&c.files.length)
    ? `<div class="files">${c.files.map(f=>`<span class="file">${esc(f)}</span>`).join('')}</div>` : '';
}
// A session summary's narrative is "Label: text" lines -> titled sections.
function summaryHTML(c) {
  const secs = (c.narrative||'').split('\\n').filter(Boolean).map(line => {
    const i = line.indexOf(':');
    const label = i>0 ? line.slice(0,i) : '';
    const body = i>0 ? line.slice(i+1).trim() : line;
    return `<div class="summary-sec"><h4>${esc(label)}</h4><p>${esc(body)}</p></div>`;
  }).join('');
  return secs || `<div class="narr">${esc(c.narrative||'')}</div>`;
}
function cardHTML(c, flash) {
  const when = fmtWhen(c.created);
  const score = c.score==null ? '' : `<span class="score">${c.score}</span> · `;
  const title = c.title ? `<div class="title">${esc(c.title)}</div>` : '';
  // Always show a lead line: the subtitle, or (for untitled/heuristic facts) the
  // first fact — so a card is never just a badge + timestamp.
  const leadText = c.subtitle || (!c.title && c.facts && c.facts[0]) || '';
  const subtitle = leadText ? `<div class="subtitle">${esc(leadText)}</div>` : '';
  const meta = `<div class="meta">${score}${esc(c.type||c.kind||'')} · ${when}</div>`;
  const cls = `card${flash?' flash':''}`;
  if (c.kind === 'prompt') {
    const text = (c.facts && c.facts[0]) ? c.facts[0] : '';
    return `<div class="${cls}" data-type="prompt"><div class="chead">${badge(c)}</div><div class="cinner"><div class="prompt">${esc(text)}</div>${meta}</div></div>`;
  }
  if (c.kind === 'session_summary') {
    return `<div class="${cls}" data-type="session_summary"><div class="chead">${badge(c)}</div><div class="cinner">${title}${summaryHTML(c)}${filesHTML(c)}${meta}</div></div>`;
  }
  const facts = `<ul class="facts">${(c.facts||[]).map(f=>`<li>${esc(f)}</li>`).join('')}</ul>`;
  const narr = c.narrative ? `<div class="narr">${esc(c.narrative)}</div>` : '';
  const toggles = `<div class="toggles"><button class="toggle" data-v="facts">facts</button>`
    + (c.narrative ? `<button class="toggle" data-v="narr">narrative</button>` : '') + `</div>`;
  return `<div class="${cls}" data-type="${esc(c.type||'discovery')}"><div class="chead">${badge(c)}${toggles}</div><div class="cinner">${title}${subtitle}${facts}${narr}${filesHTML(c)}${meta}</div></div>`;
}
async function fetchFacts(extra='') {
  const pk = $('#project').value, q = $('#q').value.trim();
  const url = `/api/facts?project=${encodeURIComponent(pk)}&q=${encodeURIComponent(q)}${extra}`;
  return await (await fetch(url)).json();
}
// Full re-render from the top: a query shows all ranked search hits; a blank query
// shows the first (newest) page of the browse list, which grows via loadMore().
async function reload(flashNew) {
  const q = $('#q').value.trim();
  mode = q ? 'search' : 'list';
  offset = 0; exhausted = false;
  const rows = q ? await fetchFacts() : await fetchFacts(`&limit=${PAGE}&offset=0`);
  if (mode === 'list') { offset = rows.length; exhausted = rows.length < PAGE; }
  const prev = seen;                        // only cards absent before flash
  seen = new Set(rows.map(r => r.key));
  $('#list').innerHTML = rows.length
    ? rows.map(r => cardHTML(r, flashNew && !prev.has(r.key))).join('')
    : '<div class="empty">No facts.</div>';
}
// Infinite scroll: append the next page of the browse list. Inert during search.
async function loadMore() {
  if (mode !== 'list' || loading || exhausted) return;
  loading = true;
  const rows = await fetchFacts(`&limit=${PAGE}&offset=${offset}`);
  offset += rows.length; exhausted = rows.length < PAGE;
  if (rows.length) {
    $('#list').insertAdjacentHTML('beforeend', rows.map(r => cardHTML(r, false)).join(''));
    rows.forEach(r => seen.add(r.key));     // paged-in cards aren't "new" on the next live update
  }
  loading = false;
}
// facts/narrative toggles — independent on/off, both collapsed by default
$('#list').addEventListener('click', e => {
  const btn = e.target.closest('.toggle'); if (!btn) return;
  const card = btn.closest('.card');
  const section = card.querySelector(btn.dataset.v === 'narr' ? '.narr' : '.facts');
  if (!section) return;
  btn.classList.toggle('active', section.classList.toggle('open'));
});
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


def _card_from_rows(rows, score=None) -> dict:
    """Build one card from an observation's fact rows (browse) or a single hit (search).

    The rows of a group share title/narrative/type/files, so the head row carries the
    card metadata; `facts` is the list of atomic texts (one element for a search hit).
    """
    head = rows[0]
    try:
        files = json.loads(head["files"]) if head["files"] else []
    except (ValueError, TypeError):
        files = []
    return {
        "key": head["observation_id"] or head["id"],
        "type": head["type"] or "",
        "title": head["title"],
        "subtitle": head["subtitle"],
        "narrative": head["narrative"],
        "files": files,
        "kind": head["kind"],
        "created": head["created_at"],
        "score": score,
        "facts": [row["text"] for row in rows],
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
                # Fused search (vector + lexical + FTS) so the box matches every
                # indexed field — text, title, subtitle, narrative and file paths —
                # not just the embedded fact text. Each hit renders as its own card.
                k = store.active_count(project_key) or 1
                hits = search_fused(store, get_embedder(cfg), project, query, cfg, k=k)
                out = [_card_from_rows([row], round(sim, 3)) for _score, sim, row in hits]
            else:
                limit = _int_param(params, "limit")
                offset = _int_param(params, "offset") or 0
                groups = store.list_observations(project_key, limit=limit, offset=offset)
                out = [_card_from_rows(rows) for rows in groups]
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
