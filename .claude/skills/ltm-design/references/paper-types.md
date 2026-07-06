# Paper Types and Section Variants

claude-ltm is one project, so this skill does not carry SAK-style subject categories
(security / data / pipeline). Instead it distinguishes three **paper types** by their
centre of gravity. Choose the type before drafting; the six-section structure from
[`structure.md`](structure.md) then specialises to the type's emphasis.

If a paper genuinely spans two types, choose the primary one (the reason a reader opens
it) and let the other inform a section.

---

## empirical-evaluation

**What it is:** a benchmark-driven study. A claim about the system is stated as a
hypothesis and tested against measured data. The embedding-backend study (hash vs
bge-small vs bge-base; int8 vs float) is the canonical example.

**Centre of gravity:** § 3 Results and § 2.2 Variables. The paper lives or dies on the
numbers, so the statistical-rigour rules in [`statistics.md`](statistics.md) bind
hardest here.

**Section emphasis:**
- Introduction states **statistical hypotheses** (H1, H2, …), each answerable by Results.
- Method's *Variables & conditions* names independent/dependent/controlled variables
  precisely.
- Results carries the descriptive table **and** inferential treatment (intervals, effect
  sizes, the small-sample caveat).
- Discussion's *hypothesis evaluation* returns to each H and states supported/rejected.
- Appendix carries the raw `ltm eval` output and the reproducibility block.

**Do not:** report a number you did not reproduce this session; omit `n`; imply
significance the sample cannot bear.

---

## systems-design

**What it is:** an architecture / decision paper. The contribution is a design and its
rationale, not a measured comparison. The STM/LTM consolidation and MemoryBus design is
the canonical example: it argues that a durable **Command queue** (not an Event bus) is
the right abstraction for detached capture, and that consolidation should mirror
Active Systems Consolidation.

**Centre of gravity:** § 2 Method (the design itself) and § 4.2 Integration (why this
shape, against the alternatives).

**Section emphasis:**
- Introduction states **design claims** rather than statistical hypotheses ("The design
  claims a Command queue is correct here because…"). They are still explicit and the
  Discussion returns to each.
- Method describes the architecture, the POEAA roles, and the contracts, grounded at HEAD
  and against the pattern map in
  [`.claude/rules/02-architecture/01-poeaa-and-layers.md`](../../../rules/02-architecture/01-poeaa-and-layers.md).
  Use `/ltm-poeaa` to keep the pattern description accurate.
- Results may be **analytical rather than empirical** — a token/latency budget analysis,
  a complexity argument, a worked cache-placement calculation — but any quantitative
  claim still follows the statistics rules. Where a design claim *is* measurable, run
  `ltm eval` and report it.
- Discussion evaluates each design claim and names the rejected alternatives (the
  Event-bus option, float-rescore, an ANN index) with the property that ruled each out.
- Threats to validity here are design risks (from DESIGN.md's risk register), not
  sampling error.

**Do not:** describe the *current* architecture as if it were the decision — a systems
paper is a decision record, not a state description. If it reads "the system has three
layers, each of which…", it belongs in `docs/reference/`, not here.

---

## whitepaper

**What it is:** the integrative synthesis over the whole system — the claude-ltm
equivalent of `bitcoin.org/bitcoin.pdf`. It states the problem, the design, and the
measured evidence for the design's central claims in one self-contained document a
newcomer can read start to finish.

**Centre of gravity:** the whole arc. A whitepaper is broader than an
empirical-evaluation paper (it covers the design, not one comparison) and more
evidence-backed than a pure systems-design paper (it reports the benchmark, not only the
architecture).

**Section emphasis:**
- Introduction is the widest: the two-budget problem, the memory-research and
  retrieval-systems related work, the gap (always-on tools' standing cost; raw
  similarity's staleness), and a small set of load-bearing claims (the two budgets are
  distinct; recall belongs in a gated hook; model size is the recall lever).
- Method covers the full architecture (CQRS + Hexagonal, capture, recall, the cognitive
  lifecycle) at the level the claims need — not every module.
- Results reports the headline benchmark (the backend table) with the statistical
  treatment, plus the analytical budget arguments.
- Discussion integrates memory research honestly (including where claude-ltm diverges
  from the biology), names the limitations, and points at future directions.
- Appendices carry raw eval output, the config table, and the algorithms.

**Do not:** let breadth become vagueness. A whitepaper still grounds every claim at HEAD
and every number in `ltm eval`; it is longer, not looser.

---

## How to choose

1. **Is the contribution a measured comparison answering a hypothesis?** →
   empirical-evaluation.
2. **Is the contribution a design and its rationale?** → systems-design.
3. **Is the contribution the whole system, for a newcomer, with evidence?** →
   whitepaper.

If none fits, question whether it is a paper at all: operator-facing reference material
(setup, config docs) belongs in `docs/reference/` or the README; a plan to execute
belongs in `ltm-plan`; a code-impact analysis belongs in `ltm-analyse`.

---

## Length by type (guidance, not a rule)

| Type | Typical rendered length |
|---|---|
| empirical-evaluation | 300–600 lines |
| systems-design | 400–800 lines |
| whitepaper | 700–1,200 lines |

Longer is not better. The abstract, the hypotheses/claims, and the results table
together must convey the contribution without the reader touching an appendix.
