import pytest

from ha_nuc9_ec.controller import Controller
from ha_nuc9_ec.hardware.mock import MockBackend
from ha_nuc9_ec.mqtt import CommandRejected, MQTTAdapter, parse_command


def test_retained_commands_are_rejected():
    with pytest.raises(CommandRejected, match="retained"):
        parse_command("nuc9/nas11/set", b'{"request_id":"1","changes":{"control.mode":"override"}}', True)


def test_set_command_is_strict_and_bounded():
    request_id, changes = parse_command(
        "nuc9/nas11/set", b'{"request_id":"1","changes":{"control.mode":"override"}}', False)
    assert request_id == "1" and changes == {"control.mode": "override"}
    with pytest.raises(CommandRejected, match="duplicate"):
        parse_command("nuc9/nas11/set", b'{"request_id":"1","request_id":"2","changes":{}}', False)
    with pytest.raises(CommandRejected, match="16 KiB"):
        parse_command("nuc9/nas11/set", b"x" * (16 * 1024 + 1), False)


@pytest.mark.parametrize(("entity", "payload", "path", "value"), [
    ("control_mode", b"override", "control.mode", "override"),
    ("cpufan_override_mode", b"fixed", "fans.cpufan.override.mode", "fixed"),
    ("sysfan_fixed_duty_percent", b"55", "fans.sysfan.override.fixed.duty_percent", 55),
    ("cpufan_input_source_6370755f7061636b616765_boost_above_c", b"75.5", "fans.cpufan.override.inputs.source:6370755f7061636b616765.boost_above_c", 75.5),
])
def test_scalar_entities_bind_to_changes(entity, payload, path, value):
    request_id, changes = parse_command(f"nuc9/nas11/command/{entity}", payload, False)
    assert request_id.startswith("ha-")
    assert changes == {path: value}


def test_unknown_entity_and_invalid_scalar_are_rejected():
    with pytest.raises(CommandRejected, match="unknown"):
        parse_command("nuc9/nas11/command/nope", b"1", False)
    with pytest.raises(CommandRejected, match="invalid"):
        parse_command("nuc9/nas11/command/cpufan_fixed_duty_percent", b"true", False)


def test_scalar_operations_receive_distinct_request_ids():
    first, _ = parse_command("nuc9/nas11/command/control_mode", b"override", False)
    second, _ = parse_command("nuc9/nas11/command/control_mode", b"override", False)
    assert first != second


@pytest.mark.parametrize("path", [
    "mqtt.username", "mqtt.password_file", "device.id", "sources.cpu_package.enabled",
    "fans.cpufan.override.inputs.0.source",
])
def test_json_command_rejects_paths_outside_explicit_public_allowlist(path):
    body = ('{"request_id":"1","changes":{"%s":true}}' % path).encode()
    with pytest.raises(CommandRejected, match="not allowed|existing"):
        parse_command("nuc9/nas11/set", body, False)


def test_source_bound_scalar_command_does_not_expose_mutable_index():
    _, changes = parse_command(
        "nuc9/nas11/command/sysfan_input_source_706368_boost_above_c", b"75", False)
    assert changes == {"fans.sysfan.override.inputs.source:706368.boost_above_c": 75.0}


def test_external_state_payload_omits_all_mqtt_connection_configuration(example_config):
    adapter = MQTTAdapter(example_config.mqtt, lambda *_: None)
    payload = adapter._state_payload(Controller(example_config, MockBackend(), lambda: 1).snapshot())
    encoded = __import__('json').dumps(payload)
    for secret in (example_config.mqtt.broker, example_config.mqtt.username,
                   example_config.mqtt.password_file, example_config.mqtt.client_id):
        assert secret not in encoded
