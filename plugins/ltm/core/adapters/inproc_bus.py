"""In-process MemoryBus — a durable Command queue backed by the SQLite ``work_queue``.

The zero-dependency default (and the test double for the ``nats`` adapter). Publish
is an idempotent INSERT OR IGNORE; pull leases a batch (crash-safe via lease expiry);
ack removes it, nak reschedules with backoff, and a delivery count past
``bus_max_deliver`` dead-letters. All persistence lives in ``Store`` (Repository) —
this adapter is a thin shell over its work-queue methods.
"""

from __future__ import annotations

from core.ports.membus import Lease, MemoryBus, WorkItem
from core.store import Store


class InprocLease(Lease):
    def __init__(self, store: Store, item: WorkItem, cfg) -> None:
        self.item = item
        self._store = store
        self._cfg = cfg

    def ack(self) -> None:
        self._store.ack_work(self.item.msg_id)

    def nak(self, delay: float | None = None) -> None:
        # Exhausted deliveries -> dead-letter (the DLQ), never an infinite retry loop.
        if self.item.attempts >= self._cfg.bus_max_deliver:
            self._store.dead_work(self.item.msg_id)
            return
        if delay is None:
            sched = self._cfg.bus_backoff
            delay = sched[min(self.item.attempts - 1, len(sched) - 1)] if sched else 0.0
        self._store.nak_work(self.item.msg_id, delay)

    def term(self) -> None:
        self._store.dead_work(self.item.msg_id)


class InprocBus(MemoryBus):
    def __init__(self, cfg, store: Store) -> None:
        self._cfg = cfg
        self._store = store

    def publish(self, item: WorkItem) -> None:
        self._store.enqueue_work(
            msg_id=item.msg_id,
            stage=item.stage,
            project_key=item.project_key,
            session_id=item.session_id,
            ref=item.ref,
            payload=item.payload,
        )

    def pull(self, stage: str, max_items: int = 16) -> list[Lease]:
        rows = self._store.claim_work(stage, max_items, lease_ttl=self._cfg.lease_ttl)
        leases: list[Lease] = []
        for row in rows:
            item = WorkItem(
                stage=row["stage"],
                project_key=row["project_key"],
                msg_id=row["msg_id"],
                session_id=row["session_id"] or "",
                ref=row["ref"] or "",
                payload=row["payload"] or "",
                attempts=row["attempts"] + 1,  # claim_work incremented the stored count
                enqueued_at=row["enqueued_at"] or 0.0,
            )
            leases.append(InprocLease(self._store, item, self._cfg))
        return leases
