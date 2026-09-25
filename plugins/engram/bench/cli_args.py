"""The ``engram eval`` flag set — defined once, used by ``bench/run_eval.py`` and ``bin/engram``.

Deliberately stdlib-only (argparse + pathlib): the CLI builds its parser for every command,
so defining the eval flags must not import the benchmark harness or the core.
"""

from __future__ import annotations

import argparse
from pathlib import Path


def add_eval_arguments(parser: argparse.ArgumentParser) -> None:
    """Add every eval flag to ``parser`` (a top-level parser or the ``engram eval`` subparser)."""
    parser.add_argument("--backends", default="hash", help="comma-separated specs: name[@model][%%dim][+float]")
    parser.add_argument("--stm", action="store_true", help="also run the STM-tier lever scenario")
    parser.add_argument(
        "--antipatterns", action="store_true", help="also run the global anti-pattern surfacing scenario"
    )
    parser.add_argument("--integrate", action="store_true", help="also run the integrate (gist-chunking) scenario")
    parser.add_argument(
        "--confidence", action="store_true", help="also run the recall-verdict calibration benchmark (per backend)"
    )
    parser.add_argument(
        "--aged", action="store_true", help="also run the age-aware ranking benchmark (old vs new gold, per backend)"
    )
    parser.add_argument(
        "--distractors",
        type=int,
        default=0,
        help="--confidence/--aged: pad the store with N facts mined from a real store",
    )
    parser.add_argument(
        "--distractor-project",
        help="--confidence/--aged: project key or label to mine distractors from (required with N>0)",
    )
    parser.add_argument(
        "--distractor-db",
        type=Path,
        help="--confidence/--aged: engram DB to mine from (default: the configured store)",
    )
    parser.add_argument(
        "--ok-precision", type=float, default=0.90, help="--confidence: how often an `ok` verdict must be right"
    )
