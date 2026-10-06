"""Capture re-entrancy guard (rescue-queue-hardening Phase 1).

The `claude` distiller runs headless `claude -p` inside the capture worker; that nested
Claude session must not fire engram's hooks and capture the distiller prompt as a session
(the loop that stuffed the rescue queue with prompt-template payloads). Verifies:
  - `ENGRAM_DISABLE` gates the shared `hooks_disabled()` helper,
  - each hook entry point exits 0 (no side effect) when it is set,
  - `ClaudeCliDistiller` sets it in the headless subprocess env,
  - capture drops a transcript that is itself a distiller prompt (defensive backstop).
Stdlib unittest, hash embedder, no network.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from dataclasses import replace
from unittest import mock

from _bootstrap import hooks_disabled
from _harness import ROOT, scoped_env, temp_data_dir

from core import service
from core.adapters import llm_distillers
from core.adapters.llm_distillers import ClaudeCliDistiller
from core.config import get_config
from core.ports import distill
from core.ports.distill import is_distiller_prompt
from core.ports.embedding import HashEmbedding
from core.store import Store

_HOOKS = [
    "capture.py",
    "recall_session_start.py",
    "recall_prompt.py",
    "index_docs.py",
    "prefer_memory.py",
    "mark_consulted.py",
    "index_edit.py",
    "credit_read.py",
]


class HooksDisabledHelperTests(unittest.TestCase):
    def test_off_by_default(self):
        self.assertFalse(hooks_disabled())  # the harness clears any ambient ENGRAM_DISABLE

    def test_on_when_set(self):
        scoped_env(self, ENGRAM_DISABLE="1")
        self.assertTrue(hooks_disabled())


class HookNoOpTests(unittest.TestCase):
    """Every hook exits 0 and does nothing when ENGRAM_DISABLE=1 (driven as a subprocess)."""

    def test_all_hooks_noop_when_disabled(self):
        env = {**os.environ, "ENGRAM_DISABLE": "1"}
        for hook in _HOOKS:
            proc = subprocess.run(
                [sys.executable, str(ROOT / "bin" / hook)],
                input="{}",  # a hook that reads stdin gets empty JSON; the guard fires first anyway
                text=True,
                capture_output=True,
                timeout=30,
                env=env,
            )
            self.assertEqual(proc.returncode, 0, f"{hook} did not exit 0: {proc.stderr[:200]}")
            self.assertEqual(proc.stdout, "", f"{hook} emitted output while disabled: {proc.stdout[:200]}")


class DistillerEnvTests(unittest.TestCase):
    """The distiller subprocess must be spawned inside a tight isolation envelope.

    All three tests inspect the exact ``subprocess.run`` call the Gateway makes, so they
    share one helper that patches ``subprocess.run`` and returns the captured ``args`` / ``kwargs``.
    """

    def _spawn_args(self, **distiller_kwargs) -> tuple[list, dict]:
        """Invoke ``ClaudeCliDistiller._complete`` with ``subprocess.run`` stubbed.

        Returns the ``(args, kwargs)`` the Gateway would have passed to the real subprocess.
        """
        captured: dict = {}

        class _Result:
            returncode = 0
            stdout = "{}"
            stderr = ""

        def fake_run(args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return _Result()

        with mock.patch.object(llm_distillers.subprocess, "run", fake_run):
            ClaudeCliDistiller(**distiller_kwargs)._complete("some prompt")
        return captured["args"], captured["kwargs"]

    def test_claude_distiller_sets_disable_env(self):
        _, kwargs = self._spawn_args(cmd="claude")
        env = kwargs.get("env")
        self.assertIsNotNone(env, "distiller must pass an explicit env")
        self.assertEqual(env.get("ENGRAM_DISABLE"), "1")

    def test_claude_distiller_runs_with_no_tools(self):
        # Regression: the headless distiller must be spawned tool-less (`--tools ""`) so a
        # confused model can't write to the working tree via the project's allow-list
        # (e.g. `Bash(cat > *)`). This is the tool-side guard; ENGRAM_DISABLE is the hook-side one.
        args, _ = self._spawn_args(cmd="claude", model="haiku")
        self.assertIn("--tools", args)
        self.assertEqual(args[args.index("--tools") + 1], "", "--tools value must be '' (all tools disabled)")
        joined = " ".join(args)
        for banned in ("Edit", "Write", "Bash", "NotebookEdit"):
            self.assertNotIn(banned, joined, f"distiller must not be granted the {banned} tool")

    def test_claude_distiller_isolates_mcp(self):
        # Regression: `--tools ""` disables only the BUILT-IN tool set — it does NOT stop the
        # nested `claude -p` loading ambient MCP servers (Chrome DevTools, Linear, plugin MCP
        # servers, …), which are then auto-permitted by the inherited allow-list and can perform
        # side-effecting "ghost actions". `--strict-mcp-config` (with NO `--mcp-config`) loads
        # zero MCP servers — verified behaviourally: without it an engram `recall` MCP tool call
        # executes; with it the MCP server never starts. See docs/generated/plans/plan-distiller-mcp-sandbox.md.
        args, _ = self._spawn_args(cmd="claude", model="haiku")
        self.assertIn("--strict-mcp-config", args, "distiller must load ZERO MCP servers")
        # No --mcp-config source → the strict set is empty (that is the whole point).
        self.assertNotIn("--mcp-config", args, "no MCP config source may be passed — the strict set must stay empty")
        # No MCP tool may be granted by name either.
        self.assertNotIn("mcp__", " ".join(args), "distiller must not be granted any mcp__ tool")
        # Ordering guard: the variadic `--tools <tools...>` must stay LAST so it can't swallow
        # `--strict-mcp-config`. Assert strict appears before --tools.
        self.assertLess(
            args.index("--strict-mcp-config"),
            args.index("--tools"),
            "--strict-mcp-config must precede the variadic --tools so it isn't swallowed",
        )


class DistillerPromptBackstopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_data_dir(self)
        self.cfg = replace(get_config(), distiller="heuristic")
        self.store = Store(self.cfg.db_path)
        self.embedder = HashEmbedding(dim=self.cfg.dim)
        self.project = {"key": "p", "path": "/tmp/p", "label": "p"}

    def tearDown(self):
        self.store.close()

    def test_is_distiller_prompt(self):
        self.assertTrue(is_distiller_prompt("You extract durable long-term memory from a coding assistant session."))
        self.assertTrue(is_distiller_prompt("Summarise this coding-assistant session as one durable memory."))
        self.assertTrue(is_distiller_prompt("You are consolidating long-term memory for a coding assistant."))
        self.assertFalse(is_distiller_prompt("The deploy target is AWS Lambda."))

    def test_every_prompt_the_llm_distillers_send_is_recognised(self):
        # Derived from the prompts, so a new or reworded one can't slip past the backstop (the
        # review prompt once did, while the prefixes were a hand-kept copy).
        sent = [
            distill._build_prompt("Edited auth.py", [("id1", "an existing fact")]),
            distill._build_summary_prompt("a session"),
            distill._build_merge_prompt(["fact a", "fact b"]),
            distill._build_antipattern_prompt("a session", []),
            distill._build_review_prompt([("id1", "a fact")], "context"),
        ]
        for prompt in sent:
            self.assertTrue(is_distiller_prompt(prompt), prompt[:60])

    def test_capture_text_skips_a_distiller_prompt(self):
        n = service.capture_text(
            self.store,
            self.embedder,
            self.cfg,
            self.project,
            "s1",
            "You extract durable long-term memory from a coding assistant session.\n\nGroup related facts…",
        )
        self.assertEqual(n, 0)
        self.assertEqual(len(self.store.active_rows_for_project(self.project["key"])), 0)

    def test_capture_text_still_stores_a_real_delta(self):
        n = service.capture_text(
            self.store, self.embedder, self.cfg, self.project, "s1", "The deploy target is fly.io."
        )
        self.assertGreaterEqual(n, 1)

    def test_capture_text_drops_ephemeral_status_lines(self):
        # CI/build status lines are transient, not durable facts — the heuristic now skips them.
        n = service.capture_text(
            self.store,
            self.embedder,
            self.cfg,
            self.project,
            "s1",
            "TypeScript compilation passed\nproduction build succeeded\nworking tree clean",
        )
        self.assertEqual(n, 0)
        self.assertEqual(len(self.store.active_rows_for_project(self.project["key"])), 0)

    def _prompt_texts(self):
        return [r["text"] for r in self.store.active_rows_for_project(self.project["key"]) if r["kind"] == "prompt"]

    def test_capture_prompts_drops_trivial_keeps_substantive(self):
        service.capture_prompts(
            self.store,
            self.embedder,
            self.cfg,
            self.project,
            "s1",
            [
                "yes lets commit",
                "Apply all",
                "Option C",
                "/engram-git commit",
                "Paginate the consolidation panel please.",
            ],
        )
        kept = self._prompt_texts()
        self.assertEqual(kept, ["Paginate the consolidation panel please."])  # only the substantive one

    def test_capture_prompts_gate_disabled_at_zero(self):
        cfg = replace(self.cfg, ingest_min_prompt_len=0)
        service.capture_prompts(self.store, self.embedder, cfg, self.project, "s1", ["Option C"])
        self.assertEqual(self._prompt_texts(), ["Option C"])  # gate off → captured verbatim

    def test_capture_prompts_fails_open_on_policy_error(self):
        # A policy bug must never make capture drop a prompt (fail-open contract).
        def boom(*_a, **_k):
            raise RuntimeError("policy exploded")

        with mock.patch.object(service, "is_trivial_prompt", boom):
            service.capture_prompts(self.store, self.embedder, self.cfg, self.project, "s1", ["Option C"])
        self.assertEqual(self._prompt_texts(), ["Option C"])  # kept despite the raising predicate


if __name__ == "__main__":
    unittest.main()
