import dataclasses
import json
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


def eventually(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        threading.Event().wait(.005)
    return False


def test_puback_barrier_preserves_newer_state_sequence(example_config):
    config = enabled(example_config)
    adapter = MQTTAdapter(config.mqtt, lambda *_: None)
    old, new = snapshot(config, 0), snapshot(config, 1)
    entered, release = threading.Event(), threading.Event()
    revisions = []
    def announce(state, *, force_discovery=False, expected_generation=None):
        revisions.append(state.revision)
        if len(revisions) == 1:
            entered.set(); assert release.wait(2)
        return True
    adapter._announce = announce
    adapter._publish = lambda *args, **kwargs: True
    with adapter._lock:
        adapter._client = object(); adapter._connected = True; adapter._generation = adapter._synced_generation = 1
    worker = threading.Thread(target=adapter._work)
    worker.start()
    try:
        adapter.publish_state(old)
        assert entered.wait(2)
        adapter.publish_state(new)
        release.set()
        assert eventually(lambda: adapter._state_done == adapter._state_sequence == 2)
        assert revisions[:2] == [0, 1]
    finally:
        adapter._stop.set(); adapter._wake.set(); worker.join(2)


def test_puback_barrier_preserves_birth_and_new_connection_generation(example_config):
    config = enabled(example_config)
    adapter = MQTTAdapter(config.mqtt, lambda *_: None)
    entered, release = threading.Event(), threading.Event()
    announces, online_generations = [], []
    def announce(state, *, force_discovery=False, expected_generation=None):
        announces.append(adapter._generation)
        if len(announces) == 1:
            entered.set(); assert release.wait(2)
        return True
    def publish(topic, payload, *, retain, **kwargs):
        if topic.endswith('/availability') and payload == 'online':
            online_generations.append(adapter._generation)
        return True
    adapter._announce, adapter._publish = announce, publish
    with adapter._lock:
        adapter._latest = snapshot(config); adapter._state_sequence = 1
        adapter._client = object(); adapter._connected = True; adapter._generation = 1
    worker = threading.Thread(target=adapter._work); worker.start(); adapter._wake.set()
    try:
        assert entered.wait(2)
        with adapter._lock:
            adapter._birth_sequence += 1
            adapter._generation = 2
            adapter._connected = True
        adapter._wake.set(); release.set()
        assert eventually(lambda: adapter._birth_done == adapter._birth_sequence and
                          adapter._synced_generation == 2 and online_generations == [2])
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
    adapter._discovery_fingerprint = json.dumps(old.model_dump(mode='json'), sort_keys=True, separators=(',', ':'))
    new_topics = set(build_discovery(added)) - adapter._discovery_topics
    attempts = []
    failed = [False]
    def partial(topic, payload, *, retain, **kwargs):
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
    assert adapter._announce(snapshot(old), force_discovery=False)
    assert all((topic, b'') in attempts for topic in uncertain)
    assert not (adapter._discovery_topics & new_topics)


class PublishInfo:
    rc = 0
    def __init__(self, entered, release, published=True):
        self.entered, self.release, self.published = entered, release, published
    def wait_for_publish(self, timeout=None):
        self.entered.set()
        assert self.release.wait(timeout)
    def is_published(self):
        return self.published


class FakeClient:
    def __init__(self, info):
        self.info, self.messages = info, []
    def publish(self, topic, payload, qos, retain):
        self.messages.append((topic, payload))
        return self.info


class EnqueueBlockingClient(FakeClient):
    def __init__(self, info, entered, release):
        super().__init__(info)
        self.entered, self.release = entered, release
    def publish(self, topic, payload, qos, retain):
        self.entered.set()
        assert self.release.wait(2)
        return super().publish(topic, payload, qos, retain)


def test_online_enqueue_boundary_is_serialized_with_generation_change(example_config):
    config = enabled(example_config)
    adapter = MQTTAdapter(config.mqtt, lambda *_: None)
    entered, release = threading.Event(), threading.Event()
    ack_release = threading.Event(); ack_release.set()
    old = EnqueueBlockingClient(PublishInfo(threading.Event(), ack_release), entered, release)
    with adapter._lock:
        adapter._client = old; adapter._connected = True; adapter._generation = 1
    result = []
    publishing = threading.Thread(target=lambda: result.append(adapter._publish(
        'nuc9/nas11/availability', 'online', retain=True, expected_generation=1)))
    publishing.start(); assert entered.wait(2)
    changed = threading.Event()
    def change_generation():
        with adapter._lock:
            adapter._generation = 2
        changed.set()
    changer = threading.Thread(target=change_generation); changer.start()
    assert not changed.wait(.05)
    release.set(); publishing.join(2); changer.join(2)
    assert result == [True] and changed.is_set()


def test_uncertain_old_online_is_not_enqueued_on_fresh_transport(example_config):
    config = enabled(example_config)
    adapter = MQTTAdapter(config.mqtt, lambda *_: None)
    entered, release = threading.Event(), threading.Event()
    old = FakeClient(PublishInfo(entered, release, published=False))
    new = FakeClient(PublishInfo(threading.Event(), threading.Event()))
    with adapter._lock:
        adapter._client = old; adapter._connected = True; adapter._generation = 1
    result = []
    publishing = threading.Thread(target=lambda: result.append(adapter._publish(
        'nuc9/nas11/availability', 'online', retain=True, expected_generation=1)))
    publishing.start(); assert entered.wait(2)
    adapter._retire_transport(old)
    with adapter._lock:
        adapter._client = new; adapter._connected = True; adapter._generation = 2
    release.set(); publishing.join(2)
    assert result == [False]
    assert len(old.messages) == 1
    assert new.messages == []
    assert not adapter._publish('nuc9/nas11/availability', 'online', retain=True, expected_generation=1)
    assert new.messages == []


def test_stop_during_worker_transport_start_leaves_no_running_loop(example_config, monkeypatch):
    """A SIGTERM-style stop must own cleanup even while reconnect is starting."""
    from ha_nuc9_ec.mqtt import mqtt

    adapter = MQTTAdapter(enabled(example_config).mqtt, lambda *_: None)
    entered, release, stop_reached = threading.Event(), threading.Event(), threading.Event()
    calls, running = [], []

    class ObservedLock:
        """Release the startup barrier at contention or at premature cleanup."""
        def __init__(self):
            self.lock = threading.Lock()
        def __enter__(self):
            if not self.lock.acquire(blocking=False):
                if threading.current_thread().name == 'signal-stop':
                    stop_reached.set()
                self.lock.acquire()
            return self
        def __exit__(self, *args):
            self.lock.release()

    adapter._lock = ObservedLock()
    def connect_async(client, *args):
        entered.set()
        assert release.wait(3)
        calls.append('connect_async')
    def loop_start(client):
        calls.append('loop_start')
        running.append(client)
    def disconnect(client):
        calls.append('disconnect')
    def loop_stop(client):
        # A callback must still be able to take the adapter lock during join.
        callback_done = threading.Event()
        def callback():
            with adapter._lock:
                callback_done.set()
        callback_thread = threading.Thread(target=callback)
        callback_thread.start()
        assert callback_done.wait(2)
        callback_thread.join(2)
        calls.append('loop_stop')
        running.clear()
        stop_reached.set()

    monkeypatch.setattr(mqtt.Client, 'connect_async', connect_async)
    monkeypatch.setattr(mqtt.Client, 'loop_start', loop_start)
    monkeypatch.setattr(mqtt.Client, 'disconnect', disconnect)
    monkeypatch.setattr(mqtt.Client, 'loop_stop', loop_stop)
    adapter._worker = threading.Thread(target=adapter._work)
    stopper = threading.Thread(target=adapter.stop, name='signal-stop')
    adapter._worker.start()
    adapter._wake.set()
    try:
        assert entered.wait(2)
        stopper.start()
        assert stop_reached.wait(2)
        release.set()
        stopper.join(3)
        assert not stopper.is_alive()
        assert not adapter._worker.is_alive()  # Exercise stop's real worker join.
        assert adapter._client is None
        assert not running, calls
        assert calls[-1] == 'loop_stop'
    finally:
        release.set()
        adapter._stop.set()
        adapter._wake.set()
        if stopper.ident is not None:
            stopper.join(3)
        adapter._worker.join(3)
