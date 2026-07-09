"""WorkQueue — a durable Command queue for detached per-memory processing.

A **Command** queue (one handler per item, at-least-once with retry + dead-letter),
**not** an Event bus / pub-sub — the name says so. It is the durable form of the
detached capture Job-claim: publish a WorkItem, a worker pulls a batch, processes
each, and acks / naks (retry) / terms (dead-letter). It survives dropped connections
and distiller outages — a nak'd item is redelivered later; one that exhausts
``queue_max_deliver`` lands in the dead-letter (``status='dead'``).

Separated Interface (Hexagonal port): the core depends on this ABC, never on a
concrete backend. The sole backend is ``inproc`` (a stdlib SQLite ``work_queue``,
zero deps); the port is retained so a future out-of-process backend can attach
behind it. Never used on the recall hot path.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class WorkItem:
    """One unit of detached work. Value Object / wire DTO.

    ``msg_id`` is the idempotency key (a content hash) — publishing the same
    ``msg_id`` twice is a no-op, so re-capture never duplicates work. Carry a
    pointer in ``ref`` (transcript/file path) or inline data in ``payload`` (JSON).
    ``attempts`` is the delivery count of the current lease (1 on first delivery).
    """

    stage: str
    project_key: str
    msg_id: str
    session_id: str = ""
    ref: str = ""
    payload: str = ""
    attempts: int = 0
    enqueued_at: float = 0.0


class Lease(ABC):
    """A claimed WorkItem plus its completion controls (the redelivery contract)."""

    item: WorkItem

    @abstractmethod
    def ack(self) -> None:
        """Mark the work done — remove it from the queue."""

    @abstractmethod
    def nak(self, delay: float | None = None) -> None:
        """Return the work for later retry (backoff). Dead-letters past ``queue_max_deliver``."""

    @abstractmethod
    def term(self) -> None:
        """Give up permanently — send straight to the dead-letter, no retry."""


class WorkQueue(ABC):
    """Port: a durable Command queue. The sole backend is inproc (SQLite ``work_queue``)."""

    @abstractmethod
    def publish(self, item: WorkItem) -> None:
        """Durably enqueue work; idempotent on ``item.msg_id``."""

    @abstractmethod
    def pull(self, stage: str, max_items: int = 16) -> list[Lease]:
        """Claim up to ``max_items`` due items for ``stage`` (leased; crash-safe)."""

    def close(self) -> None:
        """Release any resources. No-op for the in-process backend."""


def get_queue(cfg, store) -> WorkQueue:
    """Composition-root selection — Plugin pattern.

    One backend today: the in-process SQLite ``work_queue`` in ``store``. The port is
    retained as the seam so a future out-of-process backend can be added here without
    touching the core; a selector config key would return with it. Never on the hot path.
    """
    from core.adapters.inproc_queue import InprocQueue

    return InprocQueue(cfg, store)
