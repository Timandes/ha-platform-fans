import pytest
from pydantic import ValidationError

from ha_nuc9_ec.cli import main
from ha_nuc9_ec.config import ConfigError, apply_changes, load_config


def test_example_is_valid_and_duration_units_are_normalized(example_config):
    assert example_config.sources["cpu_package"].poll_interval == 0.1
    assert example_config.control.decrease_delay == 10.0


@pytest.mark.parametrize("text", ["1", "1m", "1s trailing", " 1s", "1.0msjunk"])
def test_duration_requires_complete_explicit_ms_or_s(tmp_path, example_path, text):
    raw = example_path.read_text().replace("100ms", text, 1)
    candidate = tmp_path / "bad-duration.yaml"
    candidate.write_text(raw)
    with pytest.raises(ConfigError, match="poll_interval"):
        load_config(candidate)


def test_duplicate_yaml_key_is_rejected(tmp_path, example_path):
    candidate = tmp_path / "duplicate.yaml"
    candidate.write_text(example_path.read_text().replace("version: 1", "version: 1\nversion: 1"))
    with pytest.raises(ConfigError, match="duplicate key.*version"):
        load_config(candidate)


def test_boolean_version_is_rejected(tmp_path, example_path):
    candidate = tmp_path / "boolean-version.yaml"
    candidate.write_text(example_path.read_text().replace("version: 1", "version: true", 1))
    with pytest.raises(ConfigError, match="version"):
        load_config(candidate)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("duty_percent: 40", "duty_percent: true", "duty_percent"),
        ("minimum_temperature_c: 47", "minimum_temperature_c: .nan", "finite"),
        ("topic_prefix: nuc9/nas11", "topic_prefix: nuc9/nas11\n  password: secret", "password"),
        ("type: x86_pkg_temp", "type: x86_pkg_temp, name: forbidden", "selector"),
    ],
)
def test_strict_fields_and_numbers_are_rejected(tmp_path, example_path, old, new, message):
    candidate = tmp_path / "invalid.yaml"
    candidate.write_text(example_path.read_text().replace(old, new, 1))
    with pytest.raises(ConfigError, match=message):
        load_config(candidate)


def test_unknown_and_disabled_sources_are_rejected(tmp_path, example_path):
    for source in ("missing", "disk"):
        candidate = tmp_path / f"{source}.yaml"
        candidate.write_text(example_path.read_text().replace("source: pch", f"source: {source}", 1))
        with pytest.raises(ConfigError, match=source):
            load_config(candidate)


def test_preset_allows_cpu_without_curve_but_requires_non_cpu_curve(tmp_path, example_path):
    valid = tmp_path / "valid.yaml"
    text = example_path.read_text().replace("mode: custom", "mode: quiet", 1)
    text = text.replace("          custom:\n            minimum_temperature_c: 47\n            minimum_duty_percent: 40\n            duty_increment_percent_per_c: 2\n", "", 1)
    valid.write_text(text)
    assert load_config(valid).fans.cpufan.override.mode == "quiet"

    invalid = tmp_path / "invalid.yaml"
    invalid.write_text(text.replace("mode: custom", "mode: cool", 1).replace(
        "          custom:\n            minimum_temperature_c: 50\n            minimum_duty_percent: 40\n            duty_increment_percent_per_c: 3\n", "", 1
    ))
    with pytest.raises(ConfigError, match="non-CPU.*custom"):
        load_config(invalid)


def test_fan_off_is_explicitly_unsupported(tmp_path, example_path):
    candidate = tmp_path / "fan-off.yaml"
    candidate.write_text(example_path.read_text().replace("enabled: false, temperature_c: 0", "enabled: true, temperature_c: 30", 1))
    with pytest.raises(ConfigError, match="fan stop.*not verified"):
        load_config(candidate)


def test_apply_changes_is_allowlisted_atomic_and_does_not_mutate(example_config):
    before = example_config.model_dump()
    changed = apply_changes(example_config, {
        "control.mode": "override",
        "fans.cpufan.override.inputs.0.custom.minimum_temperature_c": 48.5,
        "fans.sysfan.override.fixed.duty_percent": 60,
    })
    assert changed.control.mode == "override"
    assert changed.fans.cpufan.override.inputs[0].custom.minimum_temperature_c == 48.5
    assert example_config.model_dump() == before

    with pytest.raises(ConfigError, match="not remotely mutable"):
        apply_changes(example_config, {"mqtt.password_file": "/tmp/stolen"})
    with pytest.raises((ConfigError, ValidationError)):
        apply_changes(example_config, {"fans.cpufan.override.fixed.duty_percent": 101})
    assert example_config.model_dump() == before

    with pytest.raises(ConfigError):
        apply_changes(example_config, {
            "control.mode": "override",
            "fans.sysfan.override.fixed.duty_percent": 101,
        })
    assert example_config.model_dump() == before


def test_boost_must_be_above_curve_minimum(example_config):
    with pytest.raises(ConfigError, match="boost_above_c"):
        apply_changes(example_config, {
            "fans.cpufan.override.inputs.0.boost_above_c": 47,
        })


def test_cli_validates_and_evaluates_without_hardware(tmp_path, example_path, capsys):
    assert main(["validate", str(example_path)]) == 0
    config = tmp_path / "override.yaml"
    config.write_text(example_path.read_text().replace("mode: bios", "mode: override", 1))
    samples = tmp_path / "samples.json"
    samples.write_text('{"cpu_package":{"celsius":55,"read_at":10},"pch":{"celsius":60,"read_at":10}}')
    assert main(["evaluate", str(config), str(samples), "--now", "10.1", "--bounds", "40", "80"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == '{"cpu":56,"sys":70}'


def test_cli_validation_error_does_not_echo_secret(tmp_path, example_path, capsys):
    candidate = tmp_path / "secret.yaml"
    secret = "do-not-print-this-password"
    candidate.write_text(example_path.read_text().replace("password_file: /run/secrets/mqtt_password", f"password_file: /run/secrets/mqtt_password\n  password: {secret}"))
    assert main(["validate", str(candidate)]) == 2
    error = capsys.readouterr().err
    assert "mqtt.password" in error
    assert secret not in error


def test_cli_yaml_syntax_error_does_not_echo_source_line(tmp_path, example_path, capsys):
    candidate = tmp_path / "syntax-secret.yaml"
    secret = "do-not-print-this-syntax-secret"
    candidate.write_text(example_path.read_text().replace(
        "password_file: /run/secrets/mqtt_password",
        f"password_file: [{secret}",
    ))
    assert main(["validate", str(candidate)]) == 2
    error = capsys.readouterr().err
    assert "YAML" in error
    assert secret not in error


@pytest.mark.parametrize('field', [
    'duty_increment_percent_per_c', 'minimum_temperature_c', 'boost_above_c', 'temperature_c',
])
@pytest.mark.parametrize('value', [True, False])
def test_float_fields_reject_yaml_booleans(tmp_path, example_path, field, value):
    import yaml
    data = yaml.safe_load(example_path.read_text())
    override = data['fans']['cpufan']['override']
    # Keep true/false-as-1/0 above the curve minimum so threshold ordering
    # cannot accidentally hide a primitive numeric-type validation defect.
    override['inputs'][0]['custom']['minimum_temperature_c'] = -10
    if field == 'boost_above_c':
        target = override['inputs'][0]
    elif field == 'temperature_c':
        target = override['fan_off']
    else:
        target = override['inputs'][0]['custom']
    target[field] = value
    path = tmp_path / 'boolean-float.yaml'
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match=field):
        load_config(path)


def test_strict_float_fields_preserve_yaml_integer_numbers(example_config):
    override = example_config.fans.cpufan.override
    assert override.inputs[0].custom.minimum_temperature_c == 47.0
    assert override.inputs[0].custom.duty_increment_percent_per_c == 2.0
    assert override.inputs[0].boost_above_c == 75.0
    assert override.fan_off.temperature_c == 0.0
    changed = apply_changes(example_config, {
        'fans.cpufan.override.inputs.0.custom.minimum_temperature_c': 46,
        'fans.cpufan.override.inputs.0.custom.duty_increment_percent_per_c': 3,
        'fans.cpufan.override.inputs.0.boost_above_c': 76,
    })
    assert changed.fans.cpufan.override.inputs[0].custom.minimum_temperature_c == 46.0
    assert changed.fans.cpufan.override.inputs[0].custom.duty_increment_percent_per_c == 3.0
    assert changed.fans.cpufan.override.inputs[0].boost_above_c == 76.0


@pytest.mark.parametrize("mode", ["quiet", "balanced", "cool"])
def test_preset_boost_validation_uses_current_curve_start(example_config, mode):
    raw = example_config.model_dump()
    raw["fans"]["cpufan"]["override"]["mode"] = mode
    raw["fans"]["cpufan"]["override"]["inputs"] = [
        {"source": "cpu_package", "boost_above_c": 65}
    ]
    cfg = type(example_config).model_validate(raw, context={"normalized_duration": True})
    assert cfg.fans.cpufan.override.inputs[0].boost_above_c == 65
    with pytest.raises(ConfigError, match="boost_above_c"):
        apply_changes(cfg, {"fans.cpufan.override.inputs.0.boost_above_c": 60})
    # An explicit input curve supplies its own validation threshold.
    raw["fans"]["cpufan"]["override"]["inputs"][0]["custom"] = {
        "minimum_temperature_c": 70,
        "minimum_duty_percent": 40,
        "duty_increment_percent_per_c": 2,
    }
    with pytest.raises(ValidationError, match="boost_above_c"):
        type(example_config).model_validate(raw, context={"normalized_duration": True})
