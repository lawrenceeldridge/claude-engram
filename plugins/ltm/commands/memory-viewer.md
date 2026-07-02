---
description: Launch the claude-ltm localhost viewer to browse stored memory across projects
---

Run the claude-ltm viewer so the user can browse and search their long-term
memory in a browser.

Execute this command and report the URL to the user:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/bin/ltm" viewer
```

The viewer serves a read-only UI at http://127.0.0.1:7801/ listing every project
in the global store, with semantic search within a project. It is safe to run
alongside a live session. Pass `--port N` to change the port or `--no-open` to
skip opening a browser.
