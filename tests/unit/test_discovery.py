from ha_nuc9_ec.discovery import build_discovery


def test_discovery_exposes_controls_measurements_and_stable_ids(example_config):
    config = example_config
    messages = build_discovery(config)
    payloads = messages
    device_id = config.device.id
    assert f"homeassistant/select/{device_id}/control_mode/config" in payloads
    assert f"homeassistant/sensor/{device_id}/cpu_rpm/config" in payloads
    assert f"homeassistant/sensor/{device_id}/source_cpu_package_temperature/config" in payloads
    assert f"homeassistant/number/{device_id}/cpufan_input_cpu_package_boost_above_c/config" in payloads
    ids = [payload["unique_id"] for payload in payloads.values()]
    assert len(ids) == len(set(ids))
    assert all(value.startswith(f"{device_id}_") for value in ids)
    assert all(payload["device"]["identifiers"] == [device_id] for payload in payloads.values())


def test_input_unique_id_uses_source_identity_not_dynamic_index(example_config):
    config = example_config
    first = build_discovery(config)
    raw = config.model_dump(mode="python")
    raw["fans"]["sysfan"]["override"]["inputs"].reverse()
    moved = type(config).model_validate(raw, context={"normalized_duration": True})
    second = build_discovery(moved)
    first_ids = {value["unique_id"] for value in first.values()}
    second_ids = {value["unique_id"] for value in second.values()}
    assert first_ids == second_ids
