# Test Data & Fixtures

How to produce test data and set up fixtures in claude-ltm. Everything here is
stdlib: local helpers, a stub embedding, stub distillers, and a throwaway store.
No faker, no database server, no network, no live model.

---

## Principle: local, deterministic, self-cleaning

- **Local** — build inputs inline or from a tiny helper in the test module. There
  is no external factory library and no fixture DB to seed.
- **Deterministic** — `HashEmbedding` is a pure function of its input text, so the
  same string always embeds the same way. Tests never depend on a downloaded
  model or a random seed.
- **Self-cleaning** — a `TemporaryDirectory` holds the SQLite store; `tearDown`
  removes it and restores every env var. Nothing survives a test.

The rule from the source project still holds: **a value should not look special
unless it IS special.** Hardcode `"SW1A 1AA"`-style constants only when the exact
value is the point (a known cosine, a boundary length, a specific slug).

---

## The store fixture

The one fixture almost every stateful test needs. `LTM_DATA_DIR` points the config
at a temp dir, so `get_config()` resolves a throwaway `db_path`:

```python
def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    os.environ["LTM_DATA_DIR"] = self.tmp.name
    self.cfg = get_config()
    self.store = Store(self.cfg.db_path)
    self.embedder = HashEmbedding(dim=self.cfg.dim)
    self.project = {"key": "p", "path": "/tmp/p", "label": "p"}

def tearDown(self):
    self.store.close()
    os.environ.pop("LTM_DATA_DIR", None)
    self.tmp.cleanup()
```

If a test needs a specific config knob, set the matching `LTM_*` env var in
`setUp` **before** `get_config()` and pop it in `tearDown`:

```python
os.environ["LTM_DISTILLER"] = "ollama"   # exercise the LLM/recovery path
os.environ["LTM_MIN_SIM"] = "0.0"        # disable the context gate for a ranking test
```

Restoring these is not optional — a leaked env var makes another test pass or fail
depending on run order.

---

## Stub embedding — `HashEmbedding`

`HashEmbedding` is the zero-dependency embedding gateway. It is not a mock; it is
a real, deterministic lexical embedder that satisfies the same `EmbeddingGateway`
port as `fastembed`. Use it everywhere in the default test path:

```python
from core.embedding import HashEmbedding

embedder = HashEmbedding(dim=128)
vec = embedder.embed_one("deploys to AWS Lambda")
```

Because it is lexical, two paraphrases with disjoint vocabulary will *not* match —
that is expected. Tests that need semantic generalisation belong under a
`@unittest.skipUnless(_has_fastembed(), ...)` gate, or in the `ltm eval` benchmark,
not in the core suite.

---

## Stub distillers and summarizers

The distiller and summarizer are ports with tiny interfaces. Implement a
duck-typed stub that matches the **real** signatures in `core/distill.py`, then
inject it with `mock.patch.object`:

```python
from core.distill import DistilledFact
from core import service


class _StubDistiller:
    def __init__(self, records):
        self._records = records

    def distill(self, text, existing):        # real signature
        return [DistilledFact(**r) for r in self._records]

    def summarize(self, text):                # real signature
        return None


def _titled():
    return [{"text": "uses gemma3 for distillation", "title": "Distiller", "type": "feature", "degraded": False}]


def _degraded():
    return [{"text": "some raw line", "type": "discovery", "degraded": True}]
```

Inject:

```python
with mock.patch.object(service, "get_distiller", return_value=_StubDistiller(_titled())):
    service.capture_text(self.store, self.embedder, self.cfg, self.project, "s1", raw_text)
```

A summarizer stub returns a single `DistilledFact` with `type="session_summary"`:

```python
class _StubSummarizer:
    def summarize(self, text):
        return DistilledFact(text="session did things", title="Session",
                             narrative="Investigated: x", type="session_summary")

    def distill(self, text, existing):
        return []
```

Keep stub signatures in lockstep with the real port — a drifted stub passes while
the real path is broken (the "outdated test double" anti-pattern in
[`standards.md`](standards.md)).

---

## Transcript / event fixtures

Hooks and capture consume JSONL transcripts and JSON hook events. Build them
inline — write a tiny file to the tempdir rather than checking in a fixture:

```python
def _write_transcript(self, path: Path, nbytes: int) -> None:
    msg = {"type": "assistant",
           "message": {"role": "assistant", "content": [{"type": "text", "text": "x" * nbytes}]}}
    path.write_text(json.dumps(msg) + "\n", encoding="utf-8")
```

A hook event is just a dict fed on stdin:

```python
event = {"tool_name": "Grep", "tool_input": {"pattern": "foo"}, "session_id": str(os.getpid())}
```

Namespace anything session-scoped (dedupe markers, per-session state) by
`os.getpid()` so parallel or repeated runs don't collide.

---

## Index fixtures (code/docs index tests)

The indexer re-reads live files to compute freshness, so index tests write real
files into the tempdir rather than faking the filesystem:

```python
def test_indexes_markdown_headings(self):
    doc = Path(self.tmp.name) / "guide.md"
    doc.write_text("# Setup\nintro\n\n## Database\nbody\n", encoding="utf-8")
    # index_file(...) then assert the outline / freshness
```

Tree-sitter-backed languages are gated:

```python
def _has_treesitter() -> bool:
    try:
        import tree_sitter_language_pack  # noqa: F401
        return True
    except Exception:
        return False


@unittest.skipUnless(_has_treesitter(), "tree-sitter not provisioned")
class TypeScriptSymbolTests(unittest.TestCase):
    ...
```

Python symbol extraction uses stdlib `ast`, so those tests need **no** gate.

---

## Summary

1. **One store fixture** — tempdir + `LTM_DATA_DIR`, closed and cleaned in `tearDown`.
2. **`HashEmbedding`** is the default embedder — deterministic, zero-dep, real port.
3. **Duck-typed stub distillers/summarizers** matching the live signatures; inject
   with `mock.patch.object`.
4. **Build transcripts/events inline**; write real files for index/freshness tests.
5. **Restore every env var**; gate every optional dep with `skipUnless`.
