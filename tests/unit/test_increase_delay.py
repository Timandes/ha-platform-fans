from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from ha_nuc9_ec.config import AppConfig, apply_changes
from ha_nuc9_ec.controller import Controller, TemporaryFailure
from ha_nuc9_ec.hardware.mock import MockBackend
from ha_nuc9_ec.model import DutyPair, Sample


def delayed_config():
    data = yaml.safe_load((Path(__file__).parents[2] / 'config/nas11/config.yaml').read_text())
    for fan, delay in [('cpufan', '1s'), ('sysfan', '5s')]:
        for item in data['fans'][fan]['override']['inputs']:
            item['increase_delay'] = delay
    return AppConfig.model_validate(data)


@asynccontextmanager
async def running():
    config = delayed_config()
    clock = [0.0]
    backend = MockBackend()
    c = Controller(config, backend, lambda: clock[0])
    def sample(at, cpu=40, nvmes=(47, 47, 47)):
        clock[0] = at
        values = {'cpu_package': cpu, **dict(zip(('nvme_02', 'nvme_03', 'nvme_04'), nvmes))}
        for key, value in values.items():
            c.on_sample(Sample(key, value, at, None))
    async def tick(at, cpu=40, nvmes=(47, 47, 47)):
        sample(at, cpu, nvmes)
        await c.tick()
        return c.snapshot().duty
    sample(0)
    await c.start()
    try:
        yield c, backend, clock, sample, tick
    finally:
        await c.stop('normal')


def test_delay_duration_validation_and_normalized_roundtrip():
    config = delayed_config()
    assert config.fans.cpufan.override.inputs[0].increase_delay == 1
    assert config.fans.sysfan.override.inputs[-1].increase_delay == 5
    assert apply_changes(config, {'fans.cpufan.override.mode': 'cool'}).fans.sysfan.override.inputs[-1].increase_delay == 5
    raw = config.model_dump()
    for value in ['0s', '-1s', 'nan', 5, True]:
        raw['fans']['sysfan']['override']['inputs'][0]['increase_delay'] = value
        with pytest.raises(ValidationError):
            AppConfig.model_validate(raw, context={'normalized_duration': True})


@pytest.mark.asyncio
async def test_cpu_and_sys_wait_independently_and_next_rise_needs_new_window():
    async with running() as (c, backend, clock, sample, tick):
        assert await tick(1, 70) == DutyPair(40, 30)
        assert await tick(1.999, 70) == DutyPair(40, 30)
        assert await tick(2, 70) == DutyPair(64, 30)
        assert await tick(5.999, 70) == DutyPair(64, 30)
        assert await tick(6, 70) == DutyPair(64, 60)
        assert await tick(6.1, 80) == DutyPair(64, 60)
        assert await tick(7.1, 80) == DutyPair(88, 60)
        assert await tick(11.1, 80) == DutyPair(88, 80)


@pytest.mark.asyncio
async def test_low_sample_between_coalesced_ticks_resets_rise_window():
    async with running() as (c, backend, clock, sample, tick):
        await tick(1, 70)
        sample(1.5, 40)
        assert await tick(1.6, 70) == DutyPair(40, 30)
        assert await tick(2, 70) == DutyPair(40, 30)
        assert await tick(2.6, 70) == DutyPair(64, 30)
        assert await tick(6, 70) == DutyPair(64, 30)
        assert await tick(6.6, 70) == DutyPair(64, 60)


@pytest.mark.asyncio
async def test_error_sample_between_coalesced_ticks_resets_rise_window():
    async with running() as (c, backend, clock, sample, tick):
        await tick(1, 70)
        clock[0] = 1.5
        c.on_sample(Sample('cpu_package', None, 1.5, 'read failed'))
        assert await tick(1.6, 70) == DutyPair(40, 30)
        assert await tick(2, 70) == DutyPair(40, 30)
        assert await tick(2.6, 70) == DutyPair(64, 30)
        assert await tick(6, 70) == DutyPair(64, 30)
        assert await tick(6.6, 70) == DutyPair(64, 60)


@pytest.mark.asyncio
async def test_sys_sources_cannot_borrow_each_others_high_time():
    async with running() as (c, backend, clock, sample, tick):
        await tick(1, 70)
        assert (await tick(4, 40, (50, 47, 47))).sys == 30
        assert (await tick(6, 40, (50, 47, 47))).sys == 30
        assert (await tick(7, 40, (47, 50, 47))).sys == 30
        assert (await tick(9, 40, (47, 50, 47))).sys == 30
        assert (await tick(12, 40, (47, 50, 47))).sys == 60


@pytest.mark.asyncio
@pytest.mark.parametrize('cpu,nvmes,wanted', [(90, (47, 47, 47), DutyPair(100, 100)), (40, (53, 47, 47), DutyPair(40, 100))])
async def test_explicit_boost_threshold_bypasses_wait(cpu, nvmes, wanted):
    async with running() as (c, backend, clock, sample, tick):
        assert await tick(.1, cpu, nvmes) == wanted


@pytest.mark.asyncio
async def test_natural_full_speed_still_waits_without_explicit_boost():
    async with running() as (c, backend, clock, sample, tick):
        assert await tick(1, 85) == DutyPair(40, 30)
        assert await tick(2, 85) == DutyPair(100, 30)


@pytest.mark.asyncio
async def test_downshift_delay_and_manual_fixed_commands_remain():
    async with running() as (c, backend, clock, sample, tick):
        await tick(1, 90)
        assert await tick(2, 40) == DutyPair(100, 100)
        assert await tick(11.999, 40) == DutyPair(100, 100)
        assert await tick(12, 40) == DutyPair(40, 30)
        result = await c.change({'fans.sysfan.override.mode': 'fixed', 'fans.sysfan.override.fixed.duty_percent': 75}, 'fixed')
        assert result.ok and c.snapshot().duty.sys == 75


@pytest.mark.asyncio
async def test_successful_reload_resets_pending_windows():
    async with running() as (c, backend, clock, sample, tick):
        await tick(1, 70)
        await tick(2, 70)
        sample(3, 40)
        assert (await c.reload(c.config, 'reload')).ok
        assert (await tick(4, 70)).sys == 30
        assert (await tick(6, 70)).sys == 30
        assert (await tick(9, 70)).sys == 60


@pytest.mark.asyncio
async def test_source_failure_during_pending_rise_still_boosts_immediately():
    async with running() as (c, backend, clock, sample, tick):
        await tick(.1, 70)
        clock[0] = 1
        with pytest.raises(TemporaryFailure):
            await c.tick()
        assert c.snapshot().duty == DutyPair(100, 100)
        assert c.snapshot().fault is not None
