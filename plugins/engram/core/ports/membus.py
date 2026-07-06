"""MemoryBus — a durable Command queue for detached per-memory processing.

A **Command** queue (one handler per item, at-least-once with retry + dead-letter),
**not** an Event bus / pub-sub. It is the durable form of the existing detached
capture Job-claim: publish a WorkItem, a worker pulls a batch, processes each, and
acks / naks (retry) / terms (dead-letter). It survives dropped connections and
distiller outages — a nak'd item is redelivered later; one that exhausts
``bus_max_deliver`` lands in the dead-letter (``status='dead'``).

Separated Interface (Hexagonal port): the core depends on this ABC, never on a
concrete transport. Default adapter is ``inproc`` (a stdlib SQLite ``work_queue``,
zero deps); an opt-in ``nats`` adapter (JetStream) is selected by config and
**fails open** to ``inproc``. Never used on the recall hot path.
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
        """Return the work for later retry (backoff). Dead-letters past ``bus_max_deliver``."""

    @abstractmethod
    def term(self) -> None:
        """Give up permanently — send straight to the dead-letter, no retry."""


class MemoryBus(ABC):
    """Port: a durable Command queue. Implemented by inproc (SQLite) / nats adapters."""

    @abstractmethod
    def publish(self, item: WorkItem) -> None:
        """Durably enqueue work; idempotent on ``item.msg_id``."""

    @abstractmethod
    def pull(self, stage: str, max_items: int = 16) -> list[Lease]:
        """Claim up to ``max_items`` due items for ``stage`` (leased; crash-safe)."""

    def close(self) -> None:
        """Release any resources (a network connection). No-op for in-process adapters."""


def get_bus(cfg, store) -> MemoryBus:
    """Composition-root selection — Plugin pattern. Fails open to inproc.

    ``inproc`` (default) uses the SQLite ``work_queue`` in ``store``. ``nats`` uses
    the JetStream adapter; if ``nats-py`` is absent **or the server is unreachable**
    we fall back to ``inproc`` so a bus misconfiguration never breaks capture. The
    connectivity probe happens here (off the hot path) so callers get a working bus.
    """
    if getattr(cfg, "bus", "inproc") == "nats":
        bus = None
        try:
            from core.adapters.nats_bus import NatsBus

            bus = NatsBus(cfg, store)
            bus.connect()  # connect now so a dead server fails open here, not mid-capture
            _drain_inproc_into(bus, store)  # migrate items parked on inproc during a nats outage
            return bus
        except Exception:
            if bus is not None:
                bus.close()  # release the orphaned loop/connection before falling open
    from core.adapters.inproc_bus import InprocBus

    return InprocBus(cfg, store)


def _drain_inproc_into(bus: MemoryBus, store, cap: int = 500) -> int:
    """Migrate pending inproc SQLite work into the active (nats) backend on a switch.

    Items enqueued while nats was unreachable land in the inproc ``work_queue`` (fail-open);
    once nats is active, rescue pulls from JetStream and would never see them. Republish each
    (idempotent on ``msg_id``) then delete the inproc row. Fail-open **per item** so a publish
    error just leaves that row for next time — never loses or double-processes work.
    """
    migrated = 0
    for row in store.pending_work(cap):
        try:
            bus.publish(
                WorkItem(
                    stage=row["stage"],
                    project_key=row["project_key"],
                    msg_id=row["msg_id"],
                    session_id=row["session_id"] or "",
                    ref=row["ref"] or "",
                    payload=row["payload"] or "",
                )
            )
        except Exception:
            continue  # leave the row on inproc; try again on the next bus construction
        store.ack_work(row["msg_id"])  # remove the migrated inproc row
        migrated += 1
    return migrated
