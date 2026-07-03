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
from core.index.index_recall import get_chunk, get_outline, search_index  # noqa: E402
from core.ports.embedding import get_embedder  # noqa: E402
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
  #status { margin-inline-start:auto; display:flex; align-items:center; gap:14px; }
  #live { font:12px ui-monospace,Menlo,monospace; color:var(--muted); display:flex; align-items:center; gap:6px; }
  #live .dot { width:8px; height:8px; border-radius:50%; background:#3fb950; box-shadow:0 0 6px #3fb950; }
  #live.off .dot { background:var(--muted); box-shadow:none; }
  .svc { font:11px ui-monospace,Menlo,monospace; color:var(--muted); display:flex; align-items:center; gap:5px; cursor:default; }
  .svc b { color:#adbac7; font-weight:600; }
  .svc .d { width:7px; height:7px; border-radius:50%; background:var(--muted); }
  .svc.ok .d { background:#3fb950; box-shadow:0 0 5px #3fb950; }
  .svc.warn .d { background:#e3b341; box-shadow:0 0 5px #e3b341; }
  .svc.warn b { color:#e3b341; }
  #ledger { font:11px ui-monospace,Menlo,monospace; color:var(--muted); display:flex; align-items:center; gap:5px; cursor:default; white-space:pre; }
  #ledger b { color:#3fb950; font-weight:600; }
  #ledger.neg b { color:#e3b341; }
  .card.flash { animation:flash 1.2s ease-out; }
  @keyframes flash { from { border-color:#3fb950; } to { border-color:var(--border); } }
  .vtoggle { font:11px ui-monospace,Menlo,monospace; color:var(--muted); background:transparent;
             border:1px solid var(--border2); border-radius:6px; padding:5px 10px; cursor:pointer; }
  .vtoggle.active { color:var(--title); border-color:#58a6ff; background:#0f1b2d; }
  #views { display:flex; gap:4px; }
  .badge[data-type=code_symbol]{background:#1f6feb} .badge[data-type=doc_section]{background:#238636}
  .fresh-pill { font:600 10px/1 ui-monospace,Menlo,monospace; text-transform:uppercase; letter-spacing:.04em;
                padding:3px 6px; border-radius:5px; margin-inline-start:6px; }
  .fresh-pill[data-f=fresh]{color:#3fb950;border:1px solid #238636} .fresh-pill[data-f=edited]{color:#e3b341;border:1px solid #9e6a03}
  .fresh-pill[data-f=stale],.fresh-pill[data-f=gone]{color:#f85149;border:1px solid #da3633}
  .path { font:11px ui-monospace,Menlo,monospace; color:var(--muted); margin-top:8px; word-break:break-all; }
  .cbody { display:none; margin-top:12px; background:#0b0f14; border:1px solid var(--border);
           border-radius:8px; padding:12px 14px; overflow:auto; max-height:520px; }
  .cbody.open { display:block; }
  .cbody pre { margin:0; white-space:pre-wrap; font:12px/1.5 ui-monospace,Menlo,monospace; color:#c9d1d9; }
  .card.ix { cursor:pointer; }
  .sec { margin:24px 0 10px; font:600 12px ui-monospace,Menlo,monospace; text-transform:uppercase;
         letter-spacing:.06em; color:var(--muted); }
  .sec:first-child { margin-top:4px; }
  .qstatus { font:600 10px/1 ui-monospace,Menlo,monospace; text-transform:uppercase; padding:3px 6px;
             border-radius:5px; margin-inline-start:6px; }
  .qs-pending{color:#e3b341;border:1px solid #9e6a03} .qs-in_progress{color:#58a6ff;border:1px solid #1f6feb}
  .qs-dead{color:#f85149;border:1px solid #da3633}
  .qmeta { margin-inline-start:auto; font:11px ui-monospace,Menlo,monospace; color:var(--muted); }
  .spill { font:600 10px/1 ui-monospace,Menlo,monospace; text-transform:uppercase; padding:3px 6px;
           border-radius:5px; margin-inline-start:6px; color:#f85149; border:1px solid #da3633; }
</style></head>
<body>
<header>
  <h1>claude-ltm</h1>
  <div id="views">
    <button class="vtoggle active" data-view="stm" title="Short-term memory (fresh, awaiting rehearsal)">stm</button>
    <button class="vtoggle" data-view="ltm" title="Long-term memory (consolidated)">ltm</button>
    <button class="vtoggle" data-view="rnr" title="Refine &amp; rescue: work queue + archived/forgotten facts">rnr</button>
    <button class="vtoggle" data-view="index" title="Code &amp; docs index">index</button>
  </div>
  <select id="project"></select>
  <select id="kind" style="display:none">
    <option value="">all</option>
    <option value="doc_section">docs</option>
    <option value="code_symbol">code</option>
  </select>
  <input id="q" placeholder="semantic search within project… (blank = list all)">
  <div id="status">
    <span id="ledger" title="token-savings ledger">saved <b>…</b></span>
    <span class="svc" id="svc-bus">bus <b>…</b><span class="d"></span></span>
    <span class="svc" id="svc-emb">emb <b>…</b><span class="d"></span></span>
    <span class="svc" id="svc-dist">dist <b>…</b><span class="d"></span></span>
    <span id="live" class="off"><span class="dot"></span><span id="live-label">connecting…</span></span>
  </div>
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
let view = 'stm';   // stm|ltm = active facts by tier · rnr = queue + archived · index = code/docs
let seen = new Set();  // card keys currently rendered — used to flash only new arrivals

async function loadProjects() {
  const sel = $('#project');
  const prev = sel.value;
  let prevLabel = sel.selectedOptions[0]?.textContent || prev;   // strip the trailing " (count)"
  const paren = prevLabel.lastIndexOf(' (');
  if (paren > -1) prevLabel = prevLabel.slice(0, paren);
  const rows = await (await fetch(view === 'index' ? '/api/index_projects' : '/api/projects')).json();
  // Pin the selected project across tab switches even when this view has no data for
  // it yet (e.g. a project with memory but no index) — otherwise the dropdown would
  // silently jump to the first project. The empty view then shows an empty state.
  if (prev && !rows.some(r => r.project_key === prev))
    rows.push({ project_key: prev, label: prevLabel, count: 0 });
  sel.innerHTML = rows.map(r =>
    `<option value="${r.project_key}">${r.label} (${r.count})</option>`).join('');
  if (rows.some(r => r.project_key === prev)) sel.value = prev;  // keep selection across live refresh / tab switch
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
  const spill = (c.status && c.status !== 'active') ? `<span class="spill">${esc(c.status)}</span>` : '';
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
    return `<div class="${cls}" data-type="prompt"><div class="chead">${badge(c)}${spill}</div><div class="cinner"><div class="prompt">${esc(text)}</div>${meta}</div></div>`;
  }
  if (c.kind === 'session_summary') {
    return `<div class="${cls}" data-type="session_summary"><div class="chead">${badge(c)}${spill}</div><div class="cinner">${title}${summaryHTML(c)}${filesHTML(c)}${meta}</div></div>`;
  }
  const facts = `<ul class="facts">${(c.facts||[]).map(f=>`<li>${esc(f)}</li>`).join('')}</ul>`;
  const narr = c.narrative ? `<div class="narr">${esc(c.narrative)}</div>` : '';
  const toggles = `<div class="toggles"><button class="toggle" data-v="facts">facts</button>`
    + (c.narrative ? `<button class="toggle" data-v="narr">narrative</button>` : '') + `</div>`;
  return `<div class="${cls}" data-type="${esc(c.type||'discovery')}"><div class="chead">${badge(c)}${spill}${toggles}</div><div class="cinner">${title}${subtitle}${facts}${narr}${filesHTML(c)}${meta}</div></div>`;
}
async function fetchFacts(extra='') {
  const pk = $('#project').value, q = $('#q').value.trim();
  const tier = (view === 'stm' || view === 'ltm') ? `&tier=${view}` : '';
  const url = `/api/facts?project=${encodeURIComponent(pk)}&q=${encodeURIComponent(q)}${tier}${extra}`;
  return await (await fetch(url)).json();
}
// One indexed chunk: kind badge + freshness pill, heading/qualname title, summary,
// source path. The card is click-to-expand — the body is fetched lazily from /api/chunk.
function indexCardHTML(c) {
  const kind = c.kind || 'doc_section';
  const fresh = c.freshness ? `<span class="fresh-pill" data-f="${esc(c.freshness)}">${esc(c.freshness)}</span>` : '';
  const score = c.score==null ? '' : `<span class="score">${c.score}</span> · `;
  const badge = `<span class="badge" data-type="${esc(kind)}">${kind==='code_symbol'?'code':'doc'}</span>`;
  const title = `<div class="title">${esc(c.heading_path || c.title || c.anchor)}</div>`;
  const summary = c.summary ? `<div class="subtitle">${esc(c.summary)}</div>` : '';
  const meta = `<div class="path">${score}${esc(c.source_path||'')} · ${esc(c.anchor||'')}</div>`;
  return `<div class="card ix" data-ref="${esc(c.anchor)}"><div class="chead">${badge}${fresh}</div>`
    + `<div class="cinner">${title}${summary}${meta}<div class="cbody"></div></div></div>`;
}
async function reloadIndex() {
  mode = 'search'; exhausted = true;   // index view has no infinite-scroll
  const pk = $('#project').value, q = $('#q').value.trim(), kind = $('#kind').value;
  const url = `/api/index?project=${encodeURIComponent(pk)}&q=${encodeURIComponent(q)}&kind=${encodeURIComponent(kind)}`;
  const rows = await (await fetch(url)).json();
  seen = new Set();
  const what = kind==='code_symbol' ? 'code symbols' : kind==='doc_section' ? 'doc sections' : 'indexed chunks';
  $('#list').innerHTML = rows.length ? rows.map(indexCardHTML).join('')
    : `<div class="empty">No ${what}. Run the index_docs tool for this project.</div>`;
}
// One durable work-queue item (rescue re-distill backlog / dead-letter).
function qItemHTML(q) {
  const when = fmtWhen(q.enqueued||0);
  const body = q.payload || q.ref || '';
  return `<div class="card"><div class="chead"><span class="badge" data-type="discovery">${esc(q.stage)}</span>`
    + `<span class="qstatus qs-${esc(q.status)}">${esc(q.status)}</span>`
    + `<span class="qmeta">delivery ${q.attempts} · ${when}</span></div>`
    + `<div class="cinner"><div class="prompt">${esc(body)}</div></div></div>`;
}
// Refine & Rescue: the durable queue (rescue backlog + dead-letter) and the facts
// consolidation has archived (superseded / displaced / pruned / expired).
async function reloadRnr() {
  mode = 'search'; exhausted = true;   // no infinite scroll
  const pk = $('#project').value;
  const r = await (await fetch(`/api/rnr?project=${encodeURIComponent(pk)}`)).json();
  seen = new Set();
  const queue = r.queue || [], archived = r.archived || [];
  const qHTML = queue.length ? queue.map(qItemHTML).join('')
    : `<div class="empty">Queue empty — nothing awaiting re-distill or dead-lettered.</div>`;
  const aHTML = archived.length ? archived.map(c => cardHTML(c, false)).join('')
    : `<div class="empty">Nothing archived yet — refine/supersession/expiry haven't retired any facts.</div>`;
  $('#list').innerHTML =
    `<h3 class="sec">Rescue queue · ${queue.length}</h3>${qHTML}`
    + `<h3 class="sec">Archived / forgotten · ${archived.length}</h3>${aHTML}`;
}
// Full re-render from the top: a query shows all ranked search hits; a blank query
// shows the first (newest) page of the browse list, which grows via loadMore().
async function reload(flashNew) {
  loadLedger();  // token-savings ledger for the selected project (all views)
  if (view === 'index') return reloadIndex();
  if (view === 'rnr') return reloadRnr();
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
// facts/narrative toggles (memory), and click-to-expand a chunk's body (index).
$('#list').addEventListener('click', async e => {
  const btn = e.target.closest('.toggle');
  if (btn) {
    const section = btn.closest('.card').querySelector(btn.dataset.v === 'narr' ? '.narr' : '.facts');
    if (section) btn.classList.toggle('active', section.classList.toggle('open'));
    return;
  }
  const ix = e.target.closest('.card.ix'); if (!ix) return;
  const body = ix.querySelector('.cbody');
  if (body.classList.toggle('open') && !body.dataset.loaded) {
    const pk = $('#project').value, ref = ix.dataset.ref;
    const r = await (await fetch(`/api/chunk?project=${encodeURIComponent(pk)}&ref=${encodeURIComponent(ref)}`)).json();
    body.innerHTML = r.found ? `<pre>${esc(r.body)}</pre>` : '<pre>(section not found)</pre>';
    body.dataset.loaded = '1';
  }
});
$('#views').addEventListener('click', async e => {
  const b = e.target.closest('.vtoggle'); if (!b) return;
  view = b.dataset.view;
  document.querySelectorAll('.vtoggle').forEach(x => x.classList.toggle('active', x === b));
  $('#kind').style.display = view === 'index' ? '' : 'none';
  $('#q').style.display = view === 'rnr' ? 'none' : '';  // RnR is browse-only
  $('#q').value = '';
  $('#q').placeholder = view === 'index'
    ? 'search indexed code / docs… (blank = list)' : 'semantic search within project… (blank = list all)';
  await loadProjects();
  await reload();
});
$('#kind').addEventListener('change', () => reload());
$('#project').addEventListener('change', () => reload());
let t; $('#q').addEventListener('input', () => { clearTimeout(t); t=setTimeout(() => reload(),180); });
window.addEventListener('scroll', () => {
  if (window.innerHeight + window.scrollY >= document.body.offsetHeight - 400) loadMore();
});

// Service health: the configured bus / embedding / distiller backends and whether
// each is reachable (green = configured backend live, amber = on stdlib fallback).
function svcChip(el, name, s) {
  if (!el || !s) return;
  el.className = 'svc ' + (s.state || 'warn');
  el.title = name + ': ' + s.backend + ' — ' + s.detail;
  el.innerHTML = name + ' <b>' + esc(s.backend) + '</b><span class="d"></span>';
}
async function loadHealth() {
  try {
    const h = await (await fetch('/api/health')).json();
    svcChip($('#svc-bus'), 'bus', h.bus);
    svcChip($('#svc-emb'), 'emb', h.embedding);
    svcChip($('#svc-dist'), 'dist', h.distiller);
  } catch (e) { /* fail-open: leave the last-known chips */ }
}

// Token-savings ledger for the selected project: net = saved (targeted reads + recall
// shortcuts) - cost (bytes injected). Hover for the full breakdown.
function fmtTok(n) {
  const a = Math.abs(n);
  return a >= 1000 ? (n / 1000).toFixed(a >= 10000 ? 0 : 1).replace(/\\.0$/, '') + 'k' : String(n);
}
async function loadLedger() {
  const el = $('#ledger'); if (!el) return;
  try {
    const pk = $('#project').value;
    const s = await (await fetch('/api/stats?project=' + encodeURIComponent(pk || ''))).json();
    el.classList.toggle('neg', s.net_tokens < 0);
    el.innerHTML = 'saved <b>~' + fmtTok(s.net_tokens) + '</b> tok';
    el.title = 'net ~' + s.net_tokens.toLocaleString() + ' tokens (saved − cost)\\n'
      + '  cost injected:   ~' + s.cost_tokens.toLocaleString() + ' (' + s.injections + ' injections)\\n'
      + '  saved measured:  ~' + s.saved_measured_tokens.toLocaleString() + ' (' + s.targeted_reads + ' targeted reads)\\n'
      + '  saved estimated: ~' + s.saved_estimated_tokens.toLocaleString() + ' (' + s.ok_recalls + ' recall shortcuts)';
  } catch (e) { /* fail-open: leave the last-known value */ }
}

// Live updates: the server pushes a `change` event whenever the memory store is
// written to (capture from any session). EventSource auto-reconnects on drop.
function connectStream() {
  const es = new EventSource('/events');
  const badge = $('#live'), label = $('#live-label');
  es.onopen = () => { badge.classList.remove('off'); label.textContent = 'live'; };
  es.addEventListener('change', async () => {
    await loadProjects();      // refresh counts + keep current project selected
    if (view !== 'index') await reload(true);  // stm/ltm/rnr refresh live; avoid churn during index build
    loadHealth();              // a write may mean the distiller/bus just came up
    loadLedger();              // a capture / pull may have shifted the token ledger
  });
  es.onerror = () => { badge.classList.add('off'); label.textContent = 'reconnecting…'; };
}
(async () => {
  const n = await loadProjects();
  if (n) await reload();
  else $('#list').textContent = 'No memory captured yet.';
  loadHealth();
  loadLedger();
  setInterval(loadHealth, 20000);  // reachability can change (nats/distiller up or down)
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


def _disambiguate_labels(items: list[dict]) -> list[dict]:
    """Make dropdown labels unique when project basenames collide.

    Two distinct projects can share a basename (e.g. ``…/sak-replicate/backend`` and
    ``…/sak-assistant/backend`` both label ``backend``). The keys are unique, but the
    label alone is ambiguous — so prefix the parent directory (``sak-replicate/backend``)
    for any colliding label. Display-only; project keys are never touched.
    """
    from collections import Counter

    counts = Counter(it["label"] for it in items)
    for it in items:
        if counts[it["label"]] > 1 and it.get("path"):
            parent = os.path.basename(os.path.dirname(it["path"]))
            if parent:
                it["label"] = f"{parent}/{it['label']}"
    return items


def _tcp_ok(url: str, timeout: float = 0.6) -> bool:
    """Best-effort TCP reachability for a host:port URL (nats://, http://, https://).

    Fail-open: any parse or socket error means "unreachable", never an exception.
    """
    try:
        u = urlparse(url)
        host = u.hostname
        port = u.port or {"https": 443, "http": 80, "nats": 4222}.get(u.scheme, 0)
        if not host or not port:
            return False
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _service_health(cfg) -> dict:
    """Resolve the configured backends and cheaply probe reachability for the header.

    Everything fails open, so a subsystem is reported ``ok`` (configured backend live)
    or ``warn`` (configured backend unavailable — running on the stdlib fallback), never
    a hard error. Read-only and off the recall hot path (viewer only).
    """
    # MemoryBus — nats probed by reachability; inproc is always available.
    if cfg.bus == "nats":
        ok = _tcp_ok(cfg.nats_url)
        bus = {
            "backend": "nats",
            "state": "ok" if ok else "warn",
            "detail": cfg.nats_url if ok else f"{cfg.nats_url} unreachable — falling open to inproc",
        }
    else:
        bus = {"backend": "inproc", "state": "ok", "detail": "sqlite work_queue"}

    # Embedding — fastembed needs its provisioned venv; hash is the stdlib default.
    if cfg.embedding == "fastembed":
        try:
            from core.provision import is_provisioned

            prov = is_provisioned(cfg.data_dir)
        except Exception:
            prov = False
        bge = cfg.embedding_model or "bge-base"
        embedding = {
            "backend": "fastembed",
            "state": "ok" if prov else "warn",
            "detail": bge if prov else "venv not provisioned — falling open to hash",
        }
    else:
        embedding = {"backend": "hash", "state": "ok", "detail": "lexical (stdlib)"}

    # Distiller — an LLM backend is probed at its base URL; heuristic is stdlib.
    if cfg.distiller == "heuristic":
        distiller = {"backend": "heuristic", "state": "ok", "detail": "line extraction (stdlib)"}
    else:
        ok = _tcp_ok(cfg.distiller_base_url)
        label = cfg.distiller + (f" · {cfg.distiller_model}" if cfg.distiller_model else "")
        host = urlparse(cfg.distiller_base_url).netloc or cfg.distiller_base_url
        distiller = {
            "backend": label,
            "state": "ok" if ok else "warn",
            "detail": host if ok else f"{host} unreachable — falling open to heuristic",
        }

    return {"bus": bus, "embedding": embedding, "distiller": distiller}


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
        "tier": (head["tier"] if "tier" in head.keys() else None),
        "status": (head["status"] if "status" in head.keys() else None),
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
        elif parsed.path == "/api/health":
            self._send(200, json.dumps(_service_health(cfg)))
        elif parsed.path == "/api/stats":
            from core.service import usage_summary

            project_key = parse_qs(parsed.query).get("project", [""])[0] or None
            store = Store(cfg.db_path)
            self._send(200, json.dumps(usage_summary(store, project_key)))
            store.close()
        elif parsed.path == "/api/projects":
            store = Store(cfg.db_path)
            out = _disambiguate_labels(
                [
                    {
                        "project_key": r["project_key"],
                        "label": r["project_label"],
                        "path": r["project_path"],
                        "count": r["c"],
                    }
                    for r in store.projects()
                ]
            )
            store.close()
            self._send(200, json.dumps(out))
        elif parsed.path == "/api/facts":
            params = parse_qs(parsed.query)
            project_key = params.get("project", [""])[0]
            query = params.get("q", [""])[0].strip()
            tier = params.get("tier", [""])[0] or None  # 'stm' / 'ltm' tabs; None = both
            store = Store(cfg.db_path)
            if query and project_key:
                project = {"key": project_key, "path": "", "label": ""}
                # Fused search (vector + lexical + FTS) so the box matches every
                # indexed field — text, title, subtitle, narrative and file paths —
                # not just the embedded fact text. Each hit renders as its own card.
                k = store.active_count(project_key) or 1
                hits = search_fused(store, get_embedder(cfg), project, query, cfg, k=k)
                out = [_card_from_rows([row], round(sim, 3)) for _score, sim, row in hits]
                if tier:
                    out = [c for c in out if c.get("tier") == tier]  # keep this tab's tier
            else:
                limit = _int_param(params, "limit")
                offset = _int_param(params, "offset") or 0
                groups = store.list_observations(project_key, limit=limit, offset=offset, tier=tier, active=True)
                out = [_card_from_rows(rows) for rows in groups]
            store.close()
            self._send(200, json.dumps(out))
        elif parsed.path == "/api/rnr":
            # Refine & Rescue view: the durable work queue + archived ("forgotten") facts.
            params = parse_qs(parsed.query)
            project_key = params.get("project", [""])[0]
            store = Store(cfg.db_path)
            archived = [_card_from_rows(rows) for rows in store.list_observations(project_key, active=False)]
            queue = [
                {
                    "stage": r["stage"],
                    "status": r["status"],
                    "attempts": r["attempts"],
                    "ref": r["ref"],
                    "payload": (r["payload"] or "")[:240],
                    "enqueued": r["enqueued_at"],
                }
                for r in store.work_items(project_key)
            ]
            store.close()
            self._send(200, json.dumps({"archived": archived, "queue": queue}))
        elif parsed.path == "/api/index_projects":
            store = Store(cfg.db_path)
            # Prefer the memory (facts) label/path; fall back to the label recorded at
            # index time (index_meta), so an index-only project shows a real name and
            # never its raw key.
            labels = {r["project_key"]: (r["project_label"], r["project_path"]) for r in store.projects()}
            out = _disambiguate_labels(
                [
                    {
                        "project_key": r["project_key"],
                        "label": (labels.get(r["project_key"]) or (None, None))[0] or r["label"] or r["project_key"],
                        "path": (labels.get(r["project_key"]) or (None, None))[1] or r["path"] or "",
                        "files": r["files"],
                        "count": r["c"],
                    }
                    for r in store.chunk_projects()
                ]
            )
            store.close()
            self._send(200, json.dumps(out))
        elif parsed.path == "/api/index":
            params = parse_qs(parsed.query)
            project_key = params.get("project", [""])[0]
            query = params.get("q", [""])[0].strip()
            kind = params.get("kind", [""])[0] or None
            store = Store(cfg.db_path)
            project = self._index_project(store, project_key)
            if query:
                res = search_index(store, get_embedder(cfg), cfg, project, query, k=200, kind=kind)
                out = res["results"]
            else:
                out = get_outline(store, project, kind=kind)["sections"][:300]
            store.close()
            self._send(200, json.dumps(out))
        elif parsed.path == "/api/chunk":
            params = parse_qs(parsed.query)
            project_key = params.get("project", [""])[0]
            ref = params.get("ref", [""])[0]
            store = Store(cfg.db_path)
            out = get_chunk(store, self._index_project(store, project_key), ref)
            store.close()
            self._send(200, json.dumps(out))
        else:
            self._send(404, "{}")

    @staticmethod
    def _index_project(store, project_key: str) -> dict:
        """Resolve a project dict (key/path/label) for index queries; path drives freshness."""
        for row in store.projects():
            if row["project_key"] == project_key:
                return {"key": project_key, "path": row["project_path"] or "", "label": row["project_label"] or ""}
        return {"key": project_key, "path": "", "label": ""}

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
