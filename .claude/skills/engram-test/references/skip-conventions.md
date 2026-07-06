# Skip Conventions

claude-engram has **no pytest marker system** — no tiers, no `@pytest.mark.integration`.
The suite is stdlib `unittest`, and the only tagging it needs is a way to skip
tests whose optional dependency is absent. This keeps the default run green with
zero dependencies (`137 tests, 5 skipped`) while still exercising the real
adapters when they are installed.

---

## The stdlib-purity rule

Tests for `core/**` **must run without `fastembed` and without the network.** The
default `hash` embedding + `heuristic` distiller make this possible; it is the
promise the whole design rests on (see
[`.claude/rules/00-quality/02-testing.md`](../../../rules/00-quality/02-testing.md)).

Anything that needs a real model or a live service is the exception, and it must
be **skip-gated** so its absence yields a `skip`, never a failure.

---

## Gating an optional dependency

Probe the import once, then gate the class or method with `@unittest.skipUnless`:

```python
def _has_fastembed() -> bool:
    try:
        import fastembed  # noqa: F401
        return True
    except Exception:
        return False


def _has_treesitter() -> bool:
    try:
        import tree_sitter_language_pack  # noqa: F401
        return True
    except Exception:
        return False


@unittest.skipUnless(_has_treesitter(), "tree-sitter not provisioned")
class TypeScriptSymbolTests(unittest.TestCase):
    ...


@unittest.skipUnless(_has_fastembed(), "fastembed not provisioned")
def test_bge_base_generalises_across_paraphrase(self):
    ...
```

Rules:
- The **probe returns `False` on any exception**, not just `ImportError` — a
  half-provisioned venv should skip, not crash collection.
- Gate at the **narrowest scope** that makes sense: the whole `TestCase` if every
  method needs the dep, a single method otherwise.
- The skip reason names the **missing dep**, so a reader knows what to install to
  un-skip it.

### Optional deps in claude-engram

| Dependency | Gates | Fallback when absent |
|---|---|---|
| `fastembed` | real semantic embedding + its recall/eval tests | `HashEmbedding` (lexical, zero-dep) |
| `tree-sitter-language-pack` | TS/JS and non-Python code-symbol extraction | stdlib `ast` for Python; other languages skipped |
| `coverage` | the `coverage` mode | none — coverage is opt-in, not required |

A live LLM distiller (`claude -p`, or an Ollama endpoint) is never required by a
test: the LLM path is exercised through a **stub distiller** (see
[`test-data.md`](test-data.md)), and its *fallback* is tested by making the stub
raise.

---

## The `slow` convention

There is no `@pytest.mark.slow`. If a stdlib test is genuinely expensive (large
corpus embed, a subprocess that waits), keep it fast by default and, if it must
stay slow, gate it behind an env flag so the inner loop skips it:

```python
@unittest.skipUnless(os.environ.get("ENGRAM_SLOW_TESTS"), "slow test; set ENGRAM_SLOW_TESTS=1")
def test_embeds_a_large_corpus(self):
    ...
```

Prefer shrinking the fixture (fewer facts, smaller `dim`) over marking a test
slow — the whole suite should stay sub-second in the default path.

---

## What this replaces

The source project used a three-tier pytest-marker model (Unit / Citus-coord /
Full-cluster) enforced by a CI audit-grep test, because it ran a distributed
FastAPI/Citus app with real database and NATS infrastructure. claude-engram has none
of that — one package, one stdlib suite, dependencies that are all *optional*
rather than *infrastructural*. Skip-gating is the whole of the convention.
