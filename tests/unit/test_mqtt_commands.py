import pytest

from ha_nuc9_ec.mqtt import CommandRejected, parse_command


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
    ("cpufan_input_0_boost_above_c", b"75.5", "fans.cpufan.override.inputs.0.boost_above_c", 75.5),
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
