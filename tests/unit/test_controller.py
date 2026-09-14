import asyncio
import threading

import pytest

from ha_nuc9_ec.config import apply_changes
from ha_nuc9_ec.controller import Controller, TemporaryFailure
from ha_nuc9_ec.hardware.mock import MockBackend
from ha_nuc9_ec.model import DutyPair, Sample


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def backend():
    return MockBackend()


@pytest.fixture
def controller(example_config, backend, clock):
    control = Controller(example_config, backend, clock)
    control.on_sample(Sample('cpu_package', 50, clock(), None))
    control.on_sample(Sample('pch', 50, clock(), None))
    return control


@pytest.mark.asyncio
async def test_start_confirms_bios_and_first_override_writes_complete_pair(controller, backend):
    await controller.start()
    assert controller.snapshot().applied_mode == 'bios'
    assert controller.snapshot().duty is None
    result = await controller.change({'control.mode': 'override'}, 'start')
    assert result.ok and result.revision == 1
    assert backend.operations == [('probe',), ('restore_bios',), ('set_duty', DutyPair(46, 40))]
    await controller.stop('SIGTERM')
    assert backend.operations[-1] == ('close',)
    assert backend.operations.count(('restore_bios',)) == 1


@pytest.mark.asyncio
async def test_sensor_failure_boosts_once_without_bios_restore(controller, backend, clock):
    await controller.start()
    await controller.change({'control.mode': 'override'}, 'start')
    backend.operations.clear()
    clock.advance(1.0)
    with pytest.raises(TemporaryFailure):
        await controller.tick()
    assert backend.operations == [('set_duty', DutyPair(100, 100))]
    assert controller.snapshot().state == 'fault'
    with pytest.raises(TemporaryFailure):
        await controller.tick()
    await controller.stop('failure')
    assert backend.operations == [('set_duty', DutyPair(100, 100)), ('close',)]


@pytest.mark.asyncio
async def test_validation_and_deduplication_are_atomic(controller, backend):
    await controller.start()
    before = controller.snapshot()
    bad = await controller.change({'control.mode': 'override', 'fans.cpufan.override.fixed.duty_percent': True}, 'bad')
    assert not bad.ok and bad.revision == 0
    assert controller.snapshot() == before
    result = await controller.change({'control.mode': 'override'}, 'start')
    assert await controller.change({'control.mode': 'override'}, 'start') == result
    conflict = await controller.change({'control.mode': 'bios'}, 'start')
    assert not conflict.ok
    assert backend.operations.count(('set_duty', DutyPair(46, 40))) == 1


@pytest.mark.asyncio
async def test_communication_fault_has_unknown_state_and_no_followup_write(controller, backend):
    await controller.start()
    backend.busy_error = True
    result = await controller.change({'control.mode': 'override'}, 'start')
    assert not result.ok and result.revision == 0
    state = controller.snapshot()
    assert state.applied_mode == 'unknown' and state.duty is None and state.state == 'fault'
    backend.busy_error = False
    await controller.stop('SIGTERM')
    assert backend.operations == [('probe',), ('restore_bios',), ('close',)]


@pytest.mark.asyncio
async def test_unused_sources_do_not_trigger_boost_in_bios_or_fixed(controller, backend, clock):
    await controller.start()
    clock.advance(10)
    await controller.tick()
    result = await controller.change({'control.mode': 'override', 'fans.cpufan.override.mode': 'fixed', 'fans.sysfan.override.mode': 'fixed'}, 'fixed')
    assert result.ok
    await controller.tick()
    assert controller.snapshot().duty == DutyPair(40, 40)
    assert controller.snapshot().fault is None
    await controller.stop('SIGTERM')


@pytest.mark.asyncio
async def test_restore_shutdown_only_on_normal_stop(example_config, backend, clock):
    config = example_config.model_copy(deep=True)
    config.runtime.shutdown_action = 'restore_bios'
    controller = Controller(config, backend, clock)
    await controller.start()
    await controller.change({'control.mode': 'override', 'fans.cpufan.override.mode': 'fixed', 'fans.sysfan.override.mode': 'fixed'}, 'fixed')
    await controller.stop('SIGINT')
    assert backend.operations[-2:] == [('restore_bios',), ('close',)]
    assert controller.snapshot().duty is None


class BlockingBackend(MockBackend):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block_next = False

    def set_duty(self, pair):
        if self.block_next:
            self.block_next = False
            self.entered.set()
            assert self.release.wait(3), 'test failed to release mailbox'
        super().set_duty(pair)


async def queued():
    # Event-loop barriers, no wall-clock sleep.
    event = asyncio.Event()
    asyncio.get_running_loop().call_soon(event.set)
    await event.wait()


@pytest.mark.asyncio
async def test_periodic_targets_coalesce_and_mode_barrier_discards_old_writes(example_config, clock):
    backend = BlockingBackend()
    c = Controller(example_config, backend, clock)
    c.on_sample(Sample('cpu_package', 50, 0, None))
    c.on_sample(Sample('pch', 50, 0, None))
    await c.start()
    await c.change({'control.mode': 'override'}, 'start')
    backend.operations.clear()
    backend.block_next = True
    c.on_sample(Sample('cpu_package', 60, 0, None))
    first = asyncio.create_task(c.tick())
    assert await asyncio.to_thread(backend.entered.wait, 1)
    c.on_sample(Sample('cpu_package', 65, 0, None))
    second = asyncio.create_task(c.tick())
    await queued()
    c.on_sample(Sample('cpu_package', 70, 0, None))
    third = asyncio.create_task(c.tick())
    await queued()
    bios = asyncio.create_task(c.change({'control.mode': 'bios'}, 'bios'))
    await queued()
    backend.release.set()
    await asyncio.gather(first, second, third, bios)
    assert backend.operations == [('set_duty', DutyPair(66, 60)), ('restore_bios',)]
    assert c.snapshot().applied_mode == 'bios' and c.snapshot().duty is None
    await c.stop('SIGTERM')


@pytest.mark.asyncio
async def test_same_mode_commands_each_confirm_their_own_target(example_config, clock):
    backend = BlockingBackend()
    config = apply_changes(example_config, {'control.mode': 'override', 'fans.cpufan.override.mode': 'fixed', 'fans.sysfan.override.mode': 'fixed'})
    c = Controller(config, backend, clock)
    await c.start()
    backend.operations.clear()
    backend.block_next = True
    first = asyncio.create_task(c.change({'fans.cpufan.override.fixed.duty_percent': 55}, 'one'))
    assert await asyncio.to_thread(backend.entered.wait, 1)
    assert c.snapshot().revision == 0 and c.snapshot().duty == DutyPair(40, 40)
    second = asyncio.create_task(c.change({'fans.cpufan.override.fixed.duty_percent': 65}, 'two'))
    await queued()
    backend.release.set()
    results = await asyncio.gather(first, second)
    assert [r.revision for r in results] == [1, 2]
    assert backend.operations == [('set_duty', DutyPair(55, 40)), ('set_duty', DutyPair(65, 40))]
    await c.stop('SIGTERM')


@pytest.mark.asyncio
async def test_pending_periodic_targets_keep_only_latest(example_config, clock):
    backend = BlockingBackend()
    c = Controller(example_config, backend, clock)
    c.on_sample(Sample('cpu_package', 50, 0, None))
    c.on_sample(Sample('pch', 50, 0, None))
    await c.start()
    await c.change({'control.mode': 'override'}, 'start')
    backend.operations.clear()
    backend.block_next = True
    c.on_sample(Sample('cpu_package', 60, 0, None))
    first = asyncio.create_task(c.tick())
    assert await asyncio.to_thread(backend.entered.wait, 1)
    c.on_sample(Sample('cpu_package', 65, 0, None))
    second = asyncio.create_task(c.tick())
    await queued()
    c.on_sample(Sample('cpu_package', 70, 0, None))
    third = asyncio.create_task(c.tick())
    await queued()
    backend.release.set()
    await asyncio.gather(first, second, third)
    assert backend.operations == [('set_duty', DutyPair(66, 60)), ('set_duty', DutyPair(86, 80))]
    await c.stop('SIGTERM')


@pytest.mark.asyncio
async def test_reload_replaces_memory_and_failed_reload_keeps_old_config(controller, example_config):
    await controller.start()
    await controller.change({'fans.cpufan.override.fixed.duty_percent': 65}, 'edit')
    invalid = example_config.model_copy(deep=True)
    invalid.fans.cpufan.override.fixed.duty_percent = 101
    result = await controller.reload(invalid, 'bad-reload')
    assert not result.ok and result.revision == 1
    assert controller.config.fans.cpufan.override.fixed.duty_percent == 65
    result = await controller.reload(example_config, 'reload')
    assert result.ok and result.revision == 2
    assert controller.config.fans.cpufan.override.fixed.duty_percent == 40
    await controller.stop('SIGTERM')


@pytest.mark.asyncio
async def test_startup_override_with_no_samples_boosts_after_probe_only(example_config, backend, clock):
    config = apply_changes(example_config, {'control.mode': 'override'})
    c = Controller(config, backend, clock)
    with pytest.raises(TemporaryFailure):
        await c.start()
    assert backend.operations == [('probe',), ('set_duty', DutyPair(100, 100))]
    await c.stop('SIGTERM')
    assert ('restore_bios',) not in backend.operations


@pytest.mark.asyncio
async def test_failed_probe_never_boosts_or_restores(example_config, backend, clock):
    backend.busy_error = True
    c = Controller(example_config, backend, clock)
    with pytest.raises(TemporaryFailure):
        await c.start()
    await c.stop('SIGTERM')
    assert backend.operations == [('close',)]


@pytest.mark.asyncio
async def test_reload_with_unavailable_new_required_source_preserves_old_bios(controller, example_config, backend):
    await controller.start()
    candidate = apply_changes(example_config, {'control.mode': 'override'})
    controller.on_sample(Sample('pch', None, 0, 'failed'))
    result = await controller.reload(candidate, 'reload')
    assert not result.ok and result.revision == 0
    assert controller.snapshot().applied_mode == 'bios'
    assert controller.snapshot().fault is None
    assert controller.config == example_config
    assert backend.operations == [('probe',), ('restore_bios',)]
    await controller.stop('SIGTERM')


@pytest.mark.asyncio
async def test_pending_control_runs_before_pending_rpm_and_cancellation_does_not_abort_command(example_config, clock):
    backend = BlockingBackend()
    config = apply_changes(example_config, {'control.mode': 'override', 'fans.cpufan.override.mode': 'fixed', 'fans.sysfan.override.mode': 'fixed'})
    c = Controller(config, backend, clock)
    await c.start()
    backend.operations.clear()
    backend.block_next = True
    first = asyncio.create_task(c.change({'fans.cpufan.override.fixed.duty_percent': 55}, 'first'))
    assert await asyncio.to_thread(backend.entered.wait, 1)
    rpm = asyncio.create_task(c.read_rpm())
    await queued()
    second = asyncio.create_task(c.change({'fans.cpufan.override.fixed.duty_percent': 65}, 'second'))
    await queued()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    backend.release.set()
    assert (await second).revision == 2
    await rpm
    assert backend.operations == [('set_duty', DutyPair(55, 40)), ('set_duty', DutyPair(65, 40)), ('read_rpm',)]
    await c.stop('SIGTERM')


@pytest.mark.asyncio
async def test_rpm_before_identity_verification_does_not_access_mailbox(controller, backend):
    with pytest.raises(TemporaryFailure, match='not started'):
        await controller.read_rpm()
    assert backend.operations == []
    await controller.stop('normal')


@pytest.mark.asyncio
async def test_request_cache_retains_256_results_and_evicts_oldest(controller, backend):
    await controller.start()
    change = {'fans.cpufan.override.fixed.duty_percent': 55}
    for index in range(256):
        result = await controller.change(change, f'request-{index}')
        assert result.ok and result.revision == index + 1
    # At capacity both the oldest and newest replies remain idempotent.
    assert (await controller.change(change, 'request-0')).revision == 1
    assert (await controller.change(change, 'request-255')).revision == 256
    assert (await controller.change(change, 'request-256')).revision == 257
    assert (await controller.change(change, 'request-1')).revision == 2
    # The 257th distinct ID expires the oldest insertion, even if it was read.
    expired = await controller.change(change, 'request-0')
    assert expired.ok and expired.revision == 258
    assert (await controller.change(change, 'request-256')).revision == 257
    assert backend.operations == [('probe',), ('restore_bios',)]
    await controller.stop('normal')


@pytest.mark.asyncio
async def test_same_id_boolean_payload_conflicts_with_cached_numeric_success(controller, backend):
    await controller.start()
    path = 'fans.cpufan.override.inputs.0.custom.minimum_duty_percent'
    first = await controller.change({path: 1}, 'typed-id')
    assert first.ok and first.revision == 1
    conflicting = await controller.change({path: True}, 'typed-id')
    assert not conflicting.ok and 'different payload' in conflicting.error
    assert conflicting.revision == 1
    assert (await controller.change({path: 1}, 'typed-id')) == first
    assert controller.config.fans.cpufan.override.inputs[0].custom.minimum_duty_percent == 1
    assert backend.operations == [('probe',), ('restore_bios',)]
    await controller.stop('normal')


@pytest.mark.asyncio
@pytest.mark.parametrize('path', [
    'fans.cpufan.override.inputs.0.custom.duty_increment_percent_per_c',
    'fans.cpufan.override.inputs.0.custom.minimum_temperature_c',
    'fans.cpufan.override.inputs.0.boost_above_c',
])
async def test_fresh_boolean_float_command_rejects_atomically(controller, backend, path):
    await controller.start()
    initial = controller.config
    changes = {'fans.cpufan.override.fixed.duty_percent': 55,
               'fans.cpufan.override.inputs.0.custom.minimum_temperature_c': -10,
               path: True}
    result = await controller.change(changes, 'fresh-boolean')
    assert not result.ok and result.revision == 0
    assert controller.config == initial
    assert controller.snapshot().applied_mode == 'bios'
    assert controller.snapshot().fault is None
    assert backend.operations == [('probe',), ('restore_bios',)]
    await controller.stop('normal')


def test_snapshot_retains_last_success_after_source_failure(controller, clock):
    clock.advance(1)
    controller.on_sample(Sample('cpu_package', 51, clock(), None))
    clock.advance(1)
    controller.on_sample(Sample('cpu_package', None, clock(), 'read failed'))
    state = controller.snapshot()
    assert state.sources['cpu_package'].error == 'read failed'
    assert state.last_success['cpu_package'] == 1


@pytest.mark.asyncio
async def test_reload_source_replacement_does_not_reuse_old_success(controller, clock):
    await controller.start()
    clock.advance(1)
    controller.on_sample(Sample('cpu_package', 51, clock(), None))
    raw = controller.config.model_dump(mode='python')
    raw['sources']['cpu_package']['selector']['type'] = 'replacement_cpu'
    candidate = type(controller.config).model_validate(raw, context={'normalized_duration': True})
    samples = {'cpu_package': Sample('cpu_package', None, clock(), 'not available'),
               'pch': Sample('pch', 50, clock(), None)}
    result = await controller.reload(candidate, 'replace-source', samples=samples)
    assert result.ok
    assert 'cpu_package' not in controller.snapshot().last_success
    await controller.stop('normal')


@pytest.mark.asyncio
async def test_source_bound_command_resolves_after_input_reorder(controller):
    await controller.start()
    raw = controller.config.model_dump(mode='python')
    raw['fans']['sysfan']['override']['inputs'].reverse()
    candidate = type(controller.config).model_validate(raw, context={'normalized_duration': True})
    assert (await controller.reload(candidate, 'reorder')).ok
    token = 'pch'.encode().hex()
    path = f'fans.sysfan.override.inputs.source:{token}.custom.minimum_temperature_c'
    assert (await controller.change({path: 42.0}, 'stable-source')).ok
    values = {item.source: item.custom.minimum_temperature_c for item in controller.config.fans.sysfan.override.inputs}
    assert values['pch'] == 42.0
    assert values['cpu_package'] == 50.0
    await controller.stop('normal')
