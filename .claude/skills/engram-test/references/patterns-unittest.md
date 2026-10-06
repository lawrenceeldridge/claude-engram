# Unittest Patterns

Copy-paste `unittest` patterns per code type in claude-engram. Apply with the AAA
structure and fixture hygiene from [`standards.md`](standards.md). All patterns are
stdlib-only and mirror the real suite under `plugins/engram/tests/`.

Every test module starts the same way — put the package root on `sys.path`, then
import from `core`:

```python
from __future__ import annotations

import unittest

from _harness import temp_data_dir  # first: import paths, hermetic env, LLM / live-store guards

from core.config import get_config
from core.ports.embedding import HashEmbedding
from core.store import Store
```

`_harness` puts the plugin root and `bin/` on `sys.path` (so hook modules import by name),
clears the developer's ambient `ENGRAM_*` env, and pins the heuristic distiller + a temp
`ENGRAM_DATA_DIR`. A module that needs none of its helpers still imports it:
`import _harness  # noqa: F401`. `test_harness` fails any module that doesn't.

---

## Pure function (`core/domain/scoring.py`, `core/domain/quantize.py`, `core/index/chunking.py`)

No fixtures. Test a known pair, a boundary, and an invariant.

```python
from core.domain.quantize import cosine, dequantize_int8, quantize_int8
from core.domain.scoring import frequency_boost, recency_decay


class QuantizeTests(unittest.TestCase):
    def test_int8_roundtrip_preserves_direction(self):
        emb = HashEmbedding(dim=128)
        vec = emb.embed_one("the quick brown fox")
        blob, scale = quantize_int8(vec)
        self.assertGreater(cosine(vec, dequantize_int8(blob, scale)), 0.98)


class ScoringTests(unittest.TestCase):
    def test_recency_decay_halves_at_one_half_life(self):
        self.assertAlmostEqual(recency_decay(30 * 86400, 30), 0.5, places=3)

    def test_frequency_boost_is_monotonic(self):
        self.assertGreater(frequency_boost(4), frequency_boost(2))
```

Use `subTest` for a table of cases (stdlib parametrisation):

```python
def test_recency_decay_curve(self):
    cases = [(0, 30, 1.0), (30, 30, 0.5), (90, 30, 0.125)]
    for days, half_life, expected in cases:
        with self.subTest(days=days):
            self.assertAlmostEqual(recency_decay(days * 86400, half_life), expected, places=3)
```

---

## Store round-trip (`core/store.py`, `core/service.py`)

`temp_data_dir` fixture; a `HashEmbedding` and (usually) a stub distiller.

```python
from unittest import mock
from core import service
from core.ports.distill import DistilledFact


class _StubDistiller:
    def __init__(self, records):
        self._records = records

    def distill(self, text, existing):
        return [DistilledFact(**r) for r in self._records]

    def summarize(self, text):
        return None


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_data_dir(self)
        self.cfg = get_config()
        self.store = Store(self.cfg.db_path)
        self.embedder = HashEmbedding(dim=self.cfg.dim)
        self.project = {"key": "p", "path": "/tmp/p", "label": "p"}

    def tearDown(self):
        self.store.close()

    def test_capture_then_recall_returns_the_fact(self):
        # Arrange
        records = [{"text": "deploys to AWS Lambda", "title": "Deploy", "type": "fact"}]
        # Act
        with mock.patch.object(service, "get_distiller", return_value=_StubDistiller(records)):
            service.capture_text(self.store, self.embedder, self.cfg, self.project, "s1", "raw text")
        # Assert
        self.assertEqual(self.store.active_count(self.project["key"]), 1)
```

---

## Recall / search (`core/recall/`, `core/domain/fusion.py`)

Test agreement, tie-breaks, and the empty case. Fusion is pure over channels:

```python
from core.domain.fusion import Channel, fuse


class FusionTests(unittest.TestCase):
    def test_agreement_across_channels_wins(self):
        channels = [
            Channel("similarity", ["a", "b", "c"]),
            Channel("lexical", ["a", "c", "b"]),
            Channel("recency", ["b", "a", "c"]),
        ]
        self.assertEqual(fuse(channels)[0].fact_id, "a")

    def test_empty_channels_yield_nothing(self):
        self.assertEqual(fuse([]), [])
```

The context gate must suppress a below-threshold match — assert recall returns
**nothing** (Null Object), never an error, when no fact clears `min_sim`.

---

## Adapter behind a port + fail-open (`core/ports/distill.py`, `core/ports/embedding.py`)

Test the zero-dep implementation directly; assert the fallback path explicitly.

```python
from core.ports.distill import HeuristicDistiller, get_distiller


class DistillerFallbackTests(unittest.TestCase):
    def test_heuristic_distiller_extracts_lines_without_a_model(self):
        facts = HeuristicDistiller().distill("line one\nline two", existing=[])
        self.assertTrue(facts)

    def test_capture_flags_degraded_when_llm_distiller_raises(self):
        # A distiller that dies must fall back to the heuristic and flag `degraded`,
        # never break capture. Stub the LLM path to raise, assert the fallback fired.
        ...
```

Gate the real model behind a skip (see [`skip-conventions.md`](skip-conventions.md)):

```python
@unittest.skipUnless(_has_fastembed(), "fastembed not provisioned")
class FastEmbedTests(unittest.TestCase):
    ...
```

---

## Hook as subprocess (`bin/*.py`)

Hooks read a JSON event on stdin and write `additionalContext` on stdout. Drive
them as a real process and assert the fail-open contract.

```python
import json
import os
import subprocess
import sys

from _harness import ROOT  # the child inherits the harness env: heuristic distiller, temp data dir


class PreToolUseGuardTests(unittest.TestCase):
    def _run(self, event: dict, env: dict) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(ROOT / "bin" / "prefer_memory.py")],
            input=json.dumps(event),
            capture_output=True, text=True, timeout=10,
            env={**os.environ, **env},
        )

    def test_guard_exits_zero_on_malformed_event(self):
        # Fail-open: garbage in still exits 0 and blocks nothing.
        proc = self._run({"not": "a real event"}, env={"ENGRAM_ENFORCE": "strict"})
        self.assertEqual(proc.returncode, 0)
```

Namespace per-session markers by PID (as `test_hooks.py` does) so dedupe state
doesn't collide across runs.

---

## CLI subcommand (`bin/engram`)

```python
class DoctorCommandTests(unittest.TestCase):
    def test_doctor_exits_zero_and_reports_project(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "bin" / "engram"), "doctor"],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "ENGRAM_DATA_DIR": self.tmp.name},
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("project", proc.stdout.lower())
```

---

## MCP tool (`bin/mcp_server.py`)

Call the underlying function with a fixture store; assert the payload and verdict.

```python
class RecallToolTests(unittest.TestCase):
    def test_recall_reports_no_memory_verdict_on_empty_store(self):
        result = recall_tool(self.store, self.embedder, self.cfg, self.project, query="anything")
        self.assertEqual(result["verdict"], "no_memory")
```

---

## When NOT to write a test

Per [`test-leanness-heuristics.md`](test-leanness-heuristics.md), these produce
dead weight:

1. **Trivial getter** — asserting a dataclass returns its init value.
2. **Tautological** — `assertEqual(f(x), f(x))`; both sides are the same call.
3. **Type-checker-redundant** — `assertIsInstance(x, T)` when the signature returns `T`.
4. **Stdlib guarantee** — asserting `json.dumps` round-trips, or that a `dict`
   merge overrides — that is Python's job.

Spend the budget where it surfaces a real failure mode: the retrieval path,
supersession/expiry, and every fail-open branch.
