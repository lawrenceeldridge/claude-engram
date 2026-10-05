"""LLM distiller transports — driven adapters behind the ``Distiller`` port.

``core.ports.distill.LLMDistiller`` owns every prompt and parser (pure); a backend here supplies
only ``_complete``, the one I/O step: a headless ``claude -p`` subprocess (the shipped default,
on Haiku), or an OpenAI-compatible HTTP endpoint (a local Ollama / LM Studio / llama.cpp / vLLM
server for zero-token, offline distillation). Stdlib only. Selected by
``core.ports.distill.get_distiller``; any failure falls back to the heuristic in the template.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request

from core.ports.distill import LLMDistiller


class ClaudeCliDistiller(LLMDistiller):
    """Headless ``claude -p``. Defaults to Haiku — cheap and fast for extraction."""

    def __init__(self, cmd: str = "claude", model: str = "", timeout: int = 120) -> None:
        self.cmd = cmd
        self.model = model or "haiku"
        self.timeout = timeout

    def _complete(self, prompt: str) -> str:
        # Distillation is text-in → JSON-out: the subprocess needs NO tools at all. A weak model
        # can misread the transcript embedded in the prompt as instructions and act on it (prompt
        # injection), so both tool surfaces the nested session could reach are closed here — the
        # Gateway owns its own subprocess isolation envelope. Two surfaces, two flags:
        #
        #   --strict-mcp-config : load ONLY MCP servers from --mcp-config; with none passed, the
        #     effective set is EMPTY. Without it the nested `claude -p` loads every ambient MCP
        #     server (Chrome DevTools, Linear, …) and can drive them — the built-in `--tools ""`
        #     does nothing about MCP tools (they aren't in the built-in set). Observed: the
        #     distiller navigated Chrome and opened tickets while "summarising".
        #   --tools "" : disable the entire BUILT-IN tool set (Bash/Edit/Write/…) so the model
        #     *cannot* touch the working tree. Observed: it clobbered a source file mid-edit.
        #
        # Together they leave nothing for the project's (often permissive, ~200-entry) inherited
        # settings.local.json allow-list to grant — closing availability makes permission moot.
        # This is the tool-side guard; ENGRAM_DISABLE below is the hook-side (recursion) guard.
        args = [self.cmd, "-p", "--strict-mcp-config"]
        if self.model:
            args += ["--model", self.model]
        # `--tools` is variadic (`<tools...>`) — kept LAST so it can't swallow a following flag.
        args += ["--tools", ""]
        # The nested `claude -p` is itself a Claude session that would fire engram's hooks and
        # capture this very prompt (a self-referential loop). ENGRAM_DISABLE makes those hooks
        # no-op, breaking the recursion at its root.
        env = {**os.environ, "ENGRAM_DISABLE": "1"}
        result = subprocess.run(args, input=prompt, capture_output=True, text=True, timeout=self.timeout, env=env)
        if result.returncode != 0:
            raise RuntimeError((result.stderr or "llm error")[:200])
        return result.stdout


class HTTPDistiller(LLMDistiller):
    """Any OpenAI-compatible chat endpoint (Ollama / LM Studio / llama.cpp / vLLM).

    With a local server this is zero-token and fully offline. Stdlib-only.
    """

    def __init__(self, base_url: str, model: str, api_key: str = "", timeout: int = 120) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    def _complete(self, prompt: str) -> str:
        body = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": "You extract long-term memory. Output only a JSON object."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0,
                "stream": False,
                # Guarantees syntactically valid JSON, so a stray token can't drop the
                # whole capture to the heuristic fallback. Honoured by Ollama/vLLM/LM Studio.
                "response_format": {"type": "json_object"},
            }
        ).encode()
        request = urllib.request.Request(f"{self.base_url}/chat/completions", data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        if self.api_key:
            request.add_header("Authorization", f"Bearer {self.api_key}")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            data = json.loads(response.read().decode())
        return data["choices"][0]["message"]["content"]
