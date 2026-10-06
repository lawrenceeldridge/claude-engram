"""Vectorised recall scan (vectorised-recall-scan).

The numpy scorer must rank identically to the pure-Python scan — it is a speed optimisation
of the same cosine maths, not a behaviour change. Fake dict rows suffice (both scorers only
read ``row["dim"|"vec_int8"|"scale"]``). Stdlib unittest; numpy is exercised when present
(it ships with the fastembed extra), skipped cleanly otherwise.
"""

from __future__ import annotations

import random
import sys
import time
import unittest

import _harness  # noqa: F401

from core.domain.quantize import quantize_int8
from core.ports.scorer import DIM_MISMATCH, PurePythonScorer, get_scorer

try:
    import numpy  # noqa: F401

    from core.adapters.numpy_scorer import NumpyScorer

    HAS_NUMPY = True
except Exception:
    HAS_NUMPY = False


def _row(vec: list[float], dim: int | None = None) -> dict:
    blob, scale = quantize_int8(vec)
    return {"id": "f", "dim": dim if dim is not None else len(vec), "vec_int8": blob, "scale": scale}


def _order(sims: list[float]) -> list[int]:
    return sorted(range(len(sims)), key=lambda i: sims[i], reverse=True)


@unittest.skipUnless(HAS_NUMPY, "numpy not installed")
class NumpyIdentityTests(unittest.TestCase):
    """numpy scorer produces the same ranking (and ~same similarities) as pure-Python."""

    def test_ranking_identical_over_random_vectors(self):
        rng = random.Random(0)
        dim = 16
        rows = [_row([rng.uniform(-1, 1) for _ in range(dim)]) for _ in range(60)]
        query = [rng.uniform(-1, 1) for _ in range(dim)]

        pure = PurePythonScorer().cosine_all(rows, query)
        nump = NumpyScorer().cosine_all(rows, query)

        self.assertEqual(_order(pure), _order(nump), "top-k ordering must match pure-Python")
        for a, b in zip(pure, nump):
            self.assertAlmostEqual(a, b, places=5)

    def test_dim_mismatch_is_neg_inf_in_both(self):
        rows = [_row([1.0, 2.0, 3.0, 4.0]), _row([1.0, 2.0], dim=2)]  # 2nd row wrong dim vs a dim-4 query
        query = [1.0, 2.0, 3.0, 4.0]
        pure = PurePythonScorer().cosine_all(rows, query)
        nump = NumpyScorer().cosine_all(rows, query)
        self.assertEqual(pure[1], DIM_MISMATCH)
        self.assertEqual(nump[1], DIM_MISMATCH)
        self.assertAlmostEqual(pure[0], nump[0], places=5)

    def test_zero_vector_scores_zero_in_both(self):
        rows = [_row([0.0, 0.0, 0.0, 0.0])]
        query = [1.0, 0.0, 0.0, 0.0]
        self.assertEqual(PurePythonScorer().cosine_all(rows, query)[0], 0.0)
        self.assertEqual(NumpyScorer().cosine_all(rows, query)[0], 0.0)

    def test_empty_rows(self):
        self.assertEqual(NumpyScorer().cosine_all([], [1.0, 2.0]), [])

    def test_get_scorer_selects_numpy_when_present(self):
        # With numpy importable, the factory prefers the vectorised adapter.
        class _Cfg:
            scorer = "auto"

        self.assertIsInstance(get_scorer(_Cfg()), NumpyScorer)


class PureScorerTests(unittest.TestCase):
    """PurePythonScorer edge behaviour (runs with or without numpy)."""

    def test_dim_mismatch_and_gate(self):
        rows = [_row([1.0, 2.0, 3.0]), _row([1.0, 2.0], dim=2)]
        sims = PurePythonScorer().cosine_all(rows, [1.0, 2.0, 3.0])
        self.assertGreater(sims[0], 0.999)  # identical vector → cosine ≈ 1 (modulo int8 quantisation loss)
        self.assertEqual(sims[1], DIM_MISMATCH)


class FallbackTests(unittest.TestCase):
    """get_scorer degrades to pure-Python when numpy is unavailable or overridden off."""

    def test_falls_back_to_pure_when_numpy_import_fails(self):
        _MISSING = object()
        saved = sys.modules.get("numpy", _MISSING)
        sys.modules["numpy"] = None  # makes `import numpy` raise ImportError
        try:

            class _Cfg:
                scorer = "auto"

            self.assertIsInstance(get_scorer(_Cfg()), PurePythonScorer)
        finally:
            if saved is _MISSING:
                sys.modules.pop("numpy", None)
            else:
                sys.modules["numpy"] = saved

    def test_scorer_python_override_forces_pure(self):
        # Hidden ENGRAM_SCORER=python override: pure-Python even when numpy is present.
        class _Cfg:
            scorer = "python"

        self.assertIsInstance(get_scorer(_Cfg()), PurePythonScorer)

    def test_config_exposes_scorer_default_auto(self):
        from core.config import get_config

        self.assertEqual(getattr(get_config(), "scorer", None), "auto")


@unittest.skipUnless(HAS_NUMPY, "numpy not installed")
class ScaleGateTests(unittest.TestCase):
    """Acceptance gate: the vectorised scan clears the recall hook's 5s ceiling at scale."""

    def test_numpy_scan_under_5s_at_100k(self):
        rng = random.Random(0)
        dim = 64
        n = 100_000
        rows = [{"id": str(i), "dim": dim, "vec_int8": rng.randbytes(dim), "scale": 1.0} for i in range(n)]
        query = [rng.uniform(-1, 1) for _ in range(dim)]

        scorer = NumpyScorer()
        start = time.perf_counter()
        sims = scorer.cosine_all(rows, query)
        elapsed = time.perf_counter() - start

        self.assertEqual(len(sims), n)
        # Real numbers are tens of ms; the 5s bound is the hard hook ceiling this fix must clear.
        self.assertLess(elapsed, 5.0, f"numpy scan of {n} rows took {elapsed:.2f}s (>5s recall-hook ceiling)")


if __name__ == "__main__":
    unittest.main()
