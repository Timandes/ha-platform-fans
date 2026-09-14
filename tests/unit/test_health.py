import json
from dataclasses import replace

import pytest

from ha_nuc9_ec import health
from ha_nuc9_ec.model import StateSnapshot


def state(**kwargs):
    return replace(StateSnapshot('starting', 'bios', 'unknown', 0, None, None, {}, None, None, {}, {}), **kwargs)


@pytest.fixture
def identity(monkeypatch):
    monkeypatch.setattr(health, 'process_starttime', lambda pid: 100)
    monkeypatch.setattr(health.os, 'sysconf', lambda name: 10)
    monkeypatch.setattr('time.monotonic', lambda: 10.0)
    health._instance_started_at.cache_clear()


def test_startup_grace_and_runtime_expiry(tmp_path, identity):
    path = tmp_path / 'health.json'
    health.write_health(path, state(), 'instance-a')
    assert health.check_health(path, 19.9).status == 'starting'
    assert health.check_health(path, 20).status == 'stale'
    health.write_health(path, state(state='bios', last_cycle_at=30), 'instance-a')
    assert health.check_health(path, 31.9).status == 'healthy'
    assert health.check_health(path, 32).status == 'stale'


def test_notifications_do_not_fake_progress_or_depend_on_mqtt(tmp_path, identity):
    path = tmp_path / 'health.json'
    health.write_health(path, state(state='bios', last_cycle_at=30), 'a')
    health.write_health(path, state(state='bios', last_cycle_at=30, configuration={'mqtt': 'offline'}, revision=2), 'a')
    assert health.check_health(path, 33).status == 'stale'
    assert not list(tmp_path.glob('*.tmp'))


def test_reused_pid_and_terminal_states(tmp_path, identity, monkeypatch):
    path = tmp_path / 'health.json'
    health.write_health(path, state(state='bios', last_cycle_at=30), 'old')
    monkeypatch.setattr(health, 'process_starttime', lambda pid: 101)
    assert health.check_health(path, 31).status == 'stale'
    health.write_health(path, state(state='permanent_failure'), 'new')
    assert health.check_health(path, 31).status == 'permanent_failure'
    health.write_health(path, state(state='stopped'), 'new')
    assert health.check_health(path, 31).status == 'stopped'


@pytest.mark.parametrize('payload', ['{}', 'null', '{', '{"pid":true}', '{"last_cycle_at":NaN}'])
def test_invalid_health_is_unhealthy(tmp_path, payload):
    path = tmp_path / 'health.json'
    path.write_text(payload)
    assert health.check_health(path, 30).status == 'stale'


def test_health_cli_is_read_only_and_hardware_free(tmp_path):
    import subprocess
    import sys
    path = tmp_path / 'missing.json'
    code = "from ha_nuc9_ec.cli import main; import sys; result=main(['health','--health-path',sys.argv[1]]); assert result==1; assert not any(n.startswith('ha_nuc9_ec.hardware') for n in sys.modules)"
    subprocess.run([sys.executable, '-c', code, str(path)], check=True)
    assert not path.exists()


def test_run_publishes_starting_before_config_and_terminal_failure(tmp_path, identity, monkeypatch):
    from ha_nuc9_ec import cli
    path = tmp_path / 'health.json'
    observed = []
    def load(_):
        observed.append(json.loads(path.read_text()))
        raise cli.ConfigError('bad config')
    monkeypatch.setattr(cli, 'load_config', load)
    monkeypatch.setattr(cli.sys, 'platform', 'linux')
    assert cli.main(['run', 'missing.yaml', '--backend', 'mock', '--health-path', str(path)]) == 78
    assert observed[0]['state'] == 'starting'
    final = json.loads(path.read_text())
    assert final['state'] == 'permanent_failure'
    assert final['instance_id'] == observed[0]['instance_id']


def test_terminal_snapshot_remains_diagnostic_after_process_exit(tmp_path, identity, monkeypatch):
    path = tmp_path / 'health.json'
    health.write_health(path, state(state='permanent_failure'), 'dead')
    def absent(pid):
        raise ProcessLookupError()
    monkeypatch.setattr(health, 'process_starttime', absent)
    assert health.check_health(path, 40).status == 'permanent_failure'


def test_preflight_observes_new_startup_and_failure_is_terminal(tmp_path, identity, monkeypatch, example_config):
    from ha_nuc9_ec import cli
    from ha_nuc9_ec.hardware.base import PreflightError
    path = tmp_path / 'health.json'
    health.write_health(path, state(state='bios', last_cycle_at=30), 'old')
    monkeypatch.setattr(cli.sys, 'platform', 'linux')
    example_config.mqtt.enabled = False
    monkeypatch.setattr(cli, 'load_config', lambda _: example_config)
    observed = []
    def fail(*args):
        data = json.loads(path.read_text())
        observed.append(data)
        assert data['state'] == 'starting'
        assert data['instance_id'] != 'old'
        raise PreflightError('identity mismatch')
    monkeypatch.setattr('ha_nuc9_ec.hardware.linux.LinuxBackend.open', fail)
    assert cli.main(['run', 'config.yaml', '--health-path', str(path)]) == 78
    assert observed and observed[0]['state'] == 'starting'
    assert json.loads(path.read_text())['state'] == 'permanent_failure'


@pytest.mark.parametrize(('observed_at', 'want'), [(30.25, 'healthy'), (29.75, 'stale'), (32.125, 'stale')])
def test_cycle_completed_during_health_read_rechecks_clock(tmp_path, identity, monkeypatch, observed_at, want):
    path = tmp_path / 'health.json'
    health.write_health(path, state(state='bios', last_cycle_at=29.9), 'a')
    original_read = type(path).read_text
    def racing_read(target, *args, **kwargs):
        if target == path:
            # This completes after caller captured now=30, before the reader
            # opens the atomically replaced health file.
            health.write_health(path, state(state='bios', last_cycle_at=30.125), 'a')
        return original_read(target, *args, **kwargs)
    monkeypatch.setattr(type(path), 'read_text', racing_read)
    monkeypatch.setattr('time.monotonic', lambda: observed_at)
    assert health.check_health(path, 30).status == want


def test_startup_clock_excludes_prior_host_suspend(tmp_path, identity, monkeypatch):
    path = tmp_path / 'health.json'
    monkeypatch.setattr(health, 'process_starttime', lambda pid: 1000)
    monkeypatch.setattr('time.monotonic', lambda: 10.0)
    health.write_health(path, state(), 'after-suspend')
    # proc starttime reports BOOTTIME=100s (90s of prior suspension);
    # startup elapsed must instead use the controller's monotonic clock.
    assert health.check_health(path, 19.9).status == 'starting'
    monkeypatch.setattr('time.monotonic', lambda: 19.9)
    health.write_health(path, state(), 'after-suspend')
    assert health.check_health(path, 20).status == 'stale'


def test_starting_published_during_read_rechecks_clock(tmp_path, identity, monkeypatch):
    path = tmp_path / 'health.json'
    health.write_health(path, state(), 'old-start')
    original_read = type(path).read_text
    def racing_read(target, *args, **kwargs):
        if target == path:
            health.write_health(path, state(), 'new-start')
        return original_read(target, *args, **kwargs)
    clock = iter([30.125, 30.25])
    monkeypatch.setattr('time.monotonic', lambda: next(clock))
    monkeypatch.setattr(type(path), 'read_text', racing_read)
    assert health.check_health(path, 30).status == 'starting'
