"""The consolidation ("sleep") pipeline — replay / refine (+ retention score).

Mirrors active systems consolidation: a discrete, off-hot-path pass that promotes
rehearsed short-term facts (replay), and prunes low-importance ones (refine, SHY).
What it keeps vs forgets is decided by the pure retention score in scoring.py
(design section 3A). Retrieval-affecting steps are gated default-off until eval-tuned.
"""
