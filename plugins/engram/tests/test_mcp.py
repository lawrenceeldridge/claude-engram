"""MCP server smoke tests (bin/mcp_server.py) — driven as a subprocess over stdio.

The server speaks newline-delimited JSON-RPC 2.0. These assert the handshake surfaces
the always-present memory-first `instructions` and that tools list correctly, without
importing the module (its import runs the bootstrap re-exec).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _rpc(*requests: dict, cwd: str | None = None, env: dict | None = None) -> list[dict]:
    payload = "".join(json.dumps(r) + "\n" for r in requests)
    proc = subprocess.run(
        [sys.executable, str(ROOT / "bin" / "mcp_server.py")],
        input=payload,
        text=True,
        capture_output=True,
        timeout=60,
        cwd=cwd,
        env=env,
    )
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def _call_payload(resp: list[dict], req_id: int) -> dict:
    """Unwrap a tools/call result — the handler's dict is JSON in content[0].text."""
    by_id = {r.get("id"): r for r in resp}
    return json.loads(by_id[req_id]["result"]["content"][0]["text"])


class McpInitializeTests(unittest.TestCase):
    def test_initialize_surfaces_memory_first_instructions(self):
        resp = _rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertTrue(resp, "no response from server")
        result = resp[0]["result"]
        self.assertIn("instructions", result)
        text = result["instructions"]
        self.assertIn("recall", text)
        self.assertIn("search_code", text)
        self.assertIn("FIRST", text)

    def test_tools_list_includes_recall_and_search_code(self):
        resp = _rpc(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        by_id = {r.get("id"): r for r in resp}
        names = {t["name"] for t in by_id[2]["result"]["tools"]}
        self.assertIn("recall", names)
        self.assertIn("search_code", names)


class McpAnchorRoundTripTests(unittest.TestCase):
    """search_code emits an `anchor`; get_symbol must accept it (not only `ref`)."""

    def setUp(self):
        self.proj = tempfile.TemporaryDirectory()
        self.data = tempfile.TemporaryDirectory()
        (Path(self.proj.name) / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
        (Path(self.proj.name) / "widget.py").write_text(
            "def calculate_shipping_estimate(weight):\n"
            '    """Return the shipping estimate for a given weight."""\n'
            "    return weight * 2\n",
            encoding="utf-8",
        )
        self.env = {
            **os.environ,
            "ENGRAM_DATA_DIR": self.data.name,
            "ENGRAM_EMBEDDING": "hash",
            "ENGRAM_ENFORCE": "off",
            "ENGRAM_BUS": "inproc",
        }
        self.env.pop("ENGRAM_PYTHON", None)

    def tearDown(self):
        self.proj.cleanup()
        self.data.cleanup()

    def _run(self, get_symbol_args: dict) -> dict:
        resp = _rpc(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "index_docs", "arguments": {}}},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "search_code", "arguments": {"query": "calculate shipping estimate"}},
            },
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "get_symbol", "arguments": get_symbol_args},
            },
            cwd=self.proj.name,
            env=self.env,
        )
        return {"search": _call_payload(resp, 3), "symbol": _call_payload(resp, 4)}

    def test_search_code_anchor_round_trips_to_get_symbol(self):
        # First confirm search_code surfaces the symbol + an anchor, then feed that anchor
        # back via the `anchor` param (the field search_code emits) — the bug was that
        # get_symbol only read `ref`, so this silently returned found:false.
        search = _rpc(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "index_docs", "arguments": {}}},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "search_code", "arguments": {"query": "calculate shipping estimate"}},
            },
            cwd=self.proj.name,
            env=self.env,
        )
        results = _call_payload(search, 3).get("results", [])
        self.assertTrue(results, "search_code found no symbols")
        anchor = results[0]["anchor"]

        out = self._run({"anchor": anchor})["symbol"]
        self.assertTrue(out.get("found"), f"get_symbol(anchor={anchor!r}) did not resolve")
        self.assertIn("calculate_shipping_estimate", out.get("body", ""))

    def test_ref_param_still_works(self):
        search = self._run({"ref": "calculate_shipping_estimate"})
        self.assertTrue(search["symbol"].get("found"))
        self.assertIn("calculate_shipping_estimate", search["symbol"].get("body", ""))


if __name__ == "__main__":
    unittest.main()
