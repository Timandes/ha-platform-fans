"""Home Assistant MQTT discovery descriptions."""
from __future__ import annotations

import re

from .config import AppConfig


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")


def build_discovery(config: AppConfig) -> dict[str, dict]:
    device_id = config.device.id
    prefix = config.mqtt.topic_prefix
    discovery = config.mqtt.discovery.prefix
    device = {"identifiers": [device_id], "name": "NUC9 Fan Controller", "manufacturer": "Intel", "model": "NUC9i7QNB"}
    result: dict[str, dict] = {}

    def add(component: str, entity_id: str, name: str, **fields):
        entity_id = _slug(entity_id)
        payload = {
            "name": name, "unique_id": f"{device_id}_{entity_id}", "device": device,
            "state_topic": f"{prefix}/state", **fields,
        }
        if "availability" not in fields:
            payload.update(availability_topic=f"{prefix}/availability",
                           payload_available="online", payload_not_available="offline")
        result[f"{discovery}/{component}/{device_id}/{entity_id}/config"] = payload

    add("select", "control_mode", "Control mode", command_topic=f"{prefix}/command/control_mode",
        options=["bios", "override"], value_template="{{ value_json.requested_mode }}")
    for fan in ("cpufan", "sysfan"):
        title = "CPU fan" if fan == "cpufan" else "SYS fans"
        add("select", f"{fan}_override_mode", f"{title} override mode",
            command_topic=f"{prefix}/command/{fan}_override_mode",
            options=["fixed", "custom", "cool", "balanced", "quiet"],
            value_template=f"{{{{ value_json.configuration.fans.{fan}.override.mode }}}}")
        add("number", f"{fan}_fixed_duty_percent", f"{title} fixed duty",
            command_topic=f"{prefix}/command/{fan}_fixed_duty_percent", min=0, max=100, step=1,
            unit_of_measurement="%", value_template=f"{{{{ value_json.configuration.fans.{fan}.override.fixed.duty_percent }}}}")
        add("sensor", f"{fan}_target_duty_percent", f"{title} command target",
            unit_of_measurement="%", value_template=f"{{{{ value_json.target_duty.{fan.removesuffix('fan')} | default(none) }}}}")
        inputs = getattr(config.fans, fan).override.inputs
        for item in inputs:
            source_slug = _slug(item.source)
            # Topics retain the current index needed by apply_changes, while identity
            # follows the logical source so reordering inputs does not recreate entities.
            index = next(i for i, value in enumerate(inputs) if value.source == item.source)
            base = f"{fan}_input_{source_slug}"
            path_base = f"configuration.fans.{fan}.override.inputs.{index}"
            command_base = f"{fan}_input_{index}"
            if item.custom is not None:
                for suffix, label, unit, low, high, step in (
                    ("minimum_temperature_c", "minimum temperature", "°C", -100, 200, .1),
                    ("minimum_duty_percent", "minimum duty", "%", 0, 100, 1),
                    ("duty_increment_percent_per_c", "duty increment", "%/°C", .01, 100, .01),
                ):
                    add("number", f"{base}_{suffix}", f"{title} {item.source} {label}",
                        command_topic=f"{prefix}/command/{command_base}_{suffix}", min=low, max=high, step=step,
                        unit_of_measurement=unit, value_template=f"{{{{ value_json.{path_base}.custom.{suffix} }}}}")
            add("number", f"{base}_boost_above_c", f"{title} {item.source} boost temperature",
                command_topic=f"{prefix}/command/{command_base}_boost_above_c", min=-100, max=200, step=.1,
                unit_of_measurement="°C", value_template=f"{{{{ value_json.{path_base}.boost_above_c }}}}")

    for index, label in enumerate(("CPU", "SYS1", "SYS2")):
        add("sensor", f"{label.lower()}_rpm", f"{label} speed", unit_of_measurement="rpm",
            value_template=f"{{{{ value_json.rpm[{index}] | default(none) }}}}")
    for source_id in config.sources:
        if not config.sources[source_id].enabled:
            continue
        entity = f"source_{_slug(source_id)}"
        add("sensor", f"{entity}_temperature", f"{source_id} temperature", unit_of_measurement="°C",
            value_template=f"{{{{ value_json.sources.{source_id}.celsius | default(none) }}}}",
            availability=[{"topic": f"{prefix}/availability"}, {"topic": f"{prefix}/source/{source_id}/availability"}],
            availability_mode="all")
        add("sensor", f"{entity}_last_success", f"{source_id} last successful sample",
            device_class="timestamp", value_template=f"{{{{ value_json.sources.{source_id}.last_success_at | default(none) }}}}")
    add("sensor", "fault", "Controller fault", value_template="{{ value_json.fault | default('') }}")
    return result
