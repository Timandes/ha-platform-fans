import pytest

from ha_nuc9_ec.config import AppConfig, apply_changes
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


@pytest.mark.parametrize("mode,column", [("quiet", 0), ("balanced", 1), ("cool", 2)])
@pytest.mark.parametrize("temp,expected", [
    (40, (40, 40, 40)),
    (60, (40, 40, 40)),
    (65, (50, 52, 55)),
    (70, (60, 64, 70)),
    (75, (70, 76, 85)),
    (79, (78, 86, 97)),
    (80, (80, 88, 100)),
    (84, (88, 98, 100)),
    (85, (90, 100, 100)),
    (89, (98, 100, 100)),
    (90, (100, 100, 100)),
    (100, (100, 100, 100)),
])
def test_cpu_presets_ramp_to_full_speed_without_boost(example_config, mode, column, temp, expected):
    raw = example_config.model_dump()
    raw["control"]["mode"] = "override"
    raw["fans"]["cpufan"]["minimum_running_duty_percent"] = 30
    raw["fans"]["cpufan"]["override"]["mode"] = mode
    raw["fans"]["cpufan"]["override"]["inputs"] = [{"source": "cpu_package"}]
    raw["fans"]["sysfan"]["override"]["mode"] = "fixed"
    cfg = AppConfig.model_validate(raw, context={"normalized_duration": True})
    samples = {"cpu_package": Sample("cpu_package", temp, 2, None)}
    assert calculate(cfg, samples, now=2, bounds=(30, 100)).cpu == expected[column]
    # Driver limits still win over every preset, including its 40% baseline.
    assert calculate(cfg, samples, now=2, bounds=(50, 80)).cpu == min(80, max(50, expected[column]))


@pytest.mark.parametrize("mode", ["quiet", "balanced", "cool"])
def test_explicit_cpu_curve_overrides_preset(example_config, mode):
    cfg = apply_changes(example_config, {
        "control.mode": "override",
        "fans.cpufan.override.mode": mode,
        "fans.cpufan.override.inputs.0.boost_above_c": None,
        "fans.sysfan.override.mode": "fixed",
    })
    # Explicit example curve remains 47°C / 40% / 2, independent of the preset.
    samples = {"cpu_package": Sample("cpu_package", 60, 2, None)}
    assert calculate(cfg, samples, now=2, bounds=(30, 100)).cpu == 66


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


@pytest.mark.parametrize("now", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_policy_time_is_unavailable(example_config, now):
    cfg = apply_changes(example_config, {"control.mode": "override"})
    with pytest.raises(SourceUnavailable, match="now"):
        calculate(cfg, {
            "cpu_package": Sample("cpu_package", 50, 10, None),
            "pch": Sample("pch", 50, 10, None),
        }, now=now, bounds=(40, 80))


@pytest.mark.parametrize("bounds", [(40.0, 80), (40, 80.0)])
def test_bounds_require_strict_integers(example_config, bounds):
    cfg = apply_changes(example_config, {
        "control.mode": "override",
        "fans.cpufan.override.mode": "fixed",
        "fans.sysfan.override.mode": "fixed",
    })
    with pytest.raises(ValueError, match="integer"):
        calculate(cfg, {}, now=10, bounds=bounds)


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


@pytest.mark.parametrize("bounds", [(40, 80), (0, 100)])
@pytest.mark.parametrize("temp, threshold, slope, boost, expected", [
    (60, 47, 1e308, None, 100),  # product overflow
    (60, 47, 1e308, 55, 100),   # boost must survive overflow too
    (1e308, -1e308, 1.0, None, 100),  # subtraction overflow
    (1e308, -1e308, 1e-308, None, 42), # huge delta, bounded product
    (-1e308, 1e308, 1e308, None, 40),  # negative overflowing delta
    (47.1, 47, 2.0, None, 41),
    (67, 47, 2.0, None, 80),
    (67.0001, 47, 2.0, None, 81),
])
def test_validated_finite_curves_saturate_before_integer_conversion(example_config, bounds, temp, threshold, slope, boost, expected):
    cfg = apply_changes(example_config, {
        "control.mode": "override",
        "fans.cpufan.override.inputs.0.custom.minimum_temperature_c": threshold,
        "fans.cpufan.override.inputs.0.custom.duty_increment_percent_per_c": slope,
        "fans.cpufan.override.inputs.0.boost_above_c": boost,
        "fans.sysfan.override.mode": "fixed",
    })
    result = calculate(cfg, {"cpu_package": Sample("cpu_package", temp, 1, None)}, now=1, bounds=bounds)
    assert result == DutyPair(min(bounds[1], expected), 40)


@pytest.mark.parametrize("missing_source", ["cpu_package", "pch"])
def test_boost_and_saturation_still_validate_every_required_source(example_config, missing_source):
    cfg = apply_changes(example_config, {
        "control.mode": "override",
        "fans.cpufan.override.mode": "fixed",
        "fans.sysfan.override.inputs.0.custom.duty_increment_percent_per_c": 1e308,
        "fans.sysfan.override.inputs.0.boost_above_c": 55,
        "fans.sysfan.override.inputs.1.custom.duty_increment_percent_per_c": 1e308,
        "fans.sysfan.override.inputs.1.boost_above_c": 55,
    })
    samples = {key: Sample(key, 60, 1, None) for key in ("cpu_package", "pch") if key != missing_source}
    with pytest.raises(SourceUnavailable, match=missing_source):
        calculate(cfg, samples, now=1, bounds=(40, 80))


@pytest.mark.parametrize("extreme_input", [0, 1])
def test_saturation_preserves_per_source_maximum(example_config, extreme_input):
    cfg = apply_changes(example_config, {
        "control.mode": "override",
        f"fans.sysfan.override.inputs.{extreme_input}.custom.duty_increment_percent_per_c": 1e308,
    })
    samples = {key: Sample(key, 60, 1, None) for key in ("cpu_package", "pch")}
    assert calculate(cfg, samples, now=1, bounds=(40, 80)) == DutyPair(66, 80)
