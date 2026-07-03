# QE Standards

The test-quality bar for claude-ltm. Covers naming, AAA structure, fixture
hygiene, what to test per code type, the anti-pattern checklist, and the common
failure-pattern table. Applies to every test under `plugins/ltm/tests/`.

The suite is stdlib `unittest` (also runs under `pytest`), all standard-library,
no network. Keep it that way — see [`skip-conventions.md`](skip-conventions.md).

---

## Naming convention

Format: `test_<subject>_<condition>_<expected>`. The name should read as a
sentence about behaviour, not a label for a function.

```python
# Good
def test_int8_roundtrip_preserves_direction(self):
def test_recency_decay_halves_at_one_half_life(self):
def test_empty_channels_yield_nothing(self):
def test_capture_falls_back_to_heuristic_when_distiller_dies(self):

# Bad — vague
def test_quantize(self):
def test_works(self):
def test_error(self):
```

**Class names:** group by the subject under test — `QuantizeTests`,
`ScoringTests`, `FusionTests`, `RecoveryTests`. One `TestCase` per cohesive
behaviour area; split when `setUp` starts branching.

**Exceptions:** a `subTest` loop over input/expected pairs (the stdlib analogue of
parametrisation) can use a generic method name because the sub-test id carries the
variation:

```python
def test_recency_decay_curve(self):
    for age_days, half_life, expected in [(0, 30, 1.0), (30, 30, 0.5)]:
        with self.subTest(age_days=age_days):
            self.assertAlmostEqual(recency_decay(age_days * 86400, half_life), expected, places=3)
```

---

## AAA structure (Arrange – Act – Assert)

Every test has three visually separated regions:

```python
def test_int8_roundtrip_preserves_direction(self):
    # Arrange
    emb = HashEmbedding(dim=128)
    vec = emb.embed_one("the quick brown fox jumps over the lazy dog")

    # Act
    blob, scale = quantize_int8(vec)

    # Assert
    self.assertGreater(cosine(vec, dequantize_int8(blob, scale)), 0.98)
```

Rules:
- Act is one call where practical; multiple asserts on that one Act are fine.
- Section comments are encouraged but the visual break should be obvious without
  them.
- Interleaved act/assert (several SUT calls scattered between assertions) is an
  anti-pattern — split into separate tests.

---

## Fixture hygiene

State-touching tests set up a throwaway store and tear it down completely. The
canonical shape (see [`test-data.md`](test-data.md) for the full pattern):

```python
def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    os.environ["LTM_DATA_DIR"] = self.tmp.name
    self.cfg = get_config()
    self.store = Store(self.cfg.db_path)
    self.embedder = HashEmbedding(dim=self.cfg.dim)

def tearDown(self):
    self.store.close()
    os.environ.pop("LTM_DATA_DIR", None)
    self.tmp.cleanup()
```

Non-negotiable:
- **Restore every env var** you set (`LTM_DATA_DIR`, `LTM_DISTILLER`, …) in
  `tearDown`, or tests pollute each other and pass/fail depending on order.
- **Close every `Store`.** An open SQLite handle leaks across tests.
- **No shared mutable module state.** Factories/helpers return fresh objects.
- **No live embedding model in the default path.** `HashEmbedding` or a stub.

---

## Stubs, not mocks-of-everything

The two LLM/embedding seams are ports with tiny interfaces. Test against a
**duck-typed stub class** that implements the real method signatures, not a bare
`Mock()`:

```python
class _StubDistiller:
    def __init__(self, records):
        self._records = records

    def distill(self, text, existing):           # matches core/distill.py
        return [DistilledFact(**r) for r in self._records]

    def summarize(self, text):
        return None
```

Inject it with `mock.patch.object(service, "get_distiller", return_value=_StubDistiller(...))`.
The stub must track the real port's signature — a drifted stub is the "outdated
test double" anti-pattern below.

---

## What to test per code type

### Pure function (`core/scoring.py`, `core/quantize.py`, `core/chunking.py`)
Minimum 3: a known input/output pair, a boundary (empty / zero / max), and any
invariant (idempotence, monotonicity, round-trip fidelity). No fixtures needed.

### Store-touching component (`core/service.py`, `core/store.py`, `core/recall.py`)
Tempdir + `LTM_DATA_DIR` fixture. Test: the happy write→read round-trip, the empty
/ no-match case (recall returns nothing, not an error), consolidation/supersession
where relevant, and status filtering (`active` vs `superseded`/`expired`).

### Adapter behind a port (`core/embedding.py`, `core/distill.py`)
Test the zero-dep implementation directly (`HashEmbedding`, `HeuristicDistiller`).
Gate the real implementation (`FastEmbedGateway`, `ClaudeCliDistiller`) behind
`@unittest.skipUnless`. **Test the fail-open path:** a broken/unavailable backend
falls back and flags `degraded` rather than raising.

### Hook (`bin/*.py`)
Hooks are stdin/stdout processes — test them as a **subprocess** (see
`test_hooks.py`), feeding a JSON event on stdin and asserting exit 0 and the
emitted `additionalContext`. Always assert the **fail-open** contract: a malformed
event still exits 0 and injects nothing.

### CLI subcommand (`bin/ltm`)
Invoke via `subprocess` or call the `cmd_*` function directly with a parsed args
namespace; assert exit code and stdout shape.

### MCP tool (`bin/mcp_server.py`)
Call the underlying function with a fixture store; assert the returned payload
shape and the calibrated verdict (`ok` / `low_confidence` / `no_memory`).

---

## Anti-pattern checklist (review mode)

Advisory findings — each is `pattern + file:line + suggested fix`.

### 1. Live model / network in a `core/**` test
**Detect:** a test under the core path imports `fastembed`, opens a socket, or
spawns `claude -p` without a skip-gate.
**Fix:** use `HashEmbedding` / a stub distiller, or move behind `@unittest.skipUnless`.

### 2. Leaked global state
**Detect:** `os.environ[...] = ...` in `setUp`/body with no matching `pop` in
`tearDown`; a `Store()` never `.close()`d.
**Fix:** restore in `tearDown`; close the store.

### 3. Outdated test double
**Detect:** a stub method whose signature no longer matches the real port
(`distill(self, text)` when the port is `distill(self, text, existing)`), or a
`mock.patch` target that no longer exists.
**Fix:** re-sync the stub / patch target against the current adapter.

### 4. Fail-open assumed, not asserted
**Detect:** a hook/adapter test that only exercises the happy path when the
component's contract is "never break the turn".
**Fix:** add a case feeding broken input / a dead daemon and assert exit 0 / fallback.

### 5. Asserting a stdlib/framework guarantee
**Detect:** `assertIsInstance(x, ExpectedType)` where the signature already returns
that type; asserting a dataclass stores its init value.
**Fix:** delete, or replace with a behavioural assertion. See
[`test-leanness-heuristics.md`](test-leanness-heuristics.md).

---

## Common failure patterns + fixes (run mode)

| Failure text | Likely cause | Fix |
|---|---|---|
| Test passes alone, fails in the suite | Leaked `LTM_DATA_DIR` / env not restored | Restore all env in `tearDown` |
| `sqlite3.OperationalError: database is locked` | A `Store` left open by a prior test | `self.store.close()` in `tearDown` |
| `AttributeError` on a patched adapter | Stub/patch target drifted from the real port | Re-sync signature; `review` mode flags this |
| Import of `fastembed` fails in a core test | Core test reached for the real adapter | Use `HashEmbedding`; gate real path with `skipUnless` |
| `... skipped=N` when you expected them to run | Optional dep (`fastembed`, tree-sitter) absent | Install the dep, or accept the skip — this is expected |
| `ltm eval` returns all zeros | Bad backend spec or missing `bench/dataset.json` | Sanity-check with `--backends hash` |
| Hook subprocess test hangs | Waiting on a real model/daemon | Point at the stub / in-process fallback; set a timeout |

---

## Why these standards

- **Naming + AAA** make a test self-documenting — the next reader learns the
  contract from the test.
- **Fixture hygiene** is the single largest source of order-dependent flakes in a
  suite that shares one SQLite store shape.
- **Stdlib-purity + skip-gating** protect the zero-dependency promise the whole
  design rests on.
- **Fail-open assertions** are the point of a fail-open design — an unasserted
  fallback is an untested one.
