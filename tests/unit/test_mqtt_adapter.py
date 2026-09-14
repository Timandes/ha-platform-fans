import dataclasses
import threading
import time

from ha_nuc9_ec.controller import Controller
from ha_nuc9_ec.discovery import build_discovery
from ha_nuc9_ec.hardware.mock import MockBackend
from ha_nuc9_ec.mqtt import MQTTAdapter


def enabled(example_config):
    raw = example_config.model_dump(mode='python')
    raw['mqtt'].update(enabled=True, username=None, password_file=None)
    return type(example_config).model_validate(raw, context={'normalized_duration': True})


def snapshot(config, revision=0):
    state = Controller(config, MockBackend(), time.monotonic).snapshot()
    return dataclasses.replace(state, revision=revision)


def test_puback_barrier_preserves_newer_state_sequence(example_config):
    config = enabled(example_config)
    adapter = MQTTAdapter(config.mqtt, lambda *_: None)
    old, new = snapshot(config, 0), snapshot(config, 1)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    revisions = []
    def announce(state, *, force_discovery=False):
        revisions.append(state.revision)
        if len(revisions) == 1:
            entered.set(); assert release.wait(2)
        if len(revisions) == 2:
            finished.set()
        return True
    adapter._announce = announce
    adapter._publish = lambda *args, **kwargs: True
    with adapter._lock:
        adapter._connected = True; adapter._generation = adapter._synced_generation = 1
    worker = threading.Thread(target=adapter._work)
    worker.start()
    try:
        adapter.publish_state(old)
        assert entered.wait(2)
        adapter.publish_state(new)
        release.set()
        assert finished.wait(2)
        assert revisions[:2] == [0, 1]
        assert adapter._state_done == adapter._state_sequence == 2
    finally:
        adapter._stop.set(); adapter._wake.set(); worker.join(2)


def test_puback_barrier_preserves_birth_and_new_connection_generation(example_config):
    config = enabled(example_config)
    adapter = MQTTAdapter(config.mqtt, lambda *_: None)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    announces, online_generations = [], []
    def announce(state, *, force_discovery=False):
        announces.append(adapter._generation)
        if len(announces) == 1:
            entered.set(); assert release.wait(2)
        else:
            finished.set()
        return True
    def publish(topic, payload, *, retain):
        if topic.endswith('/availability') and payload == 'online':
            online_generations.append(adapter._generation)
        return True
    adapter._announce, adapter._publish = announce, publish
    with adapter._lock:
        adapter._latest = snapshot(config); adapter._state_sequence = 1
        adapter._connected = True; adapter._generation = 1
    worker = threading.Thread(target=adapter._work); worker.start(); adapter._wake.set()
    try:
        assert entered.wait(2)
        with adapter._lock:
            adapter._birth_sequence += 1
            adapter._generation = 2
            adapter._connected = True
        adapter._wake.set(); release.set()
        assert finished.wait(2)
        assert announces[:2] == [1, 2]
        assert online_generations == [2]
        assert adapter._birth_done == adapter._birth_sequence
        assert adapter._synced_generation == 2
    finally:
        adapter._stop.set(); adapter._wake.set(); worker.join(2)


def test_partial_publish_disconnect_remove_reconnect_reconciles_discovery(example_config):
    old = enabled(example_config)
    raw = old.model_dump(mode='python')
    raw['sources']['temporary-source'] = raw['sources']['cpu_package']
    added = type(old).model_validate(raw, context={'normalized_duration': True})
    adapter = MQTTAdapter(old.mqtt, lambda *_: None)
    adapter._connected = True
    adapter._discovery_topics = set(build_discovery(old))
    new_topics = set(build_discovery(added)) - adapter._discovery_topics
    attempts = []
    failed = [False]
    def partial(topic, payload, *, retain):
        attempts.append((topic, payload))
        if topic in new_topics and not failed[0]:
            failed[0] = True
            return False
        return True
    adapter._publish = partial
    assert not adapter._announce(snapshot(added), force_discovery=True)
    uncertain = adapter._discovery_topics & new_topics
    assert uncertain
    with adapter._lock:
        adapter._connected = False
        adapter._generation = 1
        adapter._latest = snapshot(old)
        adapter._state_sequence += 1
        adapter._connected = True
        adapter._generation = 2
    attempts.clear()
    assert adapter._announce(snapshot(old), force_discovery=True)
    assert all((topic, b'') in attempts for topic in uncertain)
    assert not (adapter._discovery_topics & new_topics)
