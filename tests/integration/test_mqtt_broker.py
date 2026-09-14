import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
import getpass
import dataclasses
import sys

import paho.mqtt.client as mqtt
import pytest

from ha_nuc9_ec.config import load_config
from ha_nuc9_ec.controller import Controller
from ha_nuc9_ec.hardware.mock import MockBackend
from ha_nuc9_ec.model import CommandResult, Sample
from ha_nuc9_ec.mqtt import MQTTAdapter


MOSQUITTO = (os.environ.get("MOSQUITTO_BIN") or shutil.which("mosquitto") or
             "/private/tmp/mosquitto-2.0.22/src/mosquitto")


class ControllerLoop:
    def __init__(self, config, clock=time.monotonic):
        import asyncio
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.controller = Controller(config, MockBackend(), clock)
        now = clock()
        for source_id, source in config.sources.items():
            if source.enabled:
                self.controller.on_sample(Sample(source_id, 50, now, None))
        self.call(self.controller.start()).result(2)

    def call(self, coroutine):
        import asyncio
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop)

    def submit(self, changes, request_id):
        return self.call(self.controller.change(changes, request_id))

    def close(self):
        self.call(self.controller.stop("normal")).result(2)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(2)


def wait_until(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.02)
    return False


class RestartableBroker:
    def __init__(self, path, port):
        self.path, self.port, self.process = path, port, None
        self.config = path / 'restartable.conf'
        self.config.write_text(f'listener {port} 127.0.0.1\nallow_anonymous true\npersistence false\n')

    def start(self):
        self.process = subprocess.Popen([MOSQUITTO, '-c', str(self.config)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        time.sleep(.15)
        if self.process.poll() is not None:
            pytest.fail(self.process.stdout.read().decode())

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(3)


@pytest.fixture
def restartable_broker(tmp_path, unused_tcp_port):
    if not os.path.isfile(MOSQUITTO):
        pytest.skip('local Mosquitto 2.0.22 binary unavailable')
    item = RestartableBroker(tmp_path, unused_tcp_port)
    item.start()
    try:
        yield item
    finally:
        item.stop()


@pytest.fixture
def broker(tmp_path, unused_tcp_port):
    if not os.path.isfile(MOSQUITTO):
        pytest.skip("local Mosquitto 2.0.22 binary unavailable")
    config = tmp_path / "mosquitto.conf"
    config.write_text(f"listener {unused_tcp_port} 127.0.0.1\nallow_anonymous true\npersistence false\n")
    process = subprocess.Popen([MOSQUITTO, "-c", str(config)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    time.sleep(.15)
    if process.poll() is not None:
        pytest.fail(process.stdout.read().decode())
    try:
        yield unused_tcp_port
    finally:
        process.terminate()
        process.wait(timeout=3)


@pytest.fixture
def tls_broker(tmp_path, unused_tcp_port):
    if not os.path.isfile(MOSQUITTO):
        pytest.skip("local Mosquitto 2.0.22 binary unavailable")
    ca_key, ca_cert = tmp_path / "ca.key", tmp_path / "ca.crt"
    key, csr, cert = tmp_path / "server.key", tmp_path / "server.csr", tmp_path / "server.crt"
    extensions = tmp_path / "server.ext"
    extensions.write_text("subjectAltName=DNS:localhost\n")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=MQTT Test CA", "-keyout", ca_key, "-out", ca_cert], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["openssl", "req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=localhost",
                    "-addext", "subjectAltName=DNS:localhost", "-keyout", key, "-out", csr], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["openssl", "x509", "-req", "-days", "1", "-in", csr, "-CA", ca_cert, "-CAkey", ca_key,
                    "-CAcreateserial", "-extfile", extensions, "-out", cert], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    config = tmp_path / "mosquitto-tls.conf"
    config.write_text(f"user {getpass.getuser()}\nlistener {unused_tcp_port} 127.0.0.1\nallow_anonymous true\n"
                      f"cafile {ca_cert}\ncertfile {cert}\nkeyfile {key}\npersistence false\n")
    process = subprocess.Popen([MOSQUITTO, "-c", str(config)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    time.sleep(.15)
    if process.poll() is not None:
        pytest.fail(process.stdout.read().decode())
    try:
        yield unused_tcp_port, ca_cert
    finally:
        process.terminate()
        process.wait(timeout=3)


def test_real_broker_birth_state_commands_and_retained_rejection(broker):
    example_config = load_config(Path(__file__).parents[2] / 'config' / 'example.yaml')
    raw = example_config.model_dump(mode="python")
    raw["mqtt"].update(enabled=True, broker=f"tcp://127.0.0.1:{broker}", username=None, password_file=None)
    config = type(example_config).model_validate(raw, context={"normalized_duration": True})
    controlled = ControllerLoop(config)
    adapter = MQTTAdapter(config.mqtt, controlled.submit, device_id=config.device.id)
    unsubscribe = controlled.controller.subscribe(adapter.publish_state)
    adapter.publish_state(controlled.controller.snapshot())

    received = []
    ready = threading.Event()
    observer = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="observer", clean_session=True)
    observer.on_connect = lambda client, userdata, flags, reason, properties: client.subscribe("#", qos=1)
    def on_message(client, userdata, message):
        received.append((message.topic, bytes(message.payload), message.retain))
        if message.topic == "nuc9/nas11/availability" and message.payload == b"online":
            ready.set()
    observer.on_message = on_message
    observer.connect("127.0.0.1", broker)
    observer.loop_start()
    # Store a command before the adapter subscribes; broker replay marks it retained.
    observer.publish("nuc9/nas11/command/control_mode", "bios", qos=1, retain=True).wait_for_publish()
    try:
        adapter.start()
        assert ready.wait(4)
        topics = [item[0] for item in received]
        assert topics.index("nuc9/nas11/state") < topics.index("nuc9/nas11/availability")

        before_birth = sum(topic.endswith("/config") for topic, _, _ in received)
        before_states = sum(topic == "nuc9/nas11/state" for topic, _, _ in received)
        observer.publish("homeassistant/status", "online", qos=1)
        assert wait_until(lambda: sum(topic.endswith("/config") for topic, _, _ in received) > before_birth)
        assert sum(topic == "nuc9/nas11/state" for topic, _, _ in received) > before_states

        # Explicit JSON IDs deduplicate; a conflicting payload is rejected.
        body = json.dumps({"request_id": "broker-1", "changes": {"control.mode": "override"}})
        observer.publish("nuc9/nas11/set", body, qos=1)
        observer.publish("nuc9/nas11/set", body, qos=1)
        assert wait_until(lambda: len([p for t, p, _ in received if t.endswith("/result") and b"broker-1" in p]) >= 2)
        assert controlled.controller.snapshot().revision == 1
        observer.publish("nuc9/nas11/set", json.dumps({"request_id": "broker-1", "changes": {"control.mode": "bios"}}), qos=1)
        assert wait_until(lambda: any(b"different payload" in p for t, p, _ in received if t.endswith("/result")))
        assert controlled.controller.snapshot().requested_mode == "override"

        # Scalar ABA operations each execute rather than replaying cached content.
        for value in ("bios", "override", "bios"):
            observer.publish("nuc9/nas11/command/control_mode", value, qos=1)
            assert wait_until(lambda value=value: controlled.controller.snapshot().requested_mode == value)
        assert controlled.controller.snapshot().revision == 4
        cpu_token = 'cpu_package'.encode().hex()
        boost_topic = f'nuc9/nas11/command/cpufan_input_source_{cpu_token}_boost_above_c'
        minimum_topic = f'nuc9/nas11/command/cpufan_input_source_{cpu_token}_minimum_temperature_c'
        observer.publish(boost_topic, '45', qos=1)
        assert wait_until(lambda: any(b'boost_above_c must be above' in p for t, p, _ in received if t.endswith('/result')))
        observer.publish(minimum_topic, '40', qos=1)
        assert wait_until(lambda: controlled.controller.snapshot().revision == 5)
        observer.publish(boost_topic, '45', qos=1)
        assert wait_until(lambda: controlled.controller.snapshot().revision == 6)
        assert controlled.controller.config.fans.cpufan.override.inputs[0].boost_above_c == 45
        # Rejected command traffic remains continuous, yet the independent
        # clock still causes the five-second periodic state publication.
        state_count = sum(t == 'nuc9/nas11/state' for t, _, _ in received)
        traffic_deadline = time.monotonic() + 5.3
        while time.monotonic() < traffic_deadline:
            observer.publish('nuc9/nas11/command/unknown', 'x', qos=1)
            time.sleep(.04)
        assert wait_until(lambda: sum(t == 'nuc9/nas11/state' for t, _, _ in received) > state_count)
        assert any(topic == "nuc9/nas11/result" and b"retained" in payload for topic, payload, _ in received)
    finally:
        unsubscribe()
        adapter.stop()
        observer.loop_stop()
        observer.disconnect()
        controlled.close()


def test_real_broker_tls_peer_verification(tls_broker):
    port, ca_cert = tls_broker
    config = load_config(Path(__file__).parents[2] / 'config' / 'example.yaml')
    raw = config.model_dump(mode="python")
    raw["mqtt"].update(enabled=True, broker=f"ssl://localhost:{port}", username=None, password_file=None,
                       tls={"ca_file": str(ca_cert)})
    config = type(config).model_validate(raw, context={"normalized_duration": True})
    adapter = MQTTAdapter(config.mqtt, lambda changes, request_id: CommandResult(request_id, True, 0))
    controller = Controller(config, MockBackend(), time.monotonic)
    adapter.publish_state(controller.snapshot())
    adapter.start()
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline and not adapter._client.is_connected():
        time.sleep(.02)
    assert adapter._client.is_connected()
    adapter.stop()


def test_first_state_arriving_after_connect_precedes_online(broker):
    config = mqtt_config(load_config(Path(__file__).parents[2] / 'config' / 'example.yaml'), broker)
    controlled = ControllerLoop(config)
    adapter = MQTTAdapter(config.mqtt, controlled.submit)
    received = []
    observer = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='late-state-observer', clean_session=True)
    observer.on_connect = lambda client, userdata, flags, reason, properties: client.subscribe('nuc9/nas11/#', 1)
    observer.on_message = lambda client, userdata, message: received.append((message.topic, bytes(message.payload)))
    observer.connect('127.0.0.1', broker); observer.loop_start()
    try:
        adapter.start()
        assert wait_until(lambda: adapter._client.is_connected())
        time.sleep(.2)
        assert not any(t == 'nuc9/nas11/availability' and p == b'online' for t, p in received)
        adapter.publish_state(controlled.controller.snapshot())
        assert wait_until(lambda: any(t == 'nuc9/nas11/availability' and p == b'online' for t, p in received))
        state = next(i for i, item in enumerate(received) if item[0] == 'nuc9/nas11/state')
        online = next(i for i, item in enumerate(received) if item == ('nuc9/nas11/availability', b'online'))
        assert state < online
    finally:
        adapter.stop(); observer.loop_stop(); observer.disconnect(); controlled.close()


def mqtt_config(example_config, port):
    raw = example_config.model_dump(mode='python')
    raw['mqtt'].update(enabled=True, broker=f'tcp://127.0.0.1:{port}', username=None, password_file=None)
    return type(example_config).model_validate(raw, context={'normalized_duration': True})


def test_broker_outage_keeps_local_cycles_and_reconnects_latest_state_before_online(restartable_broker):
    config = mqtt_config(load_config(Path(__file__).parents[2] / 'config' / 'example.yaml'), restartable_broker.port)
    controlled = ControllerLoop(config)
    adapter = MQTTAdapter(config.mqtt, controlled.submit)
    unsubscribe = controlled.controller.subscribe(adapter.publish_state)
    adapter.publish_state(controlled.controller.snapshot())
    received = []
    observer = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='restart-observer', clean_session=True)
    observer.on_connect = lambda client, userdata, flags, reason, properties: client.subscribe('nuc9/nas11/#', 1)
    observer.on_message = lambda client, userdata, message: received.append((message.topic, bytes(message.payload)))
    observer.connect('127.0.0.1', restartable_broker.port)
    observer.loop_start()
    try:
        adapter.start()
        assert wait_until(lambda: any(t.endswith('/availability') and p == b'online' for t, p in received))
        restartable_broker.stop()
        before = controlled.controller.snapshot().last_cycle_at
        controlled.call(controlled.controller.tick()).result(2)
        assert controlled.controller.snapshot().last_cycle_at != before
        for revision in range(400):
            adapter.publish_state(dataclasses.replace(controlled.controller.snapshot(), revision=revision))
        marker = len(received)
        restartable_broker.start()
        assert wait_until(lambda: sum(t == 'nuc9/nas11/availability' and p == b'online' for t, p in received[marker:]) >= 1, 8)
        new = received[marker:]
        state_index = next(i for i, (t, p) in enumerate(new) if t == 'nuc9/nas11/state' and json.loads(p)['revision'] == 399)
        online_index = next(i for i, (t, p) in enumerate(new) if t == 'nuc9/nas11/availability' and p == b'online')
        assert state_index < online_index
    finally:
        unsubscribe(); adapter.stop(); observer.loop_stop(); observer.disconnect(); controlled.close()


def test_abrupt_adapter_process_exit_publishes_broker_lwt(broker, tmp_path):
    config = load_config(Path(__file__).parents[2] / 'config' / 'example.yaml')
    config = mqtt_config(config, broker)
    config_path = tmp_path / 'child.yaml'
    import yaml
    config_path.write_text(yaml.safe_dump(config.model_dump(mode='json')))
    child = tmp_path / 'child.py'
    child.write_text('''
import sys,time,yaml
from pathlib import Path
from ha_nuc9_ec.config import AppConfig
from ha_nuc9_ec.mqtt import MQTTAdapter
c=AppConfig.model_validate(yaml.safe_load(Path(sys.argv[1]).read_text()), context={"normalized_duration":True})
a=MQTTAdapter(c.mqtt, lambda *args: None)
a.start()
while not a._client.is_connected(): time.sleep(.01)
print("READY", flush=True)
time.sleep(60)
''')
    received = []
    observer = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='lwt-observer', clean_session=True)
    observer.on_connect = lambda client, userdata, flags, reason, properties: client.subscribe('nuc9/nas11/availability', 1)
    observer.on_message = lambda client, userdata, message: received.append(bytes(message.payload))
    observer.connect('127.0.0.1', broker); observer.loop_start()
    process = subprocess.Popen([sys.executable, str(child), str(config_path)], stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == 'READY'
        process.kill(); process.wait(3)
        assert wait_until(lambda: b'offline' in received)
    finally:
        if process.poll() is None:
            process.kill(); process.wait(3)
        observer.loop_stop(); observer.disconnect()


def test_per_source_staleness_and_reload_delete_retained_discovery(broker):
    config = mqtt_config(load_config(Path(__file__).parents[2] / 'config' / 'example.yaml'), broker)
    now = [0.0]
    controlled = ControllerLoop(config, clock=lambda: now[0])
    adapter = MQTTAdapter(config.mqtt, controlled.submit)
    unsubscribe = controlled.controller.subscribe(adapter.publish_state)
    adapter.publish_state(controlled.controller.snapshot())
    received = []
    observer = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='source-observer', clean_session=True)
    observer.on_connect = lambda client, userdata, flags, reason, properties: client.subscribe('#', 1)
    observer.on_message = lambda client, userdata, message: received.append((message.topic, bytes(message.payload), message.retain))
    observer.connect('127.0.0.1', broker); observer.loop_start()
    try:
        adapter.start()
        assert wait_until(lambda: any(t == 'nuc9/nas11/availability' and p == b'online' for t, p, _ in received))
        now[0] = 1.0
        adapter.publish_state(controlled.controller.snapshot())
        cpu_topic = f"nuc9/nas11/source/{'cpu_package'.encode().hex()}/availability"
        pch_topic = f"nuc9/nas11/source/{'pch'.encode().hex()}/availability"
        assert wait_until(lambda: any(t == cpu_topic and p == b'offline' for t, p, _ in received))
        assert any(t == pch_topic and p == b'online' for t, p, _ in received)
        now[0] = 6.0
        adapter.publish_state(controlled.controller.snapshot())
        assert wait_until(lambda: any(t == pch_topic and p == b'offline' for t, p, _ in received))

        raw = controlled.controller.config.model_dump(mode='python')
        raw['fans']['sysfan']['override']['inputs'] = [item for item in raw['fans']['sysfan']['override']['inputs'] if item['source'] != 'pch']
        raw['sources'].pop('pch')
        candidate = type(config).model_validate(raw, context={'normalized_duration': True})
        samples = {'cpu_package': Sample('cpu_package', 50, now[0], None)}
        assert controlled.call(controlled.controller.reload(candidate, 'remove-pch', samples=samples)).result(2).ok
        topic = f"homeassistant/sensor/{config.device.id}/source_{'pch'.encode().hex()}_temperature/config"
        assert wait_until(lambda: any(t == topic and p == b'' for t, p, _ in received))
        replay = []
        newcomer = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='after-delete', clean_session=True)
        newcomer.on_connect = lambda client, userdata, flags, reason, properties: client.subscribe(topic, 1)
        newcomer.on_message = lambda client, userdata, message: replay.append(message.payload)
        newcomer.connect('127.0.0.1', broker); newcomer.loop_start(); time.sleep(.4)
        newcomer.loop_stop(); newcomer.disconnect()
        assert replay == []
    finally:
        unsubscribe(); adapter.stop(); observer.loop_stop(); observer.disconnect(); controlled.close()
