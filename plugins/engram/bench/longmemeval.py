"""LongMemEval retrieval benchmark (``engram eval --longmemeval``).

Decides — by measurement — whether verbatim conversation earns a place beside distilled facts
(plan ``verbatim-episodic-layer``). Each LongMemEval question carries its own haystack of chat
sessions and the ids of the sessions holding the evidence; retrieval is scored at **session**
level with LongMemEval's own metrics (recall_any@k, recall_all@k, NDCG@k). Four arms, each a
fresh store per question, each producing one ranked list of units mapped to sessions:

* ``P`` parity  — one document per session (user turns joined), pure cosine: mempalace's
  96.6% "raw" configuration, re-run with engram's embedder so the number is comparable;
* ``V`` verbatim — ~800-char user+assistant exchanges, engram's hybrid chunk search;
* ``D`` distilled — facts from the configured distiller per session, the ``recall`` tool path;
* ``H`` hybrid  — rank fusion of V and D units.

Engram's own axis rides alongside: characters in the top-5 units — the token cost of the
model pulling them. Dataset: HF ``xiaowu0162/longmemeval-cleaned`` (MIT), fetched at runtime
into the data dir, never committed. Abstention questions (``_abs``) have no evidence sessions,
so they are counted, not scored.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import shutil
import statistics
import tempfile
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from bench.backends import make_embedder, parse_spec
from bench.report import print_rows
from bench.stats import bootstrap_ci, mcnemar_exact, ndcg_at_k, recall_all_at_k, recall_any_at_k, wilson
from core import service
from core.domain.episodes import prepare_exchanges
from core.domain.fusion import Channel, fuse
from core.domain.quantize import cosine
from core.index.index_recall import search_index
from core.index.indexer import exchange_chunk_units, index_nonfile
from core.ports.distill import LLM_DISTILLERS, get_distiller
from core.ports.embedding import EmbeddingGateway
from core.recall import search_fused
from core.store import Store

LME_URL = "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json"
LME_FILE = "longmemeval_s_cleaned.json"
KS = (1, 3, 5, 10)
POOL = 50  # units ranked per arm — deep enough for recall@10 over ~50 sessions
ARMS = ("P", "V", "D", "H")

Unit = tuple[str, str, str]  # (unit id, session id, text) — one ranked retrieval unit


@dataclass(frozen=True)
class Session:
    sid: str
    date: str
    turns: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Question:
    qid: str
    qtype: str
    question: str
    sessions: tuple[Session, ...]
    gold: frozenset[str]

    @property
    def abstention(self) -> bool:
        return self.qid.endswith("_abs")

    @property
    def scoreable(self) -> bool:
        """Has evidence sessions to retrieve (abstention questions have none)."""
        return not self.abstention and bool(self.gold)


def parse(entries: list[dict]) -> list[Question]:
    """LongMemEval entries → Questions (turns reduced to ``(role, content)``)."""
    questions = []
    for e in entries:
        sessions = tuple(
            Session(str(sid), str(date), tuple((t.get("role", ""), t.get("content") or "") for t in turns))
            for sid, date, turns in zip(e["haystack_session_ids"], e["haystack_dates"], e["haystack_sessions"])
        )
        questions.append(
            Question(
                qid=str(e["question_id"]),
                qtype=str(e["question_type"]),
                question=str(e["question"]),
                sessions=sessions,
                gold=frozenset(str(s) for s in e.get("answer_session_ids") or ()),
            )
        )
    return questions


def load(path: Path) -> tuple[list[Question], str]:
    """Parse a LongMemEval JSON file; returns the questions and the file's sha256."""
    raw = path.read_bytes()
    return parse(json.loads(raw)), hashlib.sha256(raw).hexdigest()


def download(dest_dir: Path) -> Path:
    """Fetch the dataset once into ``dest_dir`` (atomic: a partial download never masquerades)."""
    dest = dest_dir / LME_FILE
    if dest.exists():
        return dest
    dest_dir.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(".part")
    urllib.request.urlretrieve(LME_URL, partial)  # noqa: S310 — fixed https URL, bench-only
    partial.replace(dest)
    return dest


def stratified_sample(questions: list[Question], n: int, seed: int = 0) -> list[Question]:
    """``n`` questions spread round-robin across question types (seeded shuffle within each)."""
    if n <= 0 or n >= len(questions):
        return list(questions)
    rng = random.Random(seed)
    by_type: dict[str, list[Question]] = {}
    for q in questions:
        by_type.setdefault(q.qtype, []).append(q)
    for group in by_type.values():
        rng.shuffle(group)
    picked, types = [], sorted(by_type)
    while len(picked) < n:
        for qtype in types:
            if by_type[qtype] and len(picked) < n:
                picked.append(by_type[qtype].pop())
    return picked


def session_document(session: Session) -> str:
    """Mempalace's raw unit: the session's user turns joined."""
    return "\n".join(text for role, text in session.turns if role == "user" and text.strip())


def session_text(session: Session) -> str:
    """What capture would hand the distiller: every turn's text, one per line."""
    return "\n".join(text for _role, text in session.turns if text.strip())


def _project(root: Path, key: str) -> dict:
    return {"key": key, "path": str(root), "label": "lme"}


def rank_parity(embedder: EmbeddingGateway, q: Question) -> list[Unit]:
    docs = [(s.sid, session_document(s)) for s in q.sessions]
    docs = [(sid, text) for sid, text in docs if text]
    if not docs:
        return []
    qv = embedder.embed_query(q.question)
    scored = sorted(zip(embedder.embed([t for _s, t in docs]), docs), key=lambda p: cosine(qv, p[0]), reverse=True)
    return [(sid, sid, text) for _vec, (sid, text) in scored]


def rank_verbatim(embedder: EmbeddingGateway, cfg, q: Question, root: Path) -> list[Unit]:
    """Exchanges prepared and indexed exactly as capture stores them (redacted, gated), searched
    through the chunk index."""
    store, project = Store(root / "verbatim.db"), _project(root, "lme-v")
    texts: dict[str, tuple[str, str]] = {}
    try:
        for s in q.sessions:
            exchanges = prepare_exchanges(s.turns, project["path"], cfg.episodic_min_chars)
            units = exchange_chunk_units(s.sid, exchanges, f"{s.date} · {s.sid}")
            texts.update((u["anchor"], (s.sid, u["body"])) for u in units)
            index_nonfile(store, embedder, project, "exchange", s.sid, units)
        found = search_index(store, embedder, cfg, project, q.question, k=POOL, max_chars=10**9, kind="exchange")
        return [(r["anchor"], *texts[r["anchor"]]) for r in found["results"] if r["anchor"] in texts]
    finally:
        store.close()


def rank_distilled(embedder: EmbeddingGateway, cfg, distiller, q: Question, root: Path) -> list[Unit]:
    store, project = Store(root / "distilled.db"), _project(root, "lme-d")
    try:
        for s in q.sessions:
            text = session_text(s)
            if text:  # the distiller's records go in as capture writes them (types, degraded flags)
                service.add_records(store, embedder, cfg, project, s.sid, distiller.distill(text, []))
        hits = search_fused(store, embedder, project, q.question, cfg, k=POOL)
        return [(row["id"], row["session_id"], row["text"]) for _f, _sim, row in hits]
    finally:
        store.close()


def rank_hybrid(verbatim: list[Unit], distilled: list[Unit]) -> list[Unit]:
    """Rank fusion of the verbatim and distilled unit lists (engram's own RRF)."""
    by_id = {u[0]: u for u in verbatim + distilled}
    fused = fuse([Channel("verbatim", [u[0] for u in verbatim]), Channel("distilled", [u[0] for u in distilled])])
    return [by_id[f.fact_id] for f in fused]


def sessions_of(units: list[Unit]) -> list[str]:
    """Session ids in first-appearance order — a session ranks where its best unit ranks."""
    seen: dict[str, None] = {}
    for _uid, sid, _text in units:
        seen.setdefault(sid, None)
    return list(seen)


def score(units: list[Unit], gold: frozenset[str]) -> dict:
    """LongMemEval session metrics for one arm's ranking, plus the chars in its top-5 units."""
    ranked = sessions_of(units)
    row = {f"any@{k}": recall_any_at_k(ranked, set(gold), k) for k in KS}
    row["all@5"] = recall_all_at_k(ranked, set(gold), 5)
    row["ndcg@5"] = ndcg_at_k(ranked, set(gold), 5)
    row["chars@5"] = sum(len(text) for _u, _s, text in units[:5])
    return row


def evaluate_question(embedder, cfg, distiller, q: Question) -> dict[str, dict]:
    root = Path(tempfile.mkdtemp(prefix="engram-bench-lme-"))
    try:
        verbatim = rank_verbatim(embedder, cfg, q, root)
        distilled = rank_distilled(embedder, cfg, distiller, q, root)
        rankings = {"P": rank_parity(embedder, q), "V": verbatim, "D": distilled, "H": rank_hybrid(verbatim, distilled)}
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return {arm: score(units, q.gold) for arm, units in rankings.items()}


def _summary_rows(per_arm: dict[str, list[dict]]) -> list[dict]:
    rows = []
    for arm, scores in per_arm.items():
        n = len(scores)
        if not n:
            continue
        hits5 = sum(s["any@5"] for s in scores)
        lo, hi = wilson(hits5, n)
        chars = statistics.median(s["chars@5"] for s in scores) if scores else 0
        row = {"arm": arm, "n": n}
        row.update({f"R_any@{k}": sum(s[f"any@{k}"] for s in scores) / n for k in KS})
        row["R_any@5 95% CI"] = f"[{lo:.3f}, {hi:.3f}]"
        row["R_all@5"] = sum(s["all@5"] for s in scores) / n
        row["NDCG@5"] = sum(s["ndcg@5"] for s in scores) / n
        row["median chars@5"] = int(chars)
        row["R_any@5 per 1k tok"] = (hits5 / n) / (chars / 4 / 1000) if chars else None
        rows.append(row)
    return rows


PAIRS = (("D", "V"), ("D", "H"), ("V", "H"), ("D", "P"), ("P", "V"))
PAIRED_COLS = ["comparison", "dR_any@5 (primary)", "dR_all@5", "dR_any@1", "dNDCG@5 [95% CI]"]


def _mcnemar_cell(a: list[dict], b: list[dict], metric: str) -> str:
    only_a = sum(1 for x, y in zip(a, b) if x[metric] and not y[metric])
    only_b = sum(1 for x, y in zip(a, b) if y[metric] and not x[metric])
    p = mcnemar_exact(only_a, only_b)
    return f"{(only_b - only_a) / len(a):+.3f} ({only_a}/{only_b}, p={p:.3f}){'*' if p < 0.05 else ''}"


def _paired_rows(per_arm: dict[str, list[dict]]) -> list[dict]:
    """Paired arm comparisons on the same questions: McNemar exact on the binary session metrics,
    seeded bootstrap CI on the mean NDCG@5 delta. R_any@5 is the pre-registered gate metric."""
    rows = []
    if not per_arm["D"]:
        return rows
    for a, b in PAIRS:
        xs, ys = per_arm[a], per_arm[b]
        deltas = [y["ndcg@5"] - x["ndcg@5"] for x, y in zip(xs, ys)]
        lo, hi = bootstrap_ci(deltas)
        mean = sum(deltas) / len(deltas)
        rows.append(
            {
                "comparison": f"{a} -> {b}",
                "dR_any@5 (primary)": _mcnemar_cell(xs, ys, "any@5"),
                "dR_all@5": _mcnemar_cell(xs, ys, "all@5"),
                "dR_any@1": _mcnemar_cell(xs, ys, "any@1"),
                "dNDCG@5 [95% CI]": f"{mean:+.3f} [{lo:+.3f}, {hi:+.3f}]{'*' if lo > 0 or hi < 0 else ''}",
            }
        )
    return rows


def evaluate_longmemeval(
    spec: str, questions: list[Question], cfg, distiller, progress: Callable[[int, int], None] | None = None
) -> dict:
    name, model, truncate_dim, float_mode = parse_spec(spec)
    if float_mode:
        raise ValueError("+float ranks outside the store; LongMemEval needs the real ranking paths")
    embedder = make_embedder(name, model, truncate_dim, cfg)
    scored = [q for q in questions if q.scoreable]
    per_arm: dict[str, list[dict]] = {arm: [] for arm in ARMS}
    per_type: dict[str, dict[str, list[bool]]] = {}
    records: list[dict] = []
    for index, q in enumerate(scored, 1):
        result = evaluate_question(embedder, cfg, distiller, q)
        records.append({"qid": q.qid, "qtype": q.qtype, "arms": result})
        for arm in ARMS:
            per_arm[arm].append(result[arm])
            per_type.setdefault(q.qtype, {a: [] for a in ARMS})[arm].append(result[arm]["any@5"])
        if progress:
            progress(index, len(scored))
    type_rows = [
        {"question type": qtype, "n": len(arms["P"]), **{arm: sum(v) / len(v) for arm, v in arms.items()}}
        for qtype, arms in sorted(per_type.items())
    ]
    return {
        "backend": spec,
        "scored": len(scored),
        "abstention": len(questions) - len(scored),
        "summary": _summary_rows(per_arm),
        "types": type_rows,
        "paired": _paired_rows(per_arm),
        "records": records,
    }


SUMMARY_COLS = [
    "arm",
    "n",
    "R_any@1",
    "R_any@3",
    "R_any@5",
    "R_any@5 95% CI",
    "R_any@10",
    "R_all@5",
    "NDCG@5",
    "median chars@5",
    "R_any@5 per 1k tok",
]


def resolve_dataset(args: argparse.Namespace, cfg) -> Path | None:
    if args.lme_path:
        return args.lme_path
    if args.lme_download:
        return download(Path(cfg.data_dir) / "bench-cache")
    cached = Path(cfg.data_dir) / "bench-cache" / LME_FILE
    return cached if cached.exists() else None


def run_longmemeval(cfg, backends: list[str], args: argparse.Namespace) -> None:
    path = resolve_dataset(args, cfg)
    if path is None:
        print("[longmemeval] no dataset: pass --lme-path FILE or --lme-download (HF longmemeval-cleaned, MIT)")
        return
    questions, digest = load(path)
    scoreable = [q for q in questions if q.scoreable]
    sample = stratified_sample(scoreable, args.lme_limit)  # sample only what can be scored
    for label, run_cfg, subset in distiller_runs(cfg, sample, args.lme_llm):
        _report(backends, label, run_cfg, subset, args.lme_out)
    print(
        f"  dataset {path.name} sha256={digest}: {len(sample)} of {len(scoreable)} scoreable questions "
        f"(stratified; {len(questions) - len(scoreable)} abstention questions have no evidence to score)"
    )


def distiller_runs(cfg, sample: list[Question], llm_limit: int) -> list[tuple[str, object, list[Question]]]:
    """The D arm's distiller runs. Always the offline heuristic — never the configured distiller,
    which may be an LLM (``ENGRAM_DISTILLER=claude``) and would make one external call per session.
    An LLM run exists only on explicit opt-in (``--lme-llm N``): the configured LLM distiller, or
    ``claude`` when none is configured, on the first N sampled questions."""
    runs = [("heuristic", replace(cfg, distiller="heuristic"), sample)]
    if llm_limit > 0:
        llm = cfg.distiller if cfg.distiller in LLM_DISTILLERS else "claude"
        runs.append((llm, replace(cfg, distiller=llm), sample[:llm_limit]))
    return runs


def _report(backends: list[str], label: str, run_cfg, subset: list[Question], out: Path | None) -> None:
    distiller = get_distiller(run_cfg)
    for spec in backends:

        def progress(done: int, total: int, spec: str = spec) -> None:
            if done % 10 == 0 or done == total:
                print(f"  … {spec}/{label}: {done}/{total} questions", flush=True)

        try:
            result = evaluate_longmemeval(spec, subset, run_cfg, distiller, progress)
        except Exception as exc:
            print(f"[longmemeval skipped {spec}/{label}] {exc}")
            continue
        print(
            f"\nLongMemEval session retrieval — {spec}, distiller={label}: {result['scored']} scored questions "
            f"({result['abstention']} abstention excluded)\n"
        )
        print_rows(result["summary"], SUMMARY_COLS)
        print("\nR_any@5 by question type:\n")
        print_rows(result["types"], ["question type", "n", *ARMS])
        print(
            "\nPaired comparisons (positive d favours the second arm; McNemar exact (a/b discordant) on the binary"
            " metrics, seeded bootstrap on NDCG; * = p<0.05 / CI excludes 0; R_any@5 is the pre-registered gate):\n"
        )
        print_rows(result["paired"], PAIRED_COLS)
        if out is not None:
            with out.open("a", encoding="utf-8") as fh:
                for rec in result["records"]:
                    fh.write(json.dumps({"backend": spec, "distiller": label, **rec}) + "\n")
        print(
            "\n  P = mempalace-parity configuration (compare with its 96.6% R@5); V/D/H are engram's own."
            " chars@5 = what the model would pull for the top-5 units (≈ chars/4 tokens)."
        )
