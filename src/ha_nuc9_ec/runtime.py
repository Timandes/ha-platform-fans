"""Independent source lifetimes and application-loop integration hooks."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from .config import AppConfig, ConfigError, SourceConfig, load_config
from .controller import Controller, TemporaryFailure
from .model import CommandResult, Sample
from .sources.base import Collector, SourceReadError, SourceReader
from .sources.smart import SmartctlReader
from .sources.sysfs import SysfsReader


class ConstantReader:
    """Synthetic source used only with the explicit mock backend."""
    def __init__(self, source_id: str, celsius: float = 50):
        self.source_id, self.celsius = source_id, celsius

    def read(self) -> float:
        return self.celsius


class UnavailableReader:
    def __init__(self, source_id: str, error: str):
        self.source_id, self.error = source_id, error

    def read(self) -> float:
        raise SourceReadError(self.error)


def source_factory(sys_root: Path, *, mock: bool = False):
    def make(source_id: str, source: SourceConfig) -> SourceReader:
        if mock:
            return ConstantReader(source_id)
        try:
            if source.provider == 'smartctl':
                return SmartctlReader(source_id, source)
            return SysfsReader(source_id, source, sys_root)
        except (OSError, SourceReadError) as error:
            # Missing monitoring sources are telemetry failures, not a reason
            # to override BIOS/fixed policy or skip identity verification.
            return UnavailableReader(source_id, str(error))
    return make


class _Sources:
    def __init__(self, config: AppConfig, readers, publish):
        self.config = config
        self.samples: dict[str, Sample] = {}
        self.first = {key: asyncio.Event() for key in readers}
        self.publish = publish
        self.active = False
        self.tasks = [asyncio.create_task(Collector(config.sources[key], reader).run(self.put))
                      for key, reader in readers.items()]

    def put(self, sample: Sample):
        self.samples[sample.source_id] = sample
        self.first[sample.source_id].set()
        if self.active:
            self.publish(sample)

    async def prepare(self):
        required = set()
        if self.config.control.mode == 'override':
            for fan in (self.config.fans.cpufan, self.config.fans.sysfan):
                if fan.override.mode != 'fixed':
                    required.update(item.source for item in fan.override.inputs)
        async def first(key):
            source = self.config.sources[key]
            timeout = {'thermal_zone': .1, 'hwmon': .2, 'smartctl': 5}[source.provider]
            try:
                await asyncio.wait_for(self.first[key].wait(), timeout + .5)
            except TimeoutError:
                pass  # A skipped/missing first sample remains unavailable.
        await asyncio.gather(*(first(key) for key in required))

    async def stop(self):
        self.active = False
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


class Runtime:
    def __init__(self, controller: Controller, *, reader_factory: Callable[[str, SourceConfig], SourceReader]):
        self.controller = controller
        self.reader_factory = reader_factory
        self._sources: _Sources | None = None
        self._reload_lock = asyncio.Lock()
        self._changed = asyncio.Event()
        self.stop_event = asyncio.Event()
        self.reload_event = asyncio.Event()
        self.stop_reason = 'normal'
        self._started = False

    def _publish_sample(self, sample):
        self.controller.on_sample(sample)
        self._changed.set()

    async def _prepare(self, config):
        readers = {}
        try:
            for key, source in config.sources.items():
                if source.enabled:
                    readers[key] = await asyncio.to_thread(self.reader_factory, key, source)
        except BaseException:
            for reader in readers.values():
                close = getattr(reader, 'close', None)
                if close:
                    close()
            raise
        sources = _Sources(config, readers, self._publish_sample)
        try:
            await sources.prepare()
            return sources
        except BaseException:
            await sources.stop()
            raise

    async def start(self):
        if self._started:
            return
        self._sources = await self._prepare(self.controller.config)
        self._sources.active = True
        for sample in self._sources.samples.values():
            self._publish_sample(sample)
        await self.controller.start()
        self._started = True

    async def reload_file(self, path: Path, request_id: str) -> CommandResult:
        """Stage readers and required samples before the controller commit.

        device/MQTT fields are startup-only. Candidate preparation/validation
        failure leaves both current collectors and policy intact.
        """
        async def reload():
            async with self._reload_lock:
                candidate_sources = None
                try:
                    candidate = await asyncio.to_thread(load_config, path)
                    current = self.controller.config
                    if candidate.device != current.device or candidate.mqtt != current.mqtt:
                        raise ConfigError('device and MQTT settings require process restart')
                    candidate_sources = await self._prepare(candidate)
                    old_sources = self._sources
                    def commit():
                        if old_sources:
                            old_sources.active = False
                        self._sources = candidate_sources
                        candidate_sources.active = True
                        self._changed.set()
                    result = await self.controller.reload(candidate, request_id,
                                                          samples=candidate_sources.samples, commit=commit)
                    if result.ok and self._sources is candidate_sources:
                        candidate_sources = None
                        if old_sources:
                            await old_sources.stop()
                    return result
                except (ConfigError, OSError, SourceReadError) as error:
                    return CommandResult(request_id, False, self.controller.snapshot().revision, str(error))
                finally:
                    if candidate_sources:
                        await candidate_sources.stop()
        task = asyncio.create_task(reload())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Complete or reject the staged transaction before tearing down its
            # sources; a cancelled caller does not cancel mailbox confirmation.
            await task
            raise

    def request_stop(self, reason: str = 'normal'):
        self.stop_reason = reason
        self.stop_event.set()

    async def _cycles(self):
        while True:
            self._changed.clear()
            await self.controller.tick()
            try:
                await asyncio.wait_for(self._changed.wait(), .1)
            except TimeoutError:
                pass

    async def _rpm(self):
        while True:
            await asyncio.sleep(2)
            await self.controller.read_rpm()

    async def _reloads(self, config_path, on_reload):
        sequence = 0
        while True:
            await self.reload_event.wait()
            self.reload_event.clear()
            sequence += 1
            result = await self.reload_file(config_path, f'sighup-{sequence}')
            if on_reload:
                on_reload(result)
            if self.controller.snapshot().fault:
                raise TemporaryFailure(self.controller.snapshot().fault)

    async def run(self, *, config_path: Path | None = None, on_ready=None, on_reload=None):
        tasks = []
        try:
            await self.start()
            if on_ready:
                on_ready(self.controller.snapshot())
            tasks = [asyncio.create_task(self._cycles()), asyncio.create_task(self._rpm()),
                     asyncio.create_task(self.stop_event.wait())]
            if config_path:
                tasks.append(asyncio.create_task(self._reloads(config_path, on_reload)))
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except BaseException:
            self.stop_reason = 'runtime failure'
            raise
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.stop(self.stop_reason)

    async def stop(self, reason: str):
        async with self._reload_lock:
            try:
                await self.controller.stop(reason)
            finally:
                if self._sources:
                    await self._sources.stop()
