import json
from pathlib import Path

import pytest

from ha_nuc9_ec.config import SourceConfig
from ha_nuc9_ec.sources.base import SourceReadError
from ha_nuc9_ec.sources.smart import SmartctlReader, StandbySkip
from ha_nuc9_ec.sources.sysfs import SysfsReader, resolve_sysfs
from ha_nuc9_ec.cli import main


def source(provider, selector, *, kind="sys", skip_standby=False):
    return SourceConfig.model_validate({
        "kind": kind,
        "provider": provider,
        "selector": selector,
        "poll_interval": "100ms" if provider != "smartctl" else "1s",
        "stale_after": "1s" if provider != "smartctl" else "7s",
        "skip_standby": skip_standby,
    })


def make_hwmon(root: Path, number: str, name: str, value: str, *, label=None, pci=None):
    directory = root / "class" / "hwmon" / number
    directory.mkdir(parents=True)
    (directory / "name").write_text(name)
    (directory / "temp1_input").write_text(value)
    if label is not None:
        (directory / "temp1_label").write_text(label)
    if pci is not None:
        device = root / "devices" / "pci0000:00" / pci
        device.mkdir(parents=True)
        (directory / "device").symlink_to(device, target_is_directory=True)
    return directory


def test_ambiguous_sensor_is_rejected(tmp_path):
    make_hwmon(tmp_path, "hwmon2", "pch_cannonlake", "42000")
    make_hwmon(tmp_path, "hwmon9", "pch_cannonlake", "43000")
    pch = source("hwmon", {"name": "pch_cannonlake", "channel": "temp1"})
    with pytest.raises(SourceReadError, match="ambiguous"):
        resolve_sysfs(pch, tmp_path)


def test_hwmon_stable_identity_survives_number_change(tmp_path):
    make_hwmon(tmp_path, "hwmon2", "coretemp", "42000", label="Package id 0", pci="0000:00:04.0")
    wanted = make_hwmon(tmp_path, "hwmon9", "coretemp", "43000", label="Package id 0", pci="0000:00:18.0")
    config = source("hwmon", {"name": "coretemp", "channel": "temp1", "label": "Package id 0", "pci_address": "0000:00:18.0"})
    assert resolve_sysfs(config, tmp_path) == wanted / "temp1_input"
    wanted.rename(wanted.with_name("hwmon1"))
    assert resolve_sysfs(config, tmp_path).parent.name == "hwmon1"


def test_thermal_zone_is_read_once_and_converted_to_celsius(tmp_path):
    zone = tmp_path / "class" / "thermal" / "thermal_zone7"
    zone.mkdir(parents=True)
    (zone / "type").write_text("x86_pkg_temp\n")
    (zone / "temp").write_text("42500\n")
    config = source("thermal_zone", {"type": "x86_pkg_temp"}, kind="cpu")
    assert SysfsReader("cpu", config, tmp_path).read() == 42.5


@pytest.mark.parametrize("value", ["", "hot", "nan", "inf", "42000 extra"])
def test_sysfs_rejects_non_numeric_temperature(tmp_path, value):
    zone = tmp_path / "class" / "thermal" / "thermal_zone0"
    zone.mkdir(parents=True)
    (zone / "type").write_text("x86_pkg_temp")
    (zone / "temp").write_text(value)
    config = source("thermal_zone", {"type": "x86_pkg_temp"}, kind="cpu")
    with pytest.raises(SourceReadError):
        SysfsReader("cpu", config, tmp_path).read()


def executable(tmp_path, body):
    path = tmp_path / "smartctl"
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


def fixture_text(name):
    return (Path(__file__).parents[1] / "fixtures" / "smartctl" / name).read_text().strip()


def test_smartctl_uses_json_device_type_and_accepts_health_bits(tmp_path):
    args_file = tmp_path / "args"
    program = executable(tmp_path, f"printf '%s\\n' \"$@\" > {args_file}\nprintf '%s' '{fixture_text('temperature-with-health-bit.json')}'\nexit 8\n")
    config = source("smartctl", {"device": "/dev/disk/by-id/disk-a", "device_type": "sat"}, kind="smart", skip_standby=True)
    assert SmartctlReader("disk", config, executable=program).read() == 37.0
    assert args_file.read_text().splitlines() == ["--json", "--device", "sat", "--nocheck", "standby,3,5", "/dev/disk/by-id/disk-a"]


def test_smartctl_status_3_is_a_distinct_standby_skip(tmp_path):
    program = executable(tmp_path, f"printf '%s' '{fixture_text('standby.json')}'\nexit 3\n")
    config = source("smartctl", {"device": "/dev/disk/by-id/disk-a", "device_type": "sat"}, kind="smart", skip_standby=True)
    with pytest.raises(StandbySkip, match="standby"):
        SmartctlReader("disk", config, executable=program).read()


@pytest.mark.parametrize(("status", "fixture", "message"), [(2, "error.json", "status 2"), (5, "unsupported.json", "not supported")])
def test_smartctl_special_failures_are_not_standby(tmp_path, status, fixture, message):
    program = executable(tmp_path, f"printf '%s' '{fixture_text(fixture)}'\nexit {status}\n")
    config = source("smartctl", {"device": "/dev/disk/by-id/disk-a", "device_type": "sat"}, kind="smart", skip_standby=True)
    with pytest.raises(SourceReadError, match=message) as raised:
        SmartctlReader("disk", config, executable=program).read()
    assert not isinstance(raised.value, StandbySkip)


def test_smartctl_start_failure_uses_source_error(tmp_path):
    config = source("smartctl", {"device": "/dev/disk/by-id/disk-a"}, kind="smart")
    with pytest.raises(SourceReadError, match="start smartctl"):
        SmartctlReader("disk", config, executable=tmp_path / "missing").read()


def test_smartctl_timeout_kills_and_waits_for_process(tmp_path):
    program = executable(tmp_path, "exec sleep 10\n")
    config = source("smartctl", {"device": "/dev/disk/by-id/disk-a"}, kind="smart")
    with pytest.raises(SourceReadError, match="timed out"):
        SmartctlReader("disk", config, executable=program, timeout=0.02).read()


def test_skip_standby_requires_explicit_device_type():
    with pytest.raises(Exception, match="device_type"):
        source("smartctl", {"device": "/dev/disk/by-id/disk-a"}, kind="smart", skip_standby=True)


def test_discover_lists_stable_selectors_and_cache_hint(tmp_path, capsys):
    make_hwmon(tmp_path, "hwmon8", "pch_cannonlake", "42000", label="temp1", pci="0000:00:1f.0")
    zone = tmp_path / "class" / "thermal" / "thermal_zone4"
    zone.mkdir(parents=True)
    (zone / "type").write_text("x86_pkg_temp")
    (zone / "temp").write_text("41000")
    assert main(["discover", "--sys-root", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "type: x86_pkg_temp" in output
    assert "name: pch_cannonlake" in output
    assert "pci_address: '0000:00:1f.0'" in output
    assert "cached at startup" in output


@pytest.mark.parametrize("payload", [{}, {"temperature": {"current": True}}, {"temperature": {"current": float("nan")}}])
def test_smartctl_rejects_missing_boolean_and_nonfinite_temperature(tmp_path, payload):
    program = executable(tmp_path, "printf '%s' '" + json.dumps(payload) + "'\n")
    config = source("smartctl", {"device": "/dev/disk/by-id/disk-a", "device_type": "sat"}, kind="smart")
    with pytest.raises(SourceReadError, match="temperature"):
        SmartctlReader("disk", config, executable=program).read()
