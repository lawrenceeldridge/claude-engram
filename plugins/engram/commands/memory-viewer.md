---
description: Launch the claude-engram localhost viewer to browse stored memory across projects
---

Run the claude-engram viewer so the user can browse and search their long-term
memory in a browser.

Execute this command and report the URL to the user:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/bin/engram" viewer --ensure
```

`--ensure` starts the viewer as a **detached background process** (if not already
running) and opens the browser, then returns immediately — the server keeps
running after this session ends, so the URL stays live across sessions. It is
idempotent: only one viewer instance runs regardless of how many sessions are
open. The `SessionStart` hook also runs `--ensure` automatically, so the viewer
is typically already up.

The viewer serves a read-only UI at http://127.0.0.1:7801/ listing every project
in the global store, with semantic search within a project, and **live updates**
via Server-Sent Events — new memory appears without a refresh. It is safe to run
alongside a live session.

- `--port N` — change the port (default 7801)
- `--no-open` — don't open a browser
- `--stop` — stop the resident viewer

Auto-start can be disabled with the `viewer_autostart` plugin option (or
`ENGRAM_VIEWER_AUTOSTART=0`).
