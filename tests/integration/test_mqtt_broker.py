import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
import getpass

import paho.mqtt.client as mqtt
import pytest

from ha_nuc9_ec.config import load_config
from ha_nuc9_ec.controller import Controller
from ha_nuc9_ec.hardware.mock import MockBackend
from ha_nuc9_ec.model import CommandResult
from ha_nuc9_ec.mqtt import MQTTAdapter


MOSQUITTO = os.environ.get("MOSQUITTO_BIN", "/private/tmp/mosquitto-2.0.22/src/mosquitto")


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
    submitted = []
    def submit(changes, request_id):
        submitted.append((request_id, changes))
        return CommandResult(request_id, True, 1)
    adapter = MQTTAdapter(config.mqtt, submit, device_id=config.device.id)
    controller = Controller(config, MockBackend(), time.monotonic)
    adapter.publish_state(controller.snapshot())

    received = []
    ready = threading.Event()
    observer = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="observer", clean_session=True)
    observer.on_connect = lambda client, userdata, flags, reason, properties: client.subscribe("#", qos=1)
    def on_message(client, userdata, message):
        received.append((message.topic, bytes(message.payload), message.retain))
        if message.topic.endswith("/availability") and message.payload == b"online":
            ready.set()
    observer.on_message = on_message
    observer.connect("127.0.0.1", broker)
    observer.loop_start()
    # Store a command before the adapter subscribes; broker replay marks it retained.
    observer.publish("nuc9/nas11/command/control_mode", "bios", qos=1, retain=True).wait_for_publish()
    adapter.start()
    assert ready.wait(4)
    topics = [item[0] for item in received]
    assert topics.index("nuc9/nas11/state") < topics.index("nuc9/nas11/availability")

    observer.publish("homeassistant/status", "online", qos=1)
    observer.publish("nuc9/nas11/set", json.dumps({"request_id": "broker-1", "changes": {"control.mode": "override"}}), qos=1)
    deadline = time.monotonic() + 3
    while not submitted and time.monotonic() < deadline:
        time.sleep(.02)
    assert submitted == [("broker-1", {"control.mode": "override"})]

    time.sleep(.2)
    assert len(submitted) == 1
    assert any(topic == "nuc9/nas11/result" and b"retained" in payload for topic, payload, _ in received)
    adapter.stop()
    observer.loop_stop()
    observer.disconnect()


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
