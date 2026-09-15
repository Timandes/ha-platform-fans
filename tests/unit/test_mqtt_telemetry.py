import dataclasses
import json
import threading
import time

import pytest

from ha_nuc9_ec.config import ConfigError, load_config
from ha_nuc9_ec.controller import Controller
from ha_nuc9_ec.hardware.mock import MockBackend
from ha_nuc9_ec.model import Sample
from ha_nuc9_ec.mqtt import MQTTAdapter


def wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(.005)
    assert predicate()


class Transport:
    def __init__(self):
        self.messages = []
        self.lock = threading.Lock()

    def publish(self, topic, payload, qos, retain):
        with self.lock:
            self.messages.append((topic, json.loads(payload) if topic.endswith('/state') else payload))
        class Ack:
            rc = 0
            def wait_for_publish(self, timeout):
                pass
            def is_published(self):
                return True
        return Ack()

    def values(self, suffix):
        with self.lock:
            return [payload for topic, payload in self.messages if topic.endswith(suffix)]


@pytest.fixture
def running_adapter(example_config):
    raw = example_config.model_dump(mode='python')
    raw['mqtt'].update(enabled=True, username=None, password_file=None)
    config = type(example_config).model_validate(raw, context={'normalized_duration': True})
    now = [100.0]
    adapter = MQTTAdapter(config.mqtt, lambda *_: None)
    adapter._monotonic = lambda: now[0]
    transport = Transport()
    with adapter._lock:
        adapter._client = transport
        adapter._connected = True
        adapter._generation = 1
    worker = threading.Thread(target=adapter._work)
    adapter._worker = worker
    worker.start()
    state = Controller(config, MockBackend(), time.monotonic).snapshot()
    state = dataclasses.replace(state, sources={'cpu_package': Sample('cpu_package', 40, 100, None)}, last_success={'cpu_package': 100})
    try:
        adapter.publish_state(state)
        wait_for(lambda: adapter._synced_generation == 1 and adapter._online)
        yield adapter, transport, now, state
    finally:
        adapter._stop.set(); adapter._wake.set(); worker.join(2)
        assert not worker.is_alive()


def test_frequent_samples_are_coalesced_until_interval_and_keep_latest(running_adapter):
    adapter, transport, now, state = running_adapter
    for n in range(1, 41):
        now[0] = 100 + n / 10
        adapter.publish_state(dataclasses.replace(state, sources={'cpu_package': Sample('cpu_package', n, now[0], None)}))
        threading.Event().wait(.002)
    assert len(transport.values('/state')) == 1
    now[0] = 105
    adapter._wake.set()
    wait_for(lambda: len(transport.values('/state')) == 2)
    assert transport.values('/state')[-1]['sources']['cpu_package']['celsius'] == 40
    assert transport.values('/state')[-1]['sources']['cpu_package']['read_at'] == 104
    # Periodic heartbeat does not repeat unchanged source availability.
    source = '/source/6370755f7061636b616765/availability'
    assert transport.values(source) == ['online']
    now[0] = 110
    adapter._wake.set()
    wait_for(lambda: len(transport.values('/state')) == 3)
    assert transport.values(source) == ['online']


def test_fault_source_change_and_revision_bypass_interval(running_adapter):
    adapter, transport, now, state = running_adapter
    now[0] = 100.1
    failed = dataclasses.replace(state, state='fault', fault='sensor failed', sources={'cpu_package': Sample('cpu_package', None, 100.1, 'failed')})
    adapter.publish_state(failed)
    wait_for(lambda: len(transport.values('/state')) == 2)
    assert transport.values('/state')[-1]['fault'] == 'sensor failed'
    assert transport.values('/source/6370755f7061636b616765/availability') == ['online', 'offline']
    adapter.publish_state(dataclasses.replace(state, revision=1))
    wait_for(lambda: len(transport.values('/state')) == 3)
    assert transport.values('/state')[-1]['revision'] == 1
    assert transport.values('/source/6370755f7061636b616765/availability') == ['online', 'offline', 'online']


def test_reconnect_and_ha_birth_republish_availability_without_wait(running_adapter):
    adapter, transport, now, state = running_adapter
    with adapter._lock:
        adapter._generation = 2
    adapter._wake.set()
    wait_for(lambda: adapter._synced_generation == 2)
    assert transport.values('/source/6370755f7061636b616765/availability') == ['online', 'online']
    with adapter._lock:
        adapter._birth_sequence += 1
    adapter._wake.set()
    wait_for(lambda: adapter._birth_done == 1)
    assert transport.values('/source/6370755f7061636b616765/availability') == ['online', 'online', 'online']


def test_publish_interval_has_compatible_default_and_explicit_units(example_path, tmp_path):
    assert load_config(example_path).mqtt.publish_interval == 5
    p = tmp_path / 'config.yaml'
    old_text = example_path.read_text().replace('  publish_interval: 5s\n', '')
    p.write_text(old_text)
    assert load_config(p).mqtt.publish_interval == 5
    text = old_text.replace('mqtt:\n', 'mqtt:\n  publish_interval: 2s\n')
    p.write_text(text)
    assert load_config(p).mqtt.publish_interval == 2
    for value in ['0s', '-1s', 'nan', 'true', '5', '1s trailing']:
        p.write_text(text.replace('publish_interval: 2s', 'publish_interval: '+value))
        with pytest.raises(ConfigError, match='publish_interval'):
            load_config(p)


def test_failed_source_availability_publish_is_retried(running_adapter):
    adapter, transport, now, state = running_adapter
    # Stop the worker so this transport failure can be exercised deterministically.
    adapter._stop.set(); adapter._wake.set(); adapter._worker.join(2)
    assert not adapter._worker.is_alive()
    changed = dataclasses.replace(state, sources={'cpu_package': Sample('cpu_package', None, 100, 'failed')})
    original = adapter._publish
    attempts = []
    def fail_once(topic, payload, **kwargs):
        if '/source/' in topic and payload == 'offline':
            attempts.append(payload)
            if len(attempts) == 1:
                return False
        return original(topic, payload, **kwargs)
    adapter._publish = fail_once
    assert not adapter._announce(changed)
    assert adapter._announce(changed)
    assert attempts == ['offline', 'offline']
    assert transport.values('/source/6370755f7061636b616765/availability') == ['online', 'offline']


def test_configured_interval_controls_publication_deadline(running_adapter):
    adapter, transport, now, state = running_adapter
    adapter.config.publish_interval = .5
    now[0] = 100.49
    adapter.publish_state(dataclasses.replace(state, rpm=(1000, 1100, 1200)))
    threading.Event().wait(.02)
    assert len(transport.values('/state')) == 1
    now[0] = 100.5
    adapter._wake.set()
    wait_for(lambda: len(transport.values('/state')) == 2)
    assert transport.values('/state')[-1]['rpm'] == [1000, 1100, 1200]


def test_unacknowledged_availability_must_not_hide_recovery_to_old_status(running_adapter):
    adapter, transport, now, state = running_adapter
    adapter._stop.set(); adapter._wake.set(); adapter._worker.join(2)
    original = adapter._publish
    def accepted_without_ack(topic, payload, **kwargs):
        published = original(topic, payload, **kwargs)
        return False if '/source/' in topic and payload == 'offline' else published
    adapter._publish = accepted_without_ack
    failed = dataclasses.replace(state, sources={'cpu_package': Sample('cpu_package', None, 100, 'failed')})
    assert not adapter._announce(failed)
    assert adapter._announce(state)
    assert transport.values('/source/6370755f7061636b616765/availability') == ['online', 'offline', 'online']


def test_unacknowledged_new_source_is_cleared_when_removed(running_adapter):
    adapter, transport, now, state = running_adapter
    adapter._stop.set(); adapter._wake.set(); adapter._worker.join(2)
    import copy
    configuration = copy.deepcopy(state.configuration)
    configuration['sources']['temporary'] = copy.deepcopy(configuration['sources']['cpu_package'])
    added = dataclasses.replace(state, configuration=configuration,
                                sources={**state.sources, 'temporary': Sample('temporary', 40, 100, None)})
    topic = 'nuc9/nas11/source/' + 'temporary'.encode().hex() + '/availability'
    original = adapter._publish
    def accepted_without_ack(name, payload, **kwargs):
        published = original(name, payload, **kwargs)
        return False if name == topic and payload == 'online' else published
    adapter._publish = accepted_without_ack
    assert not adapter._announce(added)
    assert adapter._announce(state)
    assert transport.values(topic) == ['online', b'']


def test_unacknowledged_removal_is_republished_when_source_returns(running_adapter):
    adapter, transport, now, state = running_adapter
    adapter._stop.set(); adapter._wake.set(); adapter._worker.join(2)
    original = adapter._publish
    def accepted_without_ack(topic, payload, **kwargs):
        published = original(topic, payload, **kwargs)
        return False if '/source/' in topic and payload == b'' else published
    adapter._publish = accepted_without_ack
    assert not adapter._announce(dataclasses.replace(state, sources={}))
    assert adapter._announce(state)
    assert transport.values('/source/6370755f7061636b616765/availability') == ['online', b'', 'online']


@pytest.mark.parametrize('failed_topic', ['state', 'availability'])
def test_worker_retries_uncertain_publish_when_recovery_matches_old_key(running_adapter, failed_topic):
    adapter, transport, now, state = running_adapter
    original = adapter._publish
    accepted = threading.Event()
    def accepted_without_ack(topic, payload, **kwargs):
        published = original(topic, payload, **kwargs)
        is_failure = (topic.endswith('/state') and isinstance(payload, dict) and
                      payload['sources']['cpu_package']['error'] is not None) if failed_topic == 'state' else (
                          '/source/' in topic and payload == 'offline')
        if is_failure and not accepted.is_set():
            # Recover while the previous publish still awaits acknowledgement.
            adapter.publish_state(state)
            accepted.set()
            return False
        return published
    adapter._publish = accepted_without_ack
    adapter.publish_state(dataclasses.replace(state, sources={'cpu_package': Sample('cpu_package', None, 100, 'failed')}))
    assert accepted.wait(2)
    wait_for(lambda: adapter._state_done == adapter._state_sequence)
    assert transport.values('/state')[-1]['sources']['cpu_package']['error'] is None
    assert transport.values('/source/6370755f7061636b616765/availability')[-1] == 'online'
    assert now[0] == 100  # No periodic deadline elapsed.
