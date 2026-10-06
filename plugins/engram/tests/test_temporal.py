"""The time-window Value Object and its ranking (core/domain/temporal.py) — pure, stdlib."""

from __future__ import annotations

import dataclasses
import math
import unittest

import _harness  # noqa: F401

from core.domain.fusion import Fused
from core.domain.temporal import WINDOW_BOOST, WINDOW_HALF_LIFE, TimeWindow, boost_by_window


class TimeWindowTests(unittest.TestCase):
    def test_distance_is_zero_inside_and_seconds_outside(self):
        window = TimeWindow(100.0, 200.0)
        self.assertEqual([window.distance(t) for t in (100.0, 150.0, 200.0)], [0.0, 0.0, 0.0])
        self.assertEqual((window.distance(40.0), window.distance(260.0)), (60.0, 60.0))

    def test_either_bound_may_be_open(self):
        self.assertEqual(TimeWindow(after=100.0).distance(10**9), 0.0)
        self.assertEqual(TimeWindow(after=100.0).distance(90.0), 10.0)
        self.assertEqual(TimeWindow(before=100.0).distance(-(10**9)), 0.0)
        self.assertEqual(TimeWindow(before=100.0).distance(130.0), 30.0)

    def test_invalid_windows_are_refused(self):
        with self.assertRaises(ValueError):
            TimeWindow()
        with self.assertRaises(ValueError):
            TimeWindow(after=200.0, before=100.0)

    def test_a_value_object(self):
        window = TimeWindow(1.0, 2.0)
        self.assertEqual(window, TimeWindow(1.0, 2.0))
        self.assertEqual(hash(window), hash(TimeWindow(1.0, 2.0)))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            window.after = 0.0  # type: ignore[misc]


class WindowBoostTests(unittest.TestCase):
    def test_full_boost_inside_halving_outside_none_without_a_stamp(self):
        window = TimeWindow(100.0, 200.0)
        self.assertEqual(window.boost(150.0), 1.0 + WINDOW_BOOST)
        self.assertAlmostEqual(window.boost(200.0 + WINDOW_HALF_LIFE), 1.0 + WINDOW_BOOST / 2)
        self.assertAlmostEqual(window.boost(100.0 - 2 * WINDOW_HALF_LIFE), 1.0 + WINDOW_BOOST / 4)
        self.assertEqual(window.boost(None), 1.0)
        self.assertGreaterEqual(window.boost(10**12), 1.0)  # never a penalty — it fades to no boost

    def test_an_in_window_candidate_overtakes_an_equal_neighbour(self):
        fused = [Fused("out", 0.0295), Fused("in", 0.0290)]  # adjacent RRF ranks: ~1.6% apart
        ranked = boost_by_window(fused, {"out": 0.0, "in": 150.0 * 86400}, TimeWindow(100.0 * 86400, 200.0 * 86400))
        self.assertEqual([f.fact_id for f in ranked], ["in", "out"])

    def test_re_orders_without_adding_or_dropping(self):
        fused = [Fused("a", 3.0), Fused("b", 2.0), Fused("c", 1.0)]
        ranked = boost_by_window(fused, {"a": 0.0, "b": None}, TimeWindow(after=10**9))
        self.assertEqual(sorted(f.fact_id for f in ranked), ["a", "b", "c"])
        self.assertEqual([f.fact_id for f in ranked], ["a", "b", "c"])  # nobody is in the window: order holds
        self.assertEqual([f.score for f in fused], [3.0, 2.0, 1.0])  # the input is not mutated

    def test_a_strong_out_of_window_match_still_wins_a_large_gap(self):
        fused = [Fused("strong", 0.0295), Fused("weak", 0.0170)]  # RRF rank 0 vs ~rank 45 on both channels
        ranked = boost_by_window(fused, {"strong": 0.0, "weak": 150.0}, TimeWindow(100.0, 200.0))
        self.assertEqual(ranked[0].fact_id, "strong")
        self.assertTrue(math.isfinite(ranked[1].score))


if __name__ == "__main__":
    unittest.main()
