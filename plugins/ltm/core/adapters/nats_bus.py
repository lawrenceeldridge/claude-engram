"""NATS JetStream MemoryBus adapter (opt-in) — durable, cross-process work queue.

Selected by ``bus=nats``; the core never imports ``nats`` (lazy, here only). Bridges
the async ``nats-py`` client to the synchronous MemoryBus port through a dedicated
event loop per bus instance — fine on the detached write path, never the recall hot
path. ``get_bus`` falls open to the inproc adapter when ``nats-py`` is absent or the
server is unreachable.

JetStream layout: one work-queue stream over ``ltm.>``; a per-stage durable pull
consumer filters ``ltm.*.<stage>``; publish sets ``Nats-Msg-Id`` = the WorkItem's
content hash for server-side dedup. ack removes the message; nak reschedules with the
configured backoff; term (or exceeding ``bus_max_deliver``) drops it.
"""

from __future__ import annotations

import asyncio

from core.ports.membus import Lease, MemoryBus, WorkItem


class NatsLease(Lease):
    def __init__(self, bus: NatsBus, msg, item: WorkItem) -> None:
        self.item = item
        self._bus = bus
        self._msg = msg

    def ack(self) -> None:
        self._bus._run(self._msg.ack())

    def nak(self, delay: float | None = None) -> None:
        if delay is None:
            sched = self._bus._cfg.bus_backoff
            delay = sched[min(self.item.attempts - 1, len(sched) - 1)] if sched else 0.0
        self._bus._run(self._msg.nak(delay=delay))

    def term(self) -> None:
        self._bus._run(self._msg.term())


class NatsBus(MemoryBus):
    def __init__(self, cfg, store=None) -> None:
        import nats  # lazy — absence raises here and get_bus falls open to inproc

        self._nats = nats
        self._cfg = cfg
        self._loop = asyncio.new_event_loop()
        self._nc = None
        self._js = None

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    async def _connect(self) -> None:
        if self._nc is not None:
            return
        from nats.js.api import RetentionPolicy, StorageType, StreamConfig

        self._nc = await self._nats.connect(self._cfg.nats_url, max_reconnect_attempts=1, connect_timeout=2)
        self._js = self._nc.jetstream()
        try:
            await self._js.add_stream(
                StreamConfig(
                    name=self._cfg.nats_stream,
                    subjects=["ltm.>"],
                    storage=StorageType.FILE,
                    retention=RetentionPolicy.WORK_QUEUE,
                    duplicate_window=300.0,  # 5-min dedup window on Nats-Msg-Id
                )
            )
        except Exception:
            # add_stream fails if our stream already exists (fine) OR if another stream
            # already owns the `ltm.>` subjects (a real misconfig). Confirm ours is there;
            # if not, re-raise so get_bus falls open to inproc rather than silently no-op.
            await self._js.stream_info(self._cfg.nats_stream)

    def _ensure(self) -> None:
        self._run(self._connect())

    def connect(self) -> None:
        """Eagerly connect + ensure the stream — used by get_bus to fail open on a dead server."""
        self._ensure()

    def publish(self, item: WorkItem) -> None:
        self._ensure()
        subject = f"ltm.{item.project_key}.{item.stage}"
        headers = {"Nats-Msg-Id": item.msg_id} if item.msg_id else None
        self._run(self._js.publish(subject, (item.payload or "").encode(), headers=headers))

    def pull(self, stage: str, max_items: int = 16) -> list[Lease]:
        self._ensure()
        return self._run(self._pull(stage, max_items))

    async def _pull(self, stage: str, max_items: int) -> list[Lease]:
        from nats.js.api import AckPolicy, ConsumerConfig

        psub = await self._js.pull_subscribe(
            f"ltm.*.{stage}",
            durable=stage,
            config=ConsumerConfig(
                ack_policy=AckPolicy.EXPLICIT,
                max_deliver=self._cfg.bus_max_deliver,
                ack_wait=float(self._cfg.lease_ttl),
            ),
        )
        try:
            msgs = await psub.fetch(max_items, timeout=1)
        except Exception:
            msgs = []  # no messages ready within the timeout
        leases: list[Lease] = []
        for msg in msgs:
            payload = msg.data.decode() if msg.data else ""
            parts = msg.subject.split(".")
            item = WorkItem(
                stage=stage,
                project_key=parts[1] if len(parts) >= 3 else "",
                msg_id=(msg.headers or {}).get("Nats-Msg-Id", ""),
                payload=payload,
                attempts=msg.metadata.num_delivered,
            )
            leases.append(NatsLease(self, msg, item))
        return leases

    def close(self) -> None:
        if self._nc is not None:
            try:
                self._run(self._nc.close())  # close (not drain) — our ops are already awaited
            except Exception:
                pass
            self._nc = None
        try:
            self._loop.close()
        except Exception:
            pass
