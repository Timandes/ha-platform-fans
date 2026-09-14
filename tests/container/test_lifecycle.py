"""Real Docker/s6 acceptance. Requires the local mock compose project up.

No hardware devices or external MQTT broker are used. Failures include identity,
clock, supervisor and log diagnostics instead of extending product deadlines.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCKER = os.environ.get('DOCKER_BIN', shutil.which('docker') or 'docker')


def docker(*args, check=True):
    return subprocess.run([DOCKER, *args], cwd=ROOT, text=True,
                          capture_output=True, check=check, timeout=45)


def compose(*args):
    return docker('compose', '-f', 'compose.mock.yaml', *args).stdout.strip()


def inside(cid, *args, check=True):
    return docker('exec', cid, *args, check=check)


def diagnostics(cid):
    script = """import json,time
from pathlib import Path
from ha_nuc9_ec.health import check_health
p=Path('/run/ha-nuc9-ec/health.json')
d=json.loads(p.read_text()); pid=d['pid']
print(dict(snapshot=d, now=time.monotonic(), check=str(check_health(p,time.monotonic()))))
for name in (f'/proc/{pid}/stat', f'/proc/{pid}/task/{pid}/stat', f'/proc/{pid}/status'):
    try: print(name, Path(name).read_text())
    except OSError as e: print(name, str(e))
"""
    return '\n'.join([
        str(docker('logs', '--tail', '100', cid, check=False)),
        str(inside(cid, '/opt/venv/bin/python', '-c', script, check=False)),
        str(inside(cid, 's6-svstat', '/run/service/nuc9-controller', check=False)),
        docker('inspect', cid, check=False).stdout,
    ])


def snapshot(cid):
    result = inside(cid, 'cat', '/run/ha-nuc9-ec/health.json', check=False)
    try:
        return json.loads(result.stdout)
    except ValueError:
        return {}


def wait_state(cid, predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        item = snapshot(cid)
        if predicate(item):
            return item
        time.sleep(.2)
    pytest.fail(diagnostics(cid))


@pytest.fixture(scope='module')
def container():
    assert (ROOT / 'compose.mock.yaml').is_file(), 'mock container packaging is missing'
    cid = compose('ps', '-q', 'controller')
    assert cid, 'run docker compose -f compose.mock.yaml up --build -d first'
    yield cid
    # Tests may stop the service, but leave project cleanup to compose down.


def test_mock_s6_restarts_killed_and_frozen_application(container):
    cid = container
    initial = wait_state(cid, lambda x: x.get('state') == 'override')
    inside(cid, 'sh', '-c', f"kill -KILL {initial['pid']}")
    killed = wait_state(cid, lambda x: x.get('state') == 'override' and x.get('instance_id') != initial['instance_id'])
    assert compose('ps', '-q', 'controller') == cid
    inside(cid, 'sh', '-c', f"kill -STOP {killed['pid']}")
    recovered = wait_state(cid, lambda x: x.get('state') == 'override' and x.get('instance_id') != killed['instance_id'])
    assert recovered['pid'] != killed['pid']
    assert compose('ps', '-q', 'controller') == cid
    assert inside(cid, '/opt/venv/bin/ha-nuc9-ec', 'health').returncode == 0


def test_runtime_supervision_files_permissions_and_readonly_health(container):
    cid = container
    for name, value in [('timeout-finish', '7000'), ('timeout-kill', '3000')]:
        assert inside(cid, 'cat', f'/run/service/nuc9-controller/{name}').stdout.strip() == value
    pid = inside(cid, 's6-svstat', '-o', 'pid', '/run/service/health-monitor').stdout.strip()
    status = inside(cid, 'cat', f'/proc/{pid}/status').stdout
    fields = dict(line.split(':', 1) for line in status.splitlines() if ':' in line)
    for key in ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb'):
        assert int(fields[key].strip(), 16) == 0
    assert fields['NoNewPrivs'].strip() == '1'
    assert inside(cid, 'touch', '/immutable-root-test', check=False).returncode != 0
    before = snapshot(cid)['instance_id']
    for _ in range(3):
        inside(cid, '/opt/venv/bin/ha-nuc9-ec', 'health')
    assert snapshot(cid)['instance_id'] == before
    info = json.loads(docker('inspect', cid).stdout)[0]
    assert not info['HostConfig']['Devices']
    assert info['HostConfig']['ReadonlyRootfs']
    assert not info['HostConfig']['Privileged']


def test_finish_classifies_errors_without_hardware_actions(container):
    hook = '/run/service/nuc9-controller/finish'
    assert inside(container, hook, '78', '0', check=False).returncode == 125
    # Measure in the same guest clock domain as sleep and s6's timeout.
    script = ('import subprocess,time,sys; start=time.monotonic(); '
              'subprocess.run([sys.argv[1],"75","0"],check=True); '
              'print(time.monotonic()-start)')
    elapsed = float(inside(container, '/opt/venv/bin/python', '-c', script, hook).stdout)
    assert 5 <= elapsed < 7
    assert inside(container, hook, '0', '0').returncode == 0


def test_stop_stays_stopped(container):
    docker('stop', '--time', '15', container)
    time.sleep(2)
    state = json.loads(docker('inspect', container).stdout)[0]['State']
    assert not state['Running']
    assert state['ExitCode'] == 0


def test_packaging_exists():
    assert (ROOT / 'compose.mock.yaml').is_file(), 'mock container packaging is missing'


def start_isolated(config, name, network=None):
    import tempfile
    import yaml
    # This directory must be shared into the Docker daemon host (e.g. Lima).
    shared = Path(os.environ.get('NUC9_TEST_SHARE', '/private/tmp/ha-nuc9-vm-share'))
    shared.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix='lifecycle-', dir=shared))
    path = folder / 'config.yaml'
    path.write_text(yaml.safe_dump(config))
    args = ['run', '-d', '--name', name, '--platform', 'linux/amd64',
            '--read-only', '--tmpfs', '/run:rw,nosuid,nodev,exec,size=16m',
            '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,size=16m',
            '-e', 'NUC9_BACKEND=mock', '-v', f'{path}:/config/config.yaml:ro']
    if network:
        args += ['--network', network]
    cid = docker(*args, 'ha-nuc9-ec:local').stdout.strip()
    return cid, folder


def test_permanent_configuration_failure_stays_down():
    import uuid
    cid, folder = start_isolated({'version': 999}, 'nuc9-invalid-' + uuid.uuid4().hex[:8])
    try:
        failed = wait_state(cid, lambda x: x.get('state') == 'permanent_failure')
        time.sleep(7)
        assert snapshot(cid)['instance_id'] == failed['instance_id'], diagnostics(cid)
        assert inside(cid, 's6-svstat', '-o', 'up,wantedup', '/run/service/nuc9-controller').stdout.strip() == 'false false'
        assert inside(cid, '/opt/venv/bin/ha-nuc9-ec', 'health', check=False).returncode == 1
        assert 'error:' in docker('logs', cid).stderr
        assert json.loads(docker('inspect', cid).stdout)[0]['State']['Running']
        docker('restart', cid)
        restarted = wait_state(cid, lambda x: x.get('state') == 'permanent_failure')
        assert restarted['instance_id'] != failed['instance_id']
    finally:
        docker('rm', '-f', cid, check=False)
        shutil.rmtree(folder)


def test_mqtt_runtime_changes_are_discarded_on_s6_restart():
    import uuid
    import yaml
    token = uuid.uuid4().hex[:8]
    network, broker = 'nuc9-test-' + token, 'nuc9-broker-' + token
    docker('network', 'create', network)
    cid = folder = None
    try:
        docker('run', '-d', '--name', broker, '--network', network,
               '--platform', 'linux/amd64', '--entrypoint', 'sh',
               'eclipse-mosquitto:2.0.22@sha256:212f89e1eaeb2c322d6441b64396e3346026674db8fa9c27beac293405c32b3c', '-c',
               'printf "listener 1883\\nallow_anonymous true\\npersistence false\\n" > /tmp/test.conf; exec mosquitto -c /tmp/test.conf')
        config = yaml.safe_load((ROOT / 'config/mock.yaml').read_text())
        config['control']['mode'] = 'bios'
        config['mqtt'].update(enabled=True, broker=f'tcp://{broker}:1883', username=None, password_file=None)
        cid, folder = start_isolated(config, 'nuc9-mqtt-' + token, network)
        initial = wait_state(cid, lambda x: x.get('state') == 'bios')
        # Publish the same HA command topic consumed by the real application.
        script = '''import paho.mqtt.client as mqtt, time, sys
c=mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
c.connect(sys.argv[1],1883); c.loop_start(); time.sleep(1)
p=c.publish("nuc9/nas11/command/control_mode", "override", qos=1); p.wait_for_publish(5)
c.disconnect(); c.loop_stop()
'''
        inside(cid, '/opt/venv/bin/python', '-c', script, broker)
        changed = wait_state(cid, lambda x: x.get('state') == 'override')
        assert changed['instance_id'] == initial['instance_id']
        inside(cid, 'sh', '-c', f"kill -KILL {changed['pid']}")
        restored = wait_state(cid, lambda x: x.get('state') == 'bios' and x.get('instance_id') != changed['instance_id'])
        assert restored['instance_id'] != changed['instance_id']
        assert yaml.safe_load((folder / 'config.yaml').read_text())['control']['mode'] == 'bios'
    finally:
        if cid:
            docker('rm', '-f', cid, check=False)
        docker('rm', '-f', broker, check=False)
        docker('network', 'rm', network, check=False)
        if folder:
            shutil.rmtree(folder)


def test_default_bios_configuration_needs_no_secret_or_broker():
    import uuid
    import yaml
    config = yaml.safe_load((ROOT / 'config/container.yaml').read_text())
    cid, folder = start_isolated(config, 'nuc9-bios-' + uuid.uuid4().hex[:8])
    try:
        wait_state(cid, lambda x: x.get('state') == 'bios')
        assert inside(cid, 'test', '-e', '/run/secrets/mqtt_password', check=False).returncode == 1
        assert inside(cid, '/opt/venv/bin/ha-nuc9-ec', 'health').returncode == 0
    finally:
        docker('rm', '-f', cid, check=False)
        shutil.rmtree(folder)
