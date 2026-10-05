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
    parser.add_argument(
        "--confidence-out", type=Path, help="--confidence: append per-query labels + scores (JSONL) here"
    )
    parser.add_argument(
        "--longmemeval",
        action="store_true",
        help="also run LongMemEval session retrieval: parity / verbatim / distilled / hybrid arms",
    )
    parser.add_argument("--lme-path", type=Path, help="--longmemeval: local longmemeval_s_cleaned.json")
    parser.add_argument(
        "--lme-download", action="store_true", help="--longmemeval: fetch the dataset (HF, MIT) into the data dir"
    )
    parser.add_argument(
        "--lme-limit",
        type=int,
        default=60,
        help="--longmemeval: stratified question sample within --lme-split (0 = the whole split)",
    )
    parser.add_argument(
        "--lme-split",
        choices=("dev", "test", "all"),
        default="dev",
        help="--longmemeval: fixed hold-out to run — tune on dev (20%%), report once on test (80%%);"
        " all = the full set",
    )
    parser.add_argument("--lme-out", type=Path, help="--longmemeval: append per-question arm scores (JSONL) here")
    parser.add_argument(
        "--lme-shipped",
        action="store_true",
        help="--longmemeval: also run the shipped capture → recall / search_history path (Vs/Ds/Hs; ~2x runtime,"
        " and with --lme-llm the LLM run distils each session twice)",
    )
    parser.add_argument(
        "--lme-llm",
        type=int,
        default=0,
        help="--longmemeval: also run an LLM-distilled arm on the first N sampled questions (external API cost)",
    )
