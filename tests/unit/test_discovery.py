from ha_nuc9_ec.discovery import build_discovery


def test_discovery_exposes_controls_measurements_and_stable_ids(example_config):
    config = example_config
    messages = build_discovery(config)
    payloads = messages
    device_id = config.device.id
    assert f"homeassistant/select/{device_id}/control_mode/config" in payloads
    assert f"homeassistant/sensor/{device_id}/cpu_rpm/config" in payloads
    source_token = "cpu_package".encode().hex()
    assert f"homeassistant/sensor/{device_id}/source_{source_token}_temperature/config" in payloads
    assert f"homeassistant/number/{device_id}/cpufan_input_{source_token}_boost_above_c/config" in payloads
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


def test_source_ids_are_collision_safe_and_templates_use_bracket_access(example_config):
    raw = example_config.model_dump(mode="python")
    raw["sources"]["cpu-a"] = raw["sources"]["cpu_package"]
    raw["sources"]["cpu_a"] = raw["sources"]["cpu_package"]
    config = type(example_config).model_validate(raw, context={"normalized_duration": True})
    messages = build_discovery(config)
    candidates = [value for value in messages.values() if value["name"] in {"cpu-a temperature", "cpu_a temperature"}]
    assert len(candidates) == 2
    assert candidates[0]["unique_id"] != candidates[1]["unique_id"]
    assert all("value_json.sources[" in item["value_template"] for item in candidates)
