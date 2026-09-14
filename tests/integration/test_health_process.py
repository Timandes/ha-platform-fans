"""Real Linux pidfd checks; no hardware paths are opened."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from ha_nuc9_ec.health import process_starttime

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='requires Linux procfs and pidfd')


def monitor():
    spec = importlib.util.spec_from_file_location('health_monitor', Path(__file__).parents[2] / 'container/health_monitor.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def child():
    proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    yield proc
    if proc.poll() is None:
        proc.kill()
    proc.wait()


def snapshot(path, child, **changes):
    data = dict(pid=child.pid, starttime=process_starttime(child.pid), instance_id='child',
                started_at=time.monotonic()-20, last_cycle_at=time.monotonic()-5, state='bios')
    data.update(changes)
    path.write_text(json.dumps(data))


def test_stalled_same_instance_is_killed(tmp_path, child):
    path = tmp_path / 'health.json'
    snapshot(path, child)
    assert monitor().check_once(path, lambda: (True, True, child.pid))
    assert child.wait(timeout=3) == -signal.SIGKILL


@pytest.mark.parametrize('case', ['down', 'other_pid', 'reused_pid', 'fresh', 'stopped', 'exited'])
def test_non_stalled_or_unowned_instances_are_ignored(tmp_path, child, case):
    path = tmp_path / 'health.json'
    changes = {'reused_pid': {'starttime': 0}, 'fresh': {'last_cycle_at': time.monotonic()}, 'stopped': {'state': 'stopped'}}.get(case, {})
    snapshot(path, child, **changes)
    if case == 'exited':
        child.terminate()
        child.wait()
    status = (case != 'exited', case != 'down', child.pid + (case == 'other_pid'))
    assert not monitor().check_once(path, lambda: status)
    if case != 'exited':
        assert child.poll() is None


def test_recheck_rejects_replaced_snapshot(tmp_path, child):
    path = tmp_path / 'health.json'
    snapshot(path, child)
    module = monitor()
    old = module.check_health(path, time.monotonic())
    fd = os.pidfd_open(child.pid)
    try:
        snapshot(path, child, instance_id='new')
        assert not module.identity_and_staleness_still_match(path, old, fd)
    finally:
        os.close(fd)


def test_monitor_import_does_not_load_hardware():
    subprocess.run([sys.executable, '-c', "import runpy, sys; runpy.run_path('container/health_monitor.py', run_name='import_test'); assert not any(n.startswith('ha_nuc9_ec.hardware') for n in sys.modules)"], check=True)


def test_capability_dropped_monitor_can_kill_same_uid_child():
    code = """
import os, runpy, subprocess, sys, signal
module = runpy.run_path('container/health_monitor.py', run_name='import_test')
status = open('/proc/self/status').read().splitlines()
assert all(int(line.split()[1], 16) == 0 for line in status if line.startswith('Cap'))
assert 'NoNewPrivs:\\t1' in status
assert not any(n.startswith('ha_nuc9_ec.hardware') for n in sys.modules)
p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
fd = os.pidfd_open(p.pid)
try:
    signal.pidfd_send_signal(fd, signal.SIGKILL)
    assert p.wait(timeout=3) == -signal.SIGKILL
finally:
    os.close(fd)
    if p.poll() is None:
        p.kill(); p.wait()
"""
    subprocess.run(['/usr/bin/setpriv', '--bounding-set=-all', '--inh-caps=-all', '--ambient-caps=-all', '--no-new-privs', sys.executable, '-c', code], check=True)


def test_cli_mock_progress_freeze_and_stop(tmp_path):
    import yaml
    from ha_nuc9_ec.health import check_health
    config = yaml.safe_load(Path('config/example.yaml').read_text())
    config['mqtt']['enabled'] = False
    config_path = tmp_path / 'config.yaml'
    config_path.write_text(yaml.safe_dump(config))
    path = tmp_path / 'health.json'
    command = [sys.executable, '-m', 'ha_nuc9_ec.cli', 'run', '--backend', 'mock', '--config', str(config_path), '--health-path', str(path), '--lock-path', str(tmp_path / 'lock')]
    p = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and check_health(path, time.monotonic()).status != 'healthy':
            assert p.poll() is None
            time.sleep(.02)
        assert check_health(path, time.monotonic()).status == 'healthy', path.read_text()
        first = json.loads(path.read_text())
        p.send_signal(signal.SIGSTOP)
        time.sleep(2.1)
        assert monitor().check_once(path, lambda: (True, True, p.pid))
        assert p.wait(timeout=3) == -signal.SIGKILL
        p = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            current = json.loads(path.read_text())
            if current['instance_id'] != first['instance_id'] and check_health(path, time.monotonic()).status == 'healthy':
                break
            time.sleep(.02)
        assert current['instance_id'] != first['instance_id']
        assert check_health(path, time.monotonic()).status == 'healthy', path.read_text()
        p.terminate()
        out, err = p.communicate(timeout=5)
        assert p.returncode == 0, err
        assert check_health(path, time.monotonic()).status == 'stopped'
        assert not monitor().check_once(path, lambda: (False, False, -1))
    finally:
        if p.poll() is None:
            p.kill()
        p.communicate()


def test_s6_wantedup_and_stop_race(tmp_path, child, monkeypatch):
    tool = tmp_path / 's6-svstat'
    tool.write_text('#!/bin/sh\n[ "$1" = "-o" ] && [ "$2" = "up,wantedup,pid" ] || exit 1\nprintf "true false 123\\n"\n')
    tool.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path) + ':' + os.environ['PATH'])
    module = monitor()
    assert module.service_status(tmp_path) == (True, False, 123)
    path = tmp_path / 'health.json'
    snapshot(path, child)
    statuses = iter([(True, True, child.pid), (True, False, child.pid)])
    assert not module.check_once(path, lambda: next(statuses))
    assert child.poll() is None
