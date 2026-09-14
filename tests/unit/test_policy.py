import pytest

from ha_nuc9_ec.config import apply_changes, load_config
from ha_nuc9_ec.model import DutyPair, Sample
from ha_nuc9_ec.policy import DownshiftGate, SourceUnavailable, calculate, curve_duty


def test_multiple_sources_use_maximum_required_duty(example_config):
    cfg = apply_changes(example_config, {"control.mode": "override"})
    result = calculate(cfg, {
        "cpu_package": Sample("cpu_package", 55.0, 10.0, None),
        "pch": Sample("pch", 60.0, 10.0, None),
    }, now=10.1, bounds=(40, 80))
    assert result == DutyPair(cpu=56, sys=70)


def test_curve_rounds_up_and_result_clips_to_upper_bound(example_config):
    assert curve_duty(47.1, 47, 40, 2) == 41
    cfg = apply_changes(example_config, {"control.mode": "override"})
    result = calculate(cfg, {
        "cpu_package": Sample("cpu_package", 99, 1, None),
        "pch": Sample("pch", 99, 1, None),
    }, now=1, bounds=(0, 75))
    assert result == DutyPair(75, 75)


@pytest.mark.parametrize("mode", ["quiet", "balanced", "cool"])
def test_bios_style_presets_use_firmware_curves_and_device_floor(example_path, tmp_path, mode):
    candidate = tmp_path / f"{mode}.yaml"
    text = example_path.read_text().replace("mode: bios", "mode: override", 1).replace("mode: custom", f"mode: {mode}", 1)
    text = text.replace("          custom:\n            minimum_temperature_c: 47\n            minimum_duty_percent: 40\n            duty_increment_percent_per_c: 2\n", "", 1)
    text = text.replace("          boost_above_c: 75\n", "", 1)
    candidate.write_text(text)
    cfg = load_config(candidate)
    result = calculate(cfg, {
        "cpu_package": Sample("cpu_package", 80, 2, None),
        "pch": Sample("pch", 50, 2, None),
    }, now=2, bounds=(40, 80))
    expected = {"quiet": 43, "balanced": 47, "cool": 51}[mode]
    assert result.cpu == expected


def test_fixed_mode_does_not_require_samples_or_activate_boost(example_config):
    cfg = apply_changes(example_config, {
        "control.mode": "override",
        "fans.cpufan.override.mode": "fixed",
        "fans.cpufan.override.fixed.duty_percent": 44,
        "fans.sysfan.override.mode": "fixed",
        "fans.sysfan.override.fixed.duty_percent": 45,
    })
    assert calculate(cfg, {}, now=999, bounds=(40, 80)) == DutyPair(44, 45)


def test_missing_error_and_stale_samples_are_unavailable(example_config):
    cfg = apply_changes(example_config, {"control.mode": "override"})
    valid_pch = Sample("pch", 50, 10, None)
    for sample in (
        None,
        Sample("cpu_package", None, 10, "read failed"),
        Sample("cpu_package", 50, 9, None),
    ):
        samples = {"pch": valid_pch}
        if sample is not None:
            samples["cpu_package"] = sample
        with pytest.raises(SourceUnavailable, match="cpu_package"):
            calculate(cfg, samples, now=10, bounds=(40, 80))


def test_same_temperature_with_recent_read_time_is_valid(example_config):
    cfg = apply_changes(example_config, {"control.mode": "override"})
    result = calculate(cfg, {
        "cpu_package": Sample("cpu_package", 50, 10, None),
        "pch": Sample("pch", 50, 10, None),
    }, now=10.2, bounds=(40, 80))
    assert result == DutyPair(46, 40)


def test_boost_uses_upper_bound(example_config):
    cfg = apply_changes(example_config, {
        "control.mode": "override",
        "fans.cpufan.override.inputs.0.boost_above_c": 60,
    })
    result = calculate(cfg, {
        "cpu_package": Sample("cpu_package", 60, 3, None),
        "pch": Sample("pch", 50, 3, None),
    }, now=3, bounds=(40, 80))
    assert result.cpu == 80


def test_sustained_lower_target_uses_latest_value():
    gate = DownshiftGate(delay_s=10)
    assert gate.apply(DutyPair(70, 70), 0, immediate=True) == DutyPair(70, 70)
    assert gate.apply(DutyPair(50, 50), 1) == DutyPair(70, 70)
    assert gate.apply(DutyPair(60, 60), 10) == DutyPair(70, 70)
    assert gate.apply(DutyPair(55, 55), 11) == DutyPair(55, 55)


def test_downshift_is_independent_and_upshift_is_immediate():
    gate = DownshiftGate(delay_s=10)
    gate.apply(DutyPair(70, 70), 0, immediate=True)
    assert gate.apply(DutyPair(50, 80), 1) == DutyPair(70, 80)
    assert gate.apply(DutyPair(75, 60), 2) == DutyPair(75, 80)
    assert gate.apply(DutyPair(75, 55), 12) == DutyPair(75, 55)
