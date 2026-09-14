import asyncio
import signal
import sys
import threading
from pathlib import Path

import pytest
import yaml

from ha_nuc9_ec.config import load_config
from ha_nuc9_ec.controller import Controller
from ha_nuc9_ec.hardware.mock import MockBackend
from ha_nuc9_ec.runtime import Runtime

ROOT = Path(__file__).parents[2]


def write_config(path, **updates):
    data = yaml.safe_load((ROOT / 'config/example.yaml').read_text())
    data['mqtt']['enabled'] = False
    for key, value in updates.items():
        data[key] = value
    path.write_text(yaml.safe_dump(data))
    return load_config(path)


class Reader:
    def __init__(self, source_id, entered=None, release=None):
        self.source_id = source_id
        self.calls = 0
        self.entered, self.release = entered, release

    def read(self):
        self.calls += 1
        if self.entered:
            self.entered.set()
            assert self.release.wait(3)
        return 50.0


@pytest.mark.asyncio
async def test_slow_rpm_and_smart_do_not_block_cpu_sampling(tmp_path):
    config = write_config(tmp_path / 'config.yaml')
    config.sources['disk'].enabled = True
    entered, release = threading.Event(), threading.Event()
    rpm_entered, rpm_release = threading.Event(), threading.Event()
    class SlowRPM(MockBackend):
        def read_rpm(self):
            rpm_entered.set()
            assert rpm_release.wait(3)
            return super().read_rpm()
    readers = {'cpu_package': Reader('cpu_package'), 'pch': Reader('pch'),
               'disk': Reader('disk', entered, release)}
    c = Controller(config, SlowRPM(), asyncio.get_running_loop().time)
    runtime = Runtime(c, reader_factory=lambda key, source: readers[key])
    try:
        await runtime.start()
        assert await asyncio.to_thread(entered.wait, 1)
        rpm = asyncio.create_task(c.read_rpm())
        assert await asyncio.to_thread(rpm_entered.wait, 1)
        # Bounded wait for real 100ms sampling, independent of both blocked I/O.
        async with asyncio.timeout(1):
            while readers['cpu_package'].calls < 3:
                await asyncio.sleep(.02)
        assert not release.is_set() and not rpm_release.is_set()
        rpm_release.set()
        await rpm
    finally:
        release.set()
        rpm_release.set()
        await runtime.stop('SIGTERM')


@pytest.mark.asyncio
async def test_runtime_reload_rejects_bad_file_and_startup_fields_then_swaps_sources(tmp_path):
    path = tmp_path / 'config.yaml'
    config = write_config(path)
    c = Controller(config, MockBackend(), asyncio.get_running_loop().time)
    runtime = Runtime(c, reader_factory=lambda key, source: Reader(key))
    await runtime.start()
    try:
        await c.change({'fans.cpufan.override.fixed.duty_percent': 65}, 'edit')
        path.write_text('invalid: true')
        assert not (await runtime.reload_file(path, 'bad')).ok
        assert c.config.fans.cpufan.override.fixed.duty_percent == 65
        write_config(path, device={'id': 'other'})
        assert not (await runtime.reload_file(path, 'identity')).ok
        write_config(path)
        data = yaml.safe_load(path.read_text())
        data['sources']['cpu_package']['poll_interval'] = '200ms'
        path.write_text(yaml.safe_dump(data))
        result = await runtime.reload_file(path, 'good')
        assert result.ok and result.revision == 2
        assert c.config.fans.cpufan.override.fixed.duty_percent == 40
        assert c.config.sources['cpu_package'].poll_interval == .2
    finally:
        await runtime.stop('SIGTERM')


@pytest.mark.asyncio
async def test_cli_mock_handles_sigterm_and_sighup_without_hardware(tmp_path):
    path = tmp_path / 'config.yaml'
    write_config(path)
    process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'ha_nuc9_ec.cli', 'run', '--backend', 'mock', '--config', str(path), '--lock-path', str(tmp_path / 'lock'), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        async with asyncio.timeout(5):
            while True:
                line = await process.stdout.readline()
                assert line, (await process.stderr.read()).decode()
                if b'"event": "ready"' in line:
                    break
            process.send_signal(signal.SIGHUP)
            while b'"event": "reload"' not in await process.stdout.readline():
                pass
            process.send_signal(signal.SIGTERM)
            stdout, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode()
        assert b'"backend": "mock"' in stdout
        assert b'"close"' in stdout
        assert b'/dev/port' not in stdout + stderr
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.asyncio
async def test_cli_invalid_config_exits_permanent_before_device_access(tmp_path):
    path = tmp_path / 'bad.yaml'
    path.write_text('invalid: true')
    process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'ha_nuc9_ec.cli', 'run', '--backend', 'linux', '--config', str(path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await process.communicate()
    assert process.returncode == 78
    assert b'error' in stderr and b'/dev/port' not in stderr


@pytest.mark.asyncio
async def test_reload_failed_required_candidate_keeps_collecting_old_sources(tmp_path):
    path = tmp_path / 'config.yaml'
    config = write_config(path)
    old_reader = Reader('cpu_package')
    def reader_factory(key, source):
        if getattr(source.selector, 'type', None) == 'missing':
            from ha_nuc9_ec.runtime import UnavailableReader
            return UnavailableReader(key, 'missing sensor')
        return old_reader if key == 'cpu_package' else Reader(key)
    c = Controller(config, MockBackend(), asyncio.get_running_loop().time)
    runtime = Runtime(c, reader_factory=reader_factory)
    await runtime.start()
    try:
        data = yaml.safe_load(path.read_text())
        data['control']['mode'] = 'override'
        data['sources']['cpu_package']['selector']['type'] = 'missing'
        path.write_text(yaml.safe_dump(data))
        result = await runtime.reload_file(path, 'unavailable')
        assert not result.ok and c.snapshot().applied_mode == 'bios'
        assert c.snapshot().fault is None
        calls = old_reader.calls
        async with asyncio.timeout(1):
            while old_reader.calls <= calls:
                await asyncio.sleep(.02)
        assert c.snapshot().sources['cpu_package'].error is None
    finally:
        await runtime.stop('SIGTERM')


@pytest.mark.asyncio
async def test_mock_single_instance_lock_and_release(tmp_path):
    path = tmp_path / 'config.yaml'
    write_config(path)
    command = [sys.executable, '-m', 'ha_nuc9_ec.cli', 'run', '--backend', 'mock', '--config', str(path), '--lock-path', str(tmp_path / 'lock')]
    first = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        async with asyncio.timeout(5):
            assert b'"event": "ready"' in await first.stdout.readline()
            second = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, error = await second.communicate()
            assert second.returncode == 78 and b'lock' in error
            first.send_signal(signal.SIGTERM)
            await first.communicate()
            assert first.returncode == 0
            third = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                assert b'"event": "ready"' in await third.stdout.readline()
                third.send_signal(signal.SIGTERM)
                await third.communicate()
                assert third.returncode == 0
            finally:
                if third.returncode is None:
                    third.kill()
                    await third.wait()
    finally:
        if first.returncode is None:
            first.kill()
            await first.wait()


@pytest.mark.asyncio
async def test_duplicate_reload_does_not_stop_the_active_collectors(tmp_path):
    path = tmp_path / 'config.yaml'
    config = write_config(path)
    created = []
    def factory(key, source):
        reader = Reader(key)
        created.append(reader)
        return reader
    c = Controller(config, MockBackend(), asyncio.get_running_loop().time)
    runtime = Runtime(c, reader_factory=factory)
    await runtime.start()
    try:
        first = await runtime.reload_file(path, 'reload-id')
        active_cpu = created[-2]
        duplicate = await runtime.reload_file(path, 'reload-id')
        assert duplicate == first
        calls = active_cpu.calls
        async with asyncio.timeout(.5):
            while active_cpu.calls <= calls:
                await asyncio.sleep(.02)
        assert c.snapshot().fault is None
    finally:
        await runtime.stop('SIGTERM')


def test_cli_startup_communication_failure_exits_temporary_without_recovery_writes(tmp_path, monkeypatch, capsys):
    from ha_nuc9_ec.cli import main
    from ha_nuc9_ec.hardware.base import HardwareError
    path = tmp_path / 'config.yaml'
    write_config(path)
    def busy(self):
        raise HardwareError('mailbox busy timeout')
    monkeypatch.setattr(MockBackend, 'probe', busy)
    code = main(['run', '--backend', 'mock', '--config', str(path), '--lock-path', str(tmp_path / 'lock')])
    output = capsys.readouterr()
    assert code == 75
    assert 'busy' in output.err
    assert '"close"' in output.out
    assert '"set_duty"' not in output.out and '"restore_bios"' not in output.out


@pytest.mark.asyncio
@pytest.mark.parametrize('permanent', [False, True])
async def test_stop_racing_shielded_rpm_failure_propagates_terminal_status_after_cleanup(tmp_path, permanent):
    from ha_nuc9_ec.controller import PermanentFailure, TemporaryFailure
    from ha_nuc9_ec.hardware.base import HardwareError, PreflightError
    hardware_error = PreflightError if permanent else HardwareError
    terminal_error = PermanentFailure if permanent else TemporaryFailure
    config = write_config(tmp_path / 'config.yaml')
    config.runtime.shutdown_action = 'restore_bios'
    entered, release = threading.Event(), threading.Event()
    stopping = asyncio.Event()
    class FailingRPM(MockBackend):
        def read_rpm(self):
            entered.set()
            assert release.wait(3), 'test must release blocked RPM'
            raise hardware_error('communication failed during SIGTERM')
    class ObservedController(Controller):
        async def stop(self, reason):
            stopping.set()
            await super().stop(reason)
    class ImmediateRPMRuntime(Runtime):
        async def _rpm(self):
            await self.controller.read_rpm()
    backend = FailingRPM()
    c = ObservedController(config, backend, asyncio.get_running_loop().time)
    runtime = ImmediateRPMRuntime(c, reader_factory=lambda key, source: Reader(key))
    task = asyncio.create_task(runtime.run())
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        runtime.request_stop('SIGTERM')
        await asyncio.wait_for(stopping.wait(), 1)
        # run is now cleaning up, and the original RPM waiter was cancelled;
        # the shielded mailbox transaction must still determine terminal status.
        release.set()
        with pytest.raises(terminal_error, match='communication failed') as caught:
            await asyncio.wait_for(task, 1)
        assert caught.value.exit_code == (78 if permanent else 75)
        assert c.snapshot().state == 'fault' and c.snapshot().applied_mode == 'unknown'
        assert backend.operations == [('probe',), ('restore_bios',), ('close',)]
        assert all(source.done() for source in runtime._sources.tasks)
    finally:
        release.set()
        if not task.done():
            runtime.request_stop('SIGTERM')
            await asyncio.gather(task, return_exceptions=True)
