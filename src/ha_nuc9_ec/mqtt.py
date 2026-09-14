"""Strict MQTT commands and nonblocking Paho network adapter."""
from __future__ import annotations

import json
import logging
import queue
import ssl
import threading
import time
import re
import uuid
from collections import deque
from concurrent.futures import Future
from dataclasses import asdict
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import paho.mqtt.client as mqtt

from .config import MQTTConfig
from .discovery import build_discovery
from .model import CommandResult, StateSnapshot


class CommandRejected(ValueError):
    pass


_PUBLIC_PATH = re.compile(
    r"(?:control\.mode|fans\.(?:cpufan|sysfan)\.override\.(?:mode|fixed\.duty_percent|"
    r"inputs\.\d+\.(?:custom\.(?:minimum_temperature_c|minimum_duty_percent|duty_increment_percent_per_c)|boost_above_c)))\Z"
)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CommandRejected(f"duplicate JSON field {key}")
        result[key] = value
    return result


def parse_command(topic: str, payload: bytes, retained: bool) -> tuple[str, dict[str, object]]:
    if retained:
        raise CommandRejected("retained commands are rejected")
    if len(payload) > 16 * 1024:
        raise CommandRejected("command exceeds 16 KiB")
    if topic.endswith("/set"):
        try:
            body = json.loads(payload, object_pairs_hook=_object)
        except CommandRejected:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CommandRejected("invalid JSON command") from error
        if not isinstance(body, dict) or set(body) != {"request_id", "changes"}:
            raise CommandRejected("command requires only request_id and changes")
        if not isinstance(body["request_id"], str) or not body["request_id"] or not isinstance(body["changes"], dict):
            raise CommandRejected("invalid request_id or changes")
        for path in body["changes"]:
            if not isinstance(path, str) or _PUBLIC_PATH.fullmatch(path) is None:
                raise CommandRejected(f"{path}: path is not allowed")
        return body["request_id"], body["changes"]
    marker = "/command/"
    if marker not in topic:
        raise CommandRejected("unknown command topic")
    entity = topic.rsplit(marker, 1)[1]
    simple = {"control_mode": "control.mode"}
    match = re_fullmatch_entity(entity)
    path = simple.get(entity)
    kind = "text"
    if match:
        fan, rest = match
        if rest == "override_mode":
            path = f"fans.{fan}.override.mode"
        elif rest == "fixed_duty_percent":
            path, kind = f"fans.{fan}.override.fixed.duty_percent", "int"
        else:
            source_token, suffix = rest.split("_", 1)
            path = f"fans.{fan}.override.inputs.source:{source_token}." + (suffix if suffix == "boost_above_c" else f"custom.{suffix}")
            kind = "int" if suffix == "minimum_duty_percent" else "float"
    if path is None:
        raise CommandRejected("unknown command entity")
    try:
        text = payload.decode("utf-8")
        value = int(text) if kind == "int" else float(text) if kind == "float" else text
        if kind != "text" and text.strip().lower() in {"true", "false", "nan", "inf", "+inf", "-inf"}:
            raise ValueError
    except (UnicodeDecodeError, ValueError) as error:
        raise CommandRejected("invalid scalar payload") from error
    return f"ha-{uuid.uuid4().hex}", {path: value}


def re_fullmatch_entity(entity: str):
    match = re.fullmatch(r"(cpufan|sysfan)_(override_mode|fixed_duty_percent|input_source_([0-9a-f]+)_(minimum_temperature_c|minimum_duty_percent|duty_increment_percent_per_c|boost_above_c))", entity)
    if not match:
        return None
    rest = match.group(2)
    if rest.startswith("input_"):
        try:
            bytes.fromhex(match.group(3)).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
        rest = f"{match.group(3)}_{match.group(4)}"
    return match.group(1), rest


@dataclass(frozen=True)
class PreparedMQTT:
    password: str | None
    ssl_context: ssl.SSLContext | None


def prepare_mqtt(config: MQTTConfig) -> PreparedMQTT:
    """Read local secrets before hardware is opened; never include them in errors."""
    if not config.enabled:
        return PreparedMQTT(None, None)
    password = None
    if config.password_file:
        try:
            password = Path(config.password_file).read_text(encoding="utf-8").rstrip("\r\n")
        except OSError as error:
            raise ValueError(f"cannot read MQTT password file {config.password_file}") from error
    parsed = urlsplit(config.broker)
    context = None
    if parsed.scheme == "ssl":
        if config.tls is None:
            raise ValueError("ssl MQTT broker requires TLS configuration")
        try:
            context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=config.tls.ca_file)
            context.check_hostname = True
            context.verify_mode = ssl.CERT_REQUIRED
            if config.tls.certificate_file:
                context.load_cert_chain(config.tls.certificate_file, config.tls.key_file, password="")
        except (OSError, ssl.SSLError) as error:
            raise ValueError("invalid MQTT TLS certificate configuration") from error
    elif config.tls is not None:
        raise ValueError("MQTT TLS configuration requires an ssl broker URL")
    return PreparedMQTT(password, context)


def validate_credentials(config: MQTTConfig) -> str | None:
    """Compatibility helper; new callers should retain prepare_mqtt's context."""
    return prepare_mqtt(config).password


class MQTTAdapter:
    """Paho owns network I/O; a bounded worker queue isolates controller work."""
    def __init__(self, config: MQTTConfig, submit, *, device_id: str | None = None,
                 prepared: PreparedMQTT | None = None):
        self.config = config.model_copy(deep=True)
        self.submit = submit
        self.device_id = device_id
        self._prepared = prepared
        self._password: str | None = None
        self._commands: queue.Queue = queue.Queue(maxsize=256)
        self._results: deque[dict] = deque()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._client: mqtt.Client | None = None
        self._retired_clients: deque[mqtt.Client] = deque()
        self._retry_at = 0.0
        self._retry_delay = 1.0
        self._latest: StateSnapshot | None = None
        self._state_sequence = 0
        self._state_done = 0
        self._connected = False
        self._online = False
        self._generation = 0
        self._synced_generation = 0
        self._birth_sequence = 0
        self._birth_done = 0
        self._packet_ids: dict[tuple[int, int], str] = {}
        self._discovery_topics: set[str] = set()
        self._discovery_fingerprint: str | None = None
        self._wall_anchor = time.time() - time.monotonic()

    def start(self) -> None:
        if not self.config.enabled or self._worker is not None:
            return
        if self._prepared is None:
            self._prepared = prepare_mqtt(self.config)
        self._password = self._prepared.password
        self._start_transport()
        self._worker = threading.Thread(target=self._work, name="mqtt-adapter", daemon=True)
        self._worker.start()

    def _start_transport(self) -> None:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.config.client_id,
                             clean_session=True, protocol=mqtt.MQTTv311, reconnect_on_failure=False)
        client.max_queued_messages_set(256)
        client.max_inflight_messages_set(20)
        if self.config.username is not None:
            client.username_pw_set(self.config.username, self._password)
        parsed = urlsplit(self.config.broker)
        if parsed.scheme == "ssl":
            assert self._prepared.ssl_context is not None
            client.tls_set_context(self._prepared.ssl_context)
        prefix = self.config.topic_prefix
        client.will_set(f"{prefix}/availability", "offline", qos=1, retain=True)
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        client.on_connect = self._on_connect
        client.on_connect_fail = self._on_connect_fail
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        with self._lock:
            if self._stop.is_set():
                return
            self._client = client
            # Commit ownership and start atomically against stop's client lookup.
            # These calls do not wait for callbacks; PUBACK and loop_stop/join
            # must remain outside this lock.
            client.connect_async(parsed.hostname, parsed.port)
            client.loop_start()

    def publish_state(self, state: StateSnapshot) -> None:
        if self.config.enabled:
            with self._lock:
                self._latest = state
                self._state_sequence += 1
            self._wake.set()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        with self._lock:
            if client is not self._client:
                return
        if reason_code != 0:
            logging.warning("MQTT connection rejected: %s", reason_code)
            self._retire_transport(client)
            return
        prefix = self.config.topic_prefix
        client.subscribe([(f"{prefix}/set", 1), (f"{prefix}/command/+", 1), ("homeassistant/status", 1)])
        with self._lock:
            if client is not self._client:
                return
            self._connected = True
            self._online = False
            self._generation += 1
            self._retry_delay = 1.0
            self._packet_ids.clear()
        self._wake.set()

    def _retire_transport(self, client) -> None:
        with self._lock:
            if client is not self._client:
                return
            self._client = None
            self._connected = False
            self._online = False
            self._retired_clients.append(client)
            self._retry_at = time.monotonic() + self._retry_delay
            self._retry_delay = min(30.0, self._retry_delay * 2)
        self._wake.set()

    def _on_connect_fail(self, client, userdata):
        self._retire_transport(client)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        self._retire_transport(client)

    def _on_message(self, client, userdata, message):
        with self._lock:
            if client is not self._client:
                return
            origin_generation = self._generation
        if message.topic == "homeassistant/status":
            if bytes(message.payload) == b"online":
                with self._lock:
                    self._birth_sequence += 1
                self._wake.set()
            return
        item = (origin_generation, message.topic, bytes(message.payload), bool(message.retain), message.mid, bool(message.dup))
        try:
            self._commands.put_nowait(item)
        except queue.Full:
            result = json.dumps({"request_id": None, "ok": False, "error": "command queue is full",
                                 "revision": self._latest.revision if self._latest else 0}, separators=(",", ":"))
            info = client.publish(f"{self.config.topic_prefix}/result", result, qos=1, retain=False)
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                logging.error("MQTT command overload result could not be queued")
        self._wake.set()

    def _publish(self, topic: str, payload, *, retain: bool,
                 expected_generation: int | None = None) -> bool:
        if not isinstance(payload, (str, bytes)):
            payload = json.dumps(payload, separators=(",", ":"), allow_nan=False)
        with self._lock:
            client = self._client
            if (client is None or not self._connected or
                    (expected_generation is not None and self._generation != expected_generation)):
                return False
            # Client identity and generation cannot change between validation and
            # Paho's nonblocking enqueue. PUBACK waiting remains outside the lock.
            info = client.publish(topic, payload, qos=1, retain=retain)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            return False
        try:
            info.wait_for_publish(timeout=2)
        except (RuntimeError, ValueError):
            return False
        return info.is_published()

    def _state_payload(self, state: StateSnapshot) -> dict:
        duty = asdict(state.duty) if state.duty is not None else None
        data = {
            "state": state.state,
            "requested_mode": state.requested_mode,
            "applied_mode": state.applied_mode,
            "revision": state.revision,
            "target_duty": duty,
            "rpm": state.rpm,
            "sources": {key: asdict(value) for key, value in state.sources.items()},
            "fault": state.fault,
            "configuration": {
                "control": {"mode": state.configuration["control"]["mode"]},
                "fans": state.configuration["fans"],
            },
        }
        for source_id, sample in data["sources"].items():
            last = state.last_success.get(source_id)
            sample["last_success_at"] = None if last is None else time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._wall_anchor + last))
        return data

    def _announce(self, state: StateSnapshot, *, force_discovery: bool = False,
                  expected_generation: int | None = None) -> bool:
        from .config import AppConfig
        config = AppConfig.model_validate(state.configuration, context={"normalized_duration": True})
        fingerprint = json.dumps(state.configuration, sort_keys=True, separators=(",", ":"))
        messages = build_discovery(config) if config.mqtt.discovery.enabled else {}
        if (force_discovery or fingerprint != self._discovery_fingerprint or
                self._discovery_topics != set(messages)):
            for removed in self._discovery_topics - messages.keys():
                if not self._publish(removed, b"", retain=True, expected_generation=expected_generation):
                    self._discovery_fingerprint = None
                    return False
                self._discovery_topics.discard(removed)
            for topic, payload in messages.items():
                # Once attempted, conservatively remember a topic even if the
                # PUBACK becomes uncertain; a later target can then delete it.
                self._discovery_topics.add(topic)
                if not self._publish(topic, payload, retain=True, expected_generation=expected_generation):
                    self._discovery_fingerprint = None
                    return False
            self._discovery_fingerprint = fingerprint
        if not self._publish(f"{self.config.topic_prefix}/state", self._state_payload(state), retain=True,
                             expected_generation=expected_generation):
            return False
        for source_id, sample in state.sources.items():
            if not self._publish(f"{self.config.topic_prefix}/source/{source_id.encode('utf-8').hex()}/availability",
                                 "online" if sample.error is None else "offline", retain=True,
                                 expected_generation=expected_generation):
                return False
        return True

    def _complete(self, request_id: str, changes: dict[str, object]) -> None:
        try:
            result = self.submit(changes, request_id)
            if isinstance(result, Future) or hasattr(result, "result"):
                result = result.result()
            if not isinstance(result, CommandResult):
                raise TypeError("command submitter did not return CommandResult")
        except Exception as error:
            revision = self._latest.revision if self._latest else 0
            result = CommandResult(request_id, False, revision, str(error))
        with self._lock:
            self._results.append(asdict(result))

    def _handle_command(self, item) -> None:
        origin_generation, topic, payload, retained, mid, duplicate = item
        try:
            request_id, changes = parse_command(topic, payload, retained)
            if "/command/" in topic:
                with self._lock:
                    key = (origin_generation, mid)
                    if duplicate and key in self._packet_ids:
                        request_id = self._packet_ids[key]
                    else:
                        self._packet_ids[key] = request_id
                        if len(self._packet_ids) > 512:
                            self._packet_ids.pop(next(iter(self._packet_ids)))
            self._complete(request_id, changes)
        except CommandRejected as error:
            with self._lock:
                self._results.append({"request_id": None, "ok": False, "error": str(error),
                                      "revision": self._latest.revision if self._latest else 0})

    def _work(self) -> None:
        last_periodic = time.monotonic()
        while not self._stop.is_set():
            self._wake.wait(.1)
            self._wake.clear()
            while True:
                with self._lock:
                    retired = self._retired_clients.popleft() if self._retired_clients else None
                if retired is None:
                    break
                retired.loop_stop()
            with self._lock:
                should_reconnect = (self._client is None and not self._stop.is_set() and
                                    time.monotonic() >= self._retry_at)
            if should_reconnect:
                self._start_transport()
            for _ in range(32):
                with self._lock:
                    if len(self._results) >= 512:
                        break
                try:
                    self._handle_command(self._commands.get_nowait())
                except queue.Empty:
                    break
            now = time.monotonic()
            with self._lock:
                connected, state = self._connected, self._latest
                generation = self._generation
                state_sequence = self._state_sequence
                birth_sequence = self._birth_sequence
                force = generation != self._synced_generation or birth_sequence != self._birth_done
                dirty = state_sequence != self._state_done
                due = now - last_periodic >= 5
            if connected and state is not None and (force or dirty or due):
                if self._announce(state, force_discovery=force, expected_generation=generation):
                    with self._lock:
                        if self._generation == generation:
                            self._synced_generation = generation
                        if self._birth_sequence == birth_sequence:
                            self._birth_done = birth_sequence
                        if self._state_sequence == state_sequence:
                            self._state_done = state_sequence
                    last_periodic = now
                    with self._lock:
                        may_announce_online = (self._connected and self._generation == generation and
                                               self._synced_generation == generation and not self._online)
                    if may_announce_online and self._publish(
                            f"{self.config.topic_prefix}/availability", "online", retain=True,
                            expected_generation=generation):
                        with self._lock:
                            if self._connected and self._generation == generation:
                                self._online = True
            while connected:
                with self._lock:
                    result = self._results[0] if self._results else None
                if result is None or not self._publish(f"{self.config.topic_prefix}/result", result, retain=False,
                                                       expected_generation=generation):
                    break
                with self._lock:
                    if self._results and self._results[0] is result:
                        self._results.popleft()

    def stop(self) -> None:
        if not self.config.enabled:
            return
        self._stop.set()
        self._wake.set()
        with self._lock:
            client = self._client
        if client:
            self._publish(f"{self.config.topic_prefix}/availability", "offline", retain=True,
                          expected_generation=self._generation)
            client.disconnect()
            client.loop_stop()
        if self._worker:
            self._worker.join(timeout=2)
        while self._retired_clients:
            self._retired_clients.popleft().loop_stop()
        self._client = None
        self._password = None
