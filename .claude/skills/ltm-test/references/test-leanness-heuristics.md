# Test-Leanness Heuristics

Heuristics for the `audit` mode — finding tests that can be deleted, merged,
turned into a `subTest` loop, or replaced. Output is always **advisory**: the
skill proposes, the human decides. It never edits or deletes a test.

---

## Why this exists

Tests are easy to add and hard to remove. A suite that grows without pruning fills
with tests that duplicate coverage already present. The goal is **lean meaningful
coverage**, not a high test count. A test that adds no failure mode beyond what
another test already catches is dead weight; a test that guards a real regression
or a fail-open branch is invaluable.

---

## The heuristics

### 1. Trivial value assertion

Asserts an object returns the value it was constructed with.

```python
def test_config_dim_is_set(self):
    cfg = get_config()
    self.assertEqual(cfg.dim, cfg.dim)   # asserts nothing
```

**Why delete:** the dataclass/config guarantees it. **Action:** delete, or replace
with a behavioural assertion (does `dim` actually shape the embedding?).

### 2. Duplicate assertion across tests

Two tests exercise the same path and assert the same thing, differing only in name.

```python
def test_capture_stores_a_fact(self):
    service.capture_text(...); self.assertEqual(self.store.active_count("p"), 1)

def test_capture_persists(self):        # near-duplicate
    service.capture_text(...); self.assertEqual(self.store.active_count("p"), 1)
```

**Action:** merge into one. If the only difference is input, use `subTest`
(heuristic 4).

### 3. Tautological test

Re-implements the SUT, or asserts a stdlib/library guarantee.

```python
def test_cosine_of_equal_vectors(self):
    v = embedder.embed_one("x")
    self.assertEqual(cosine(v, v), cosine(v, v))   # both sides identical

def test_quantize_returns_bytes(self):
    self.assertIsInstance(quantize_int8(v)[0], bytes)  # the signature guarantees it
```

**Action:** delete. Replace with something that exercises real behaviour
(`cosine(v, v)` should be `~1.0`; quantise→dequantise should preserve direction).

### 4. `subTest` candidate

Three or more methods with near-identical bodies varying only an input/expected.

```python
def test_decay_at_zero(self):   self.assertAlmostEqual(recency_decay(0, 30), 1.0, 3)
def test_decay_at_half(self):   self.assertAlmostEqual(recency_decay(30*86400, 30), 0.5, 3)
def test_decay_at_two(self):    self.assertAlmostEqual(recency_decay(60*86400, 30), 0.25, 3)
```

**Action:** collapse to one method with a `subTest` loop (the stdlib analogue of
`@pytest.mark.parametrize`) — the sub-test id preserves per-case failure reporting:

```python
def test_recency_decay_curve(self):
    for days, expected in [(0, 1.0), (30, 0.5), (60, 0.25)]:
        with self.subTest(days=days):
            self.assertAlmostEqual(recency_decay(days * 86400, 30), expected, places=3)
```

### 5. Snapshot-style overlap

Several tests run the same SUT call and each assert one field of the result.

**Action:** merge into one test that asserts the mutated field plus the preserved
invariants, keeping the Act call singular.

### 6. Test for dead code

The SUT no longer exists or has no caller outside the test.

**Detect:** grep the source for the symbol; a failing import or zero non-test
callers signals dead code. **Action:** delete the test *and* the dead code
(separate change if large). **Caveat:** the symbol may be a public CLI/MCP entry
point exercised only end-to-end — verify before deleting.

### 7. Redundant with a type guarantee

Asserts what the function signature already enforces.

```python
def test_fuse_returns_a_list(self):
    self.assertIsInstance(fuse([]), list)   # -> list[...] in the signature
```

**Action:** delete; tighten the annotation if it is `Any`.

### 8. Hand-rolled domain sweep → property test

A long `subTest`/loop hand-enumerates a function's input domain. If `hypothesis`
is available, a property test covers it more thoroughly in fewer lines.

```python
# hypothesis is OPTIONAL — not a default dependency. Gate it:
@unittest.skipUnless(_has_hypothesis(), "hypothesis not installed")
```

**Action:** *optionally* convert; keep a couple of explicit edge rows (empty
string, max length, Unicode). Do not add `hypothesis` to the core path just to do
this — the stdlib `subTest` version is perfectly acceptable.

---

## "What NOT to delete" allowlist

Even when a test matches a heuristic, **do not** propose deleting/merging it if any
flag fires:

### Flag 1 — Regression guard with a linked issue
The docstring or a nearby comment references a specific issue/PR/commit. The test
prevents a known regression; deleting it re-opens the bug.

### Flag 2 — Explicit `DO NOT DELETE`
Someone marked it deliberately. Respect the marker.

### Flag 3 — Fail-open / recovery guard
The test asserts a hook/adapter exits 0 on bad input, that capture falls back to
the heuristic and flags `degraded`, or that a dead daemon degrades to in-process.
These guard the design's core promise — never propose deleting them.

### Flag 4 — Non-obvious failure mode
Concurrency, ordering, throttling/dedupe, TTL/expiry timing, supersession edge
cases. **Detect:** look for `subprocess`, `os.getpid()` namespacing, time/`ttl`
manipulation, `mock.side_effect`, or freshness/staleness checks.

### Flag 5 — Sole coverage of a branch
The only test hitting a specific branch. Deleting it drops that branch to 0%.
Cross-reference the `coverage` mode before proposing deletion.

---

## Audit output format

```
Candidate: tests/test_smoke.py:118
  Heuristic: subTest candidate (3 near-identical decay methods)
  Proposed action: collapse into one test_recency_decay_curve with a subTest loop
  Reason: same code path, different (age, expected) pairs; ~15 LoC saved, failure reporting preserved.
  Guards: none fired

Candidate: tests/test_recovery.py:52
  Heuristic: (none — kept)
  Why: fail-open guard — asserts capture flags `degraded` when the distiller dies (Flag 3).
```

The skill emits the list; the human reviews and acts. The asymmetry is deliberate.

---

## Why advisory, not auto-delete

1. **Tests are documentation** — even a "trivial" test communicates intent.
2. **Coverage data may be wrong** — a trivial-looking test may be the only one
   hitting an edge case the report missed.
3. **False positives compound** — a proposed deletion gets reviewed; a shipped
   auto-deletion does not.
