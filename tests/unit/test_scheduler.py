import asyncio
import threading
import time

import pytest

from ha_nuc9_ec.config import SourceConfig
from ha_nuc9_ec.model import Sample
from ha_nuc9_ec.sources.base import Collector, SnapshotCache


def config(provider="thermal_zone", interval="20ms"):
    selector = {"type": "x86_pkg_temp"} if provider == "thermal_zone" else {"device": "/dev/disk/by-id/x", "device_type": "sat"}
    return SourceConfig.model_validate({"kind": "cpu" if provider == "thermal_zone" else "smart", "provider": provider,
        "selector": selector, "poll_interval": interval, "stale_after": "6s" if provider == "smartctl" else "200ms"})


class Reader:
    def __init__(self, source_id, value=42.0):
        self.source_id = source_id
        self.value = value
        self.calls = 0

    def read(self):
        self.calls += 1
        return self.value


@pytest.mark.asyncio
async def test_collectors_publish_independently_when_smart_read_blocks():
    release = threading.Event()
    started = threading.Event()

    class Blocked(Reader):
        def read(self):
            self.calls += 1
            started.set()
            release.wait()
            return 35.0

    smart = Blocked("disk")
    cpu = Reader("cpu")
    samples = []
    class FastDeadlineCollector(Collector):
        @property
        def timeout(self):
            return 0.05

    tasks = [asyncio.create_task(FastDeadlineCollector(config("smartctl"), smart).run(samples.append)),
             asyncio.create_task(Collector(config(), cpu).run(samples.append))]
    try:
        assert await asyncio.to_thread(started.wait, 0.5)
        await asyncio.sleep(0.16)
        assert len([sample for sample in samples if sample.source_id == "cpu" and sample.error is None]) >= 3
        assert smart.calls == 1
        assert any(sample.source_id == "disk" and sample.error == "read timed out" for sample in samples)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        release.set()


@pytest.mark.asyncio
async def test_cancelling_collector_is_bounded_with_unfinished_read():
    release = threading.Event()
    started = threading.Event()

    class Blocked(Reader):
        def read(self):
            started.set()
            release.wait()
            return 1.0

    task = asyncio.create_task(Collector(config("smartctl"), Blocked("disk")).run(lambda _: None))
    assert await asyncio.to_thread(started.wait, 0.5)
    task.cancel()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 0.2)
    release.set()


def test_snapshot_cache_is_thread_safe_and_returns_copy():
    cache = SnapshotCache()
    first = Sample("cpu", 42.0, 10.0, None)
    cache.put(first)
    snapshot = cache.snapshot()
    snapshot.clear()
    assert cache.snapshot() == {"cpu": first}
