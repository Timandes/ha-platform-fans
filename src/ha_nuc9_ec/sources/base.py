from __future__ import annotations

import asyncio
import math
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Protocol

from ..config import SourceConfig
from ..model import Sample


class SourceReadError(RuntimeError):
    pass


class SourceReader(Protocol):
    source_id: str

    def read(self) -> float: ...


class SnapshotCache:
    def __init__(self) -> None:
        self._samples: dict[str, Sample] = {}
        self._lock = threading.Lock()

    def put(self, sample: Sample) -> None:
        with self._lock:
            self._samples[sample.source_id] = sample

    def snapshot(self) -> dict[str, Sample]:
        with self._lock:
            return dict(self._samples)


class Collector:
    def __init__(self, source: SourceConfig, reader: SourceReader) -> None:
        self.source = source
        self.reader = reader
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"source-{reader.source_id}")
        self._pending: Future[float] | None = None

    @property
    def timeout(self) -> float:
        return {"thermal_zone": 0.1, "hwmon": 0.2, "smartctl": 5.0}[self.source.provider]

    async def _read_once(self) -> Sample:
        if self._pending is None:
            self._pending = self._executor.submit(self.reader.read)
        future = self._pending
        try:
            value = await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)), self.timeout)
            self._pending = None
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise SourceReadError("reader returned an invalid temperature")
            return Sample(self.reader.source_id, float(value), time.monotonic(), None)
        except TimeoutError:
            return Sample(self.reader.source_id, None, time.monotonic(), "read timed out")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._pending = None
            message = str(error) or error.__class__.__name__
            return Sample(self.reader.source_id, None, time.monotonic(), message)

    async def run(self, publish: Callable[[Sample], None]) -> None:
        next_due = time.monotonic()
        try:
            while True:
                publish(await self._read_once())
                next_due += self.source.poll_interval
                now = time.monotonic()
                if next_due < now:
                    next_due = now + self.source.poll_interval
                await asyncio.sleep(max(0.0, next_due - now))
        finally:
            close = getattr(self.reader, "close", None)
            if close is not None:
                close()
            self._executor.shutdown(wait=False, cancel_futures=True)
