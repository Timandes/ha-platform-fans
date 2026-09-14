"""Strict MQTT commands and nonblocking Paho network adapter."""
from __future__ import annotations

import hashlib
import json
import logging
import queue
import ssl
import threading
import time
from concurrent.futures import Future
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

import paho.mqtt.client as mqtt

from .config import MQTTConfig
from .discovery import build_discovery
from .model import CommandResult, StateSnapshot


class CommandRejected(ValueError):
    pass


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
            index, suffix = rest.split("_", 1)
            path = f"fans.{fan}.override.inputs.{index}." + (suffix if suffix == "boost_above_c" else f"custom.{suffix}")
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
    digest = hashlib.sha256(topic.encode() + b"\0" + payload).hexdigest()[:24]
    return f"ha-{digest}", {path: value}


def re_fullmatch_entity(entity: str):
    import re
    match = re.fullmatch(r"(cpufan|sysfan)_(override_mode|fixed_duty_percent|input_(\d+)_(minimum_temperature_c|minimum_duty_percent|duty_increment_percent_per_c|boost_above_c))", entity)
    if not match:
        return None
    rest = match.group(2)
    if rest.startswith("input_"):
        rest = f"{match.group(3)}_{match.group(4)}"
    return match.group(1), rest


def validate_credentials(config: MQTTConfig) -> str | None:
    """Read local secrets before hardware is opened; never include them in errors."""
    if not config.enabled:
        return None
    password = None
    if config.password_file:
        try:
            password = Path(config.password_file).read_text(encoding="utf-8").rstrip("\r\n")
        except OSError as error:
            raise ValueError(f"cannot read MQTT password file {config.password_file}") from error
    parsed = urlsplit(config.broker)
    if parsed.scheme == "ssl":
        if config.tls is None:
            raise ValueError("ssl MQTT broker requires TLS configuration")
        for label, path in (("CA", config.tls.ca_file), ("certificate", config.tls.certificate_file), ("key", config.tls.key_file)):
            if path and not Path(path).is_file():
                raise ValueError(f"MQTT TLS {label} file is not readable")
    elif config.tls is not None:
        raise ValueError("MQTT TLS configuration requires an ssl broker URL")
    return password


class MQTTAdapter:
    """Paho owns network I/O; a bounded worker queue isolates controller work."""
    def __init__(self, config: MQTTConfig, submit, *, device_id: str | None = None):
        self.config = config.model_copy(deep=True)
        self.submit = submit
        self.device_id = device_id
        self._password: str | None = None
        self._queue: queue.Queue = queue.Queue(maxsize=256)
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._client: mqtt.Client | None = None
        self._latest: StateSnapshot | None = None
        self._discovery_topics: set[str] = set()
        self._wall_anchor = time.time() - time.monotonic()

    def start(self) -> None:
        if not self.config.enabled or self._client is not None:
            return
        self._password = validate_credentials(self.config)
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.config.client_id,
                             clean_session=True, protocol=mqtt.MQTTv311)
        client.max_queued_messages_set(256)
        client.max_inflight_messages_set(20)
        if self.config.username is not None:
            client.username_pw_set(self.config.username, self._password)
        parsed = urlsplit(self.config.broker)
        if parsed.scheme == "ssl":
            tls = self.config.tls
            assert tls is not None
            client.tls_set(ca_certs=tls.ca_file, certfile=tls.certificate_file, keyfile=tls.key_file,
                           cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS_CLIENT)
            client.tls_insecure_set(False)
        prefix = self.config.topic_prefix
        client.will_set(f"{prefix}/availability", "offline", qos=1, retain=True)
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        self._client = client
        self._worker = threading.Thread(target=self._work, name="mqtt-adapter", daemon=True)
        self._worker.start()
        client.connect_async(parsed.hostname, parsed.port)
        client.loop_start()

    def _put(self, item) -> None:
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                logging.warning("MQTT adapter queue remains full; update dropped")

    def publish_state(self, state: StateSnapshot) -> None:
        if self.config.enabled:
            self._put(("state", state))

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code != 0:
            logging.warning("MQTT connection rejected: %s", reason_code)
            return
        prefix = self.config.topic_prefix
        client.subscribe([(f"{prefix}/set", 1), (f"{prefix}/command/+", 1), ("homeassistant/status", 1)])
        self._put(("connected", None))

    def _on_message(self, client, userdata, message):
        self._put(("message", (message.topic, bytes(message.payload), bool(message.retain))))

    def _publish(self, topic: str, payload, *, retain: bool) -> None:
        client = self._client
        if client is None:
            return
        if not isinstance(payload, (str, bytes)):
            payload = json.dumps(payload, separators=(",", ":"), allow_nan=False)
        client.publish(topic, payload, qos=1, retain=retain)

    def _state_payload(self, state: StateSnapshot) -> dict:
        data = asdict(state)
        duty = data.pop("duty")
        data["target_duty"] = None if duty is None else {"cpu": duty["cpu"], "sys": duty["sys"]}
        for source_id, sample in data["sources"].items():
            last = state.last_success.get(source_id)
            sample["last_success_at"] = None if last is None else time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._wall_anchor + last))
        return data

    def _announce(self, state: StateSnapshot) -> None:
        from .config import AppConfig
        config = AppConfig.model_validate(state.configuration, context={"normalized_duration": True})
        messages = build_discovery(config) if config.mqtt.discovery.enabled else {}
        for removed in self._discovery_topics - messages.keys():
            self._publish(removed, b"", retain=True)
        for topic, payload in messages.items():
            self._publish(topic, payload, retain=True)
        self._discovery_topics = set(messages)
        self._publish(f"{self.config.topic_prefix}/state", self._state_payload(state), retain=True)
        for source_id, sample in state.sources.items():
            self._publish(f"{self.config.topic_prefix}/source/{source_id}/availability",
                          "online" if sample.error is None else "offline", retain=True)

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
        self._publish(f"{self.config.topic_prefix}/result", asdict(result), retain=False)

    def _work(self) -> None:
        last_periodic = time.monotonic()
        while not self._stop.is_set():
            try:
                kind, value = self._queue.get(timeout=.5)
            except queue.Empty:
                if self._latest and time.monotonic() - last_periodic >= 5:
                    self._publish(f"{self.config.topic_prefix}/state", self._state_payload(self._latest), retain=True)
                    last_periodic = time.monotonic()
                continue
            if kind == "state":
                self._latest = value
                self._announce(value)
            elif kind == "connected":
                if self._latest:
                    self._announce(self._latest)
                    self._publish(f"{self.config.topic_prefix}/availability", "online", retain=True)
            elif kind == "message":
                topic, payload, retained = value
                if topic == "homeassistant/status":
                    if payload == b"online" and self._latest:
                        self._announce(self._latest)
                    continue
                try:
                    self._complete(*parse_command(topic, payload, retained))
                except CommandRejected as error:
                    self._publish(f"{self.config.topic_prefix}/result",
                                  {"request_id": None, "ok": False, "error": str(error),
                                   "revision": self._latest.revision if self._latest else 0}, retain=False)

    def stop(self) -> None:
        if not self.config.enabled:
            return
        self._stop.set()
        client = self._client
        if client:
            self._publish(f"{self.config.topic_prefix}/availability", "offline", retain=True)
            client.disconnect()
            client.loop_stop()
        if self._worker:
            self._worker.join(timeout=2)
        self._client = None
        self._password = None
