"""Serialized hardware transactions and atomic effective configuration.

All public async methods run on one asyncio loop. Collector threads publish via
loop.call_soon_threadsafe; observers must enqueue their work without blocking.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import re
from collections import OrderedDict, deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from pydantic import ValidationError

from .config import AppConfig, ConfigError, apply_changes
from .hardware.base import Backend, HardwareError, PreflightError
from .model import CommandResult, DutyPair, Sample, StateSnapshot
from .policy import DownshiftGate, UpshiftGate, SourceUnavailable, calculate


class TemporaryFailure(RuntimeError):
    exit_code = 75


class PermanentFailure(RuntimeError):
    exit_code = 78


def _same_payload(left: Any, right: Any) -> bool:
    """Compare request structure without Python's bool/int/float equivalence."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        keys = {(type(key), key) for key in left}
        return keys == {(type(key), key) for key in right} and all(
            _same_payload(value, right[key]) for key, value in left.items())
    if isinstance(left, (tuple, list)):
        return len(left) == len(right) and all(_same_payload(a, b) for a, b in zip(left, right))
    return left == right


class Controller:
    def __init__(self, config: AppConfig, backend: Backend, clock: Callable[[], float]):
        self._config = config.model_copy(deep=True)
        self.backend = backend
        self.clock = clock
        self._samples: dict[str, Sample] = {}
        self._last_success: dict[str, float] = {}
        self._state = 'starting'
        self._requested = config.control.mode
        self._applied = 'unknown'
        self._revision = 0
        self._duty: DutyPair | None = None
        self._rpm: tuple[int, int, int] | None = None
        self._fault: str | None = None
        self._failure_type: type[TemporaryFailure | PermanentFailure] = TemporaryFailure
        self._last_cycle: float | None = None
        self._identified = False
        self._closed = False
        self._gate = DownshiftGate(config.control.decrease_delay)
        self._upshift = UpshiftGate()
        self._commands: deque = deque()
        self._periodic = None
        self._rpm_job = None
        self._worker: asyncio.Task | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='ec-mailbox')
        self._results: OrderedDict[str, tuple[Any, CommandResult]] = OrderedDict()
        self._observers: list[Callable[[StateSnapshot], None]] = []

    @property
    def config(self) -> AppConfig:
        return self._config.model_copy(deep=True)

    def subscribe(self, observer: Callable[[StateSnapshot], None]) -> Callable[[], None]:
        """Observe confirmed state/progress; returns an unsubscribe callback."""
        self._observers.append(observer)
        return lambda: self._observers.remove(observer)

    def _publish(self) -> None:
        for observer in tuple(self._observers):
            try:
                observer(self.snapshot())
            except Exception:
                logging.exception('state observer failed')

    def snapshot(self) -> StateSnapshot:
        sources = {}
        now = self.clock()
        for key, source in self._config.sources.items():
            if not source.enabled:
                continue
            sample = self._samples.get(key, Sample(key, None, now, 'not sampled'))
            if sample.error is None and now - sample.read_at > source.stale_after:
                sample = Sample(key, sample.celsius, sample.read_at, 'sample is stale')
            sources[key] = sample
        return StateSnapshot(self._state, self._requested, self._applied, self._revision,
                             self._duty, self._rpm, sources, self._fault, self._last_cycle,
                             self._config.model_dump(mode='json'), copy.deepcopy(self._last_success))

    def on_sample(self, sample: Sample) -> None:
        if self._duty is not None and self._config.control.mode == 'override':
            self._upshift.observe(self._config, sample, self.clock(), self.backend.duty_bounds, self._duty)
        self._samples[sample.source_id] = sample
        if sample.error is None and sample.celsius is not None:
            self._last_success[sample.source_id] = sample.read_at

    def raise_if_faulted(self) -> None:
        """Propagate the terminal failure category after pending work drains."""
        if self._fault is not None:
            raise self._failure_type(self._fault)

    def _ensure_active(self) -> None:
        self.raise_if_faulted()
        if self._closed or self._state == 'stopping':
            raise TemporaryFailure('controller is stopping')

    async def _hardware(self, method, *args):
        try:
            return await asyncio.get_running_loop().run_in_executor(self._executor, method, *args)
        except PreflightError as error:
            self._mark_fault(str(error), unknown=True, failure_type=PermanentFailure)
            raise PermanentFailure(str(error)) from error
        except HardwareError as error:
            self._mark_fault(str(error), unknown=True)
            raise TemporaryFailure(str(error)) from error

    def _mark_fault(self, message: str, *, unknown: bool = False,
                    failure_type: type[TemporaryFailure | PermanentFailure] = TemporaryFailure) -> None:
        self._fault = message
        self._failure_type = failure_type
        self._state = 'fault'
        if unknown:
            self._applied = 'unknown'
            self._duty = None
            self._rpm = None
        self._publish()

    async def _sensor_failure(self, error: SourceUnavailable) -> None:
        # Called only for a required automatic input while targeting override.
        if self._identified and self._fault is None:
            upper = self.backend.duty_bounds[1]
            pair = DutyPair(upper, upper)
            await self._hardware(self.backend.set_duty, pair)
            self._duty, self._applied = pair, 'override'
        self._mark_fault(str(error))
        raise TemporaryFailure(str(error)) from error

    async def _submit(self, kind: str, operation):
        future = asyncio.get_running_loop().create_future()
        future.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        if kind == 'command':
            # Commands are barriers. In-flight operations finish, pending periodic
            # work is obsolete; never coalesce command confirmations.
            if self._periodic is not None:
                for waiter in self._periodic[1]:
                    waiter.set_result(None)
                self._periodic = None
            self._commands.append((operation, [future]))
        elif kind == 'periodic':
            if self._periodic is None:
                self._periodic = (operation, [future])
            else:
                self._periodic[1].append(future)
        else:
            if self._rpm_job is None:
                self._rpm_job = (operation, [future])
            else:
                self._rpm_job[1].append(future)
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._drain())
        # Cancelling a caller cannot interrupt an EC transaction or its commit.
        return await asyncio.shield(future)

    async def _drain(self):
        while self._commands or self._periodic is not None or self._rpm_job is not None:
            if self._commands:
                operation, waiters = self._commands.popleft()
            elif self._periodic is not None:
                operation, waiters = self._periodic
                self._periodic = None
            else:
                operation, waiters = self._rpm_job
                self._rpm_job = None
            try:
                result = await operation()
            except Exception as error:
                for waiter in waiters:
                    waiter.set_exception(error)
            else:
                for waiter in waiters:
                    waiter.set_result(result)

    def _completed(self):
        self._last_cycle = self.clock()
        self._publish()

    async def start(self) -> None:
        async def operation():
            self._ensure_active()
            if self._identified:
                return
            await self._hardware(self.backend.probe)
            self._identified = True
            await self._apply(self._config, self._samples)
            self._completed()
        await self._submit('command', operation)

    async def _apply(self, candidate: AppConfig, samples: dict[str, Sample]) -> None:
        self._requested = candidate.control.mode
        if candidate.control.mode == 'bios':
            if self._applied != 'bios':
                await self._hardware(self.backend.restore_bios)
            self._duty, self._applied, self._state = None, 'bios', 'bios'
            self._gate = DownshiftGate(candidate.control.decrease_delay)
            self._upshift = UpshiftGate()
        else:
            try:
                desired = calculate(candidate, samples, self.clock(), self.backend.duty_bounds)
            except SourceUnavailable as error:
                await self._sensor_failure(error)
            # A human request gets its own confirmed whole-pair transaction even
            # when its mode or target equals the previous confirmed value.
            await self._hardware(self.backend.set_duty, desired)
            self._duty, self._applied, self._state = desired, 'override', 'override'
            self._gate = DownshiftGate(candidate.control.decrease_delay)
            self._upshift = UpshiftGate()
            self._gate.apply(desired, self.clock(), immediate=True)

    async def _configuration_command(self, make_candidate, request_id: str, payload, samples=None, commit=None):
        async def operation():
            previous = self._results.get(request_id)
            if previous is not None:
                if _same_payload(previous[0], payload):
                    return previous[1]
                return CommandResult(request_id, False, self._revision, 'request_id reused with different payload')
            try:
                self._ensure_active()
                if not self._identified:
                    raise ConfigError('controller has not started')
                candidate = make_candidate()
                if candidate.device != self._config.device or candidate.mqtt != self._config.mqtt:
                    raise ConfigError('device and MQTT settings require process restart')
                await self._apply(candidate, self._samples if samples is None else samples)
                previous_sources = self._config.sources
                self._config = candidate
                if samples is not None:
                    self._samples = dict(samples)
                    self._last_success = {
                        key: sample.read_at if sample.error is None and sample.celsius is not None else self._last_success[key]
                        for key, sample in samples.items()
                        if ((sample.error is None and sample.celsius is not None) or
                            (key in self._last_success and key in previous_sources and
                             key in candidate.sources and previous_sources[key] == candidate.sources[key]))
                    }
                if commit is not None:
                    commit()
                self._revision += 1
                result = CommandResult(request_id, True, self._revision)
                self._completed()
            except (ConfigError, ValidationError, TemporaryFailure, PermanentFailure) as error:
                result = CommandResult(request_id, False, self._revision, str(error))
            self._results[request_id] = (payload, result)
            # Bound untrusted request IDs. Idempotence is scoped to this process
            # and its most recent 256 completed request IDs.
            if len(self._results) > 256:
                self._results.popitem(last=False)
            return result
        return await self._submit('command', operation)

    async def change(self, changes: dict[str, object], request_id: str) -> CommandResult:
        changes = copy.deepcopy(changes)
        def candidate():
            resolved = {}
            for path, value in changes.items():
                match = re.fullmatch(
                    r"fans\.(cpufan|sysfan)\.override\.inputs\.source:([0-9a-f]+)\."
                    r"(custom\.(?:minimum_temperature_c|minimum_duty_percent|duty_increment_percent_per_c)|boost_above_c)",
                    path)
                if match is None:
                    resolved[path] = value
                    continue
                try:
                    source_id = bytes.fromhex(match.group(2)).decode('utf-8')
                except (ValueError, UnicodeDecodeError) as error:
                    raise ConfigError(f'{path}: invalid source identity') from error
                inputs = getattr(self._config.fans, match.group(1)).override.inputs
                indices = [index for index, item in enumerate(inputs) if item.source == source_id]
                if len(indices) != 1:
                    raise ConfigError(f'{path}: source is no longer an existing fan input')
                resolved[f'fans.{match.group(1)}.override.inputs.{indices[0]}.{match.group(3)}'] = value
            return apply_changes(self._config, resolved)
        return await self._configuration_command(candidate, request_id, ('change', changes))

    async def reload(self, config: AppConfig, request_id: str, *, samples: dict[str, Sample] | None = None,
                     commit: Callable[[], None] | None = None) -> CommandResult:
        """Replace file baseline. Runtime passes prepared samples and a no-fail,
        synchronous collector-swap callback, executed inside the commit barrier.
        """
        payload = config.model_dump(mode='python')
        def candidate():
            result = AppConfig.model_validate(payload, context={'normalized_duration': True})
            if result.control.mode == 'override':
                # Reload preparation failure preserves the running policy; unlike
                # loss of a currently required source, it must not fault/boost.
                try:
                    calculate(result, self._samples if samples is None else samples, self.clock(), self.backend.duty_bounds)
                except SourceUnavailable as error:
                    raise ConfigError(f'reload candidate: {error}') from error
            return result
        return await self._configuration_command(candidate, request_id, ('reload', payload), samples, commit)

    async def tick(self) -> None:
        async def operation():
            self._ensure_active()
            if not self._identified:
                raise TemporaryFailure('controller has not started')
            if self._config.control.mode == 'override':
                try:
                    desired = self._upshift.apply(self._config, self._samples, self.clock(), self.backend.duty_bounds, self._duty)
                except SourceUnavailable as error:
                    await self._sensor_failure(error)
                target = self._gate.apply(desired, self.clock())
                if target != self._duty:
                    await self._hardware(self.backend.set_duty, target)
                    self._upshift.confirmed(self._duty, target)
                    self._duty = target
            self._completed()
        await self._submit('periodic', operation)

    async def read_rpm(self) -> tuple[int, int, int]:
        async def operation():
            self._ensure_active()
            if not self._identified:
                raise TemporaryFailure('controller has not started')
            self._rpm = await self._hardware(self.backend.read_rpm)
            self._publish()
            return self._rpm
        return await self._submit('rpm', operation)

    async def stop(self, reason: str) -> None:
        async def operation():
            if self._closed:
                return
            normal = self._fault is None and reason in {'SIGTERM', 'SIGINT', 'normal'}
            try:
                if normal:
                    self._state = 'stopping'
                    if self._identified and self._config.runtime.shutdown_action == 'restore_bios':
                        await self._hardware(self.backend.restore_bios)
                        self._duty, self._applied = None, 'bios'
                elif self._fault is None:
                    self._mark_fault(reason)
            finally:
                await self._hardware(self.backend.close)
                self._closed = True
                self._executor.shutdown(wait=False)
                self._publish()
        await self._submit('command', operation)
