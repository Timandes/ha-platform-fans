from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError, ValidationInfo, field_validator, model_validator


class ConfigError(ValueError):
    pass


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


Percent = Annotated[StrictInt, Field(ge=0, le=100)]
PositiveFinite = Annotated[float, Field(gt=0, allow_inf_nan=False)]
Finite = Annotated[float, Field(allow_inf_nan=False)]
_DURATION = re.compile(r"(?:0|[1-9]\d*)(?:\.\d+)?(ms|s)\Z")


def _duration(value: object, info: ValidationInfo) -> float:
    if info.context and info.context.get("normalized_duration") and isinstance(value, float):
        return value
    if not isinstance(value, str) or (match := _DURATION.fullmatch(value)) is None:
        raise ValueError("duration must be a complete number with ms or s unit")
    amount = float(value[: -len(match.group(1))])
    if amount <= 0 or not math.isfinite(amount):
        raise ValueError("duration must be finite and greater than zero")
    return amount / 1000 if match.group(1) == "ms" else amount


class DeviceConfig(_StrictModel):
    id: str = Field(min_length=1)


class ControlConfig(_StrictModel):
    mode: Literal["bios", "override"]
    decrease_delay: float

    _parse_decrease_delay = field_validator("decrease_delay", mode="before", json_schema_input_type=str)(_duration)


class ThermalZoneSelector(_StrictModel):
    type: str = Field(min_length=1)


class HwmonSelector(_StrictModel):
    name: str = Field(min_length=1)
    channel: str = Field(pattern=r"^temp[1-9]\d*$")
    label: str | None = None
    pci_address: str | None = None


class SmartctlSelector(_StrictModel):
    device: str = Field(pattern=r"^/dev/disk/by-id/[^/]+$")


Selector = ThermalZoneSelector | HwmonSelector | SmartctlSelector


class SourceConfig(_StrictModel):
    enabled: StrictBool = True
    kind: Literal["cpu", "gpu", "sys", "smart"]
    provider: Literal["thermal_zone", "hwmon", "smartctl"]
    selector: Selector
    poll_interval: float
    stale_after: float
    skip_standby: StrictBool = False

    _parse_poll = field_validator("poll_interval", mode="before", json_schema_input_type=str)(_duration)
    _parse_stale = field_validator("stale_after", mode="before", json_schema_input_type=str)(_duration)

    @model_validator(mode="after")
    def validate_provider(self) -> SourceConfig:
        expected = {
            "thermal_zone": ThermalZoneSelector,
            "hwmon": HwmonSelector,
            "smartctl": SmartctlSelector,
        }[self.provider]
        if not isinstance(self.selector, expected):
            raise ValueError(f"selector does not match provider {self.provider}")
        if self.skip_standby and self.provider != "smartctl":
            raise ValueError("skip_standby is only valid for smartctl")
        timeout = {"thermal_zone": 0.1, "hwmon": 0.2, "smartctl": 5.0}[self.provider]
        if self.stale_after <= self.poll_interval + timeout:
            raise ValueError("stale_after must exceed poll_interval plus read timeout")
        if self.kind == "smart" and self.provider not in {"hwmon", "smartctl"}:
            raise ValueError("smart sources require hwmon or smartctl")
        return self


class CurveConfig(_StrictModel):
    minimum_temperature_c: Finite
    minimum_duty_percent: Percent
    duty_increment_percent_per_c: PositiveFinite


class InputConfig(_StrictModel):
    source: str = Field(min_length=1)
    custom: CurveConfig | None = None
    boost_above_c: Finite | None = None


class FixedConfig(_StrictModel):
    duty_percent: Percent


class FanOffConfig(_StrictModel):
    enabled: StrictBool = False
    temperature_c: Finite

    @model_validator(mode="after")
    def reject_unverified_stop(self) -> FanOffConfig:
        if self.enabled:
            raise ValueError("fan stop is not verified on this device")
        return self


class OverrideConfig(_StrictModel):
    mode: Literal["fixed", "custom", "cool", "balanced", "quiet"]
    fixed: FixedConfig
    combine: Literal["max"]
    inputs: list[InputConfig] = Field(min_length=1)
    fan_off: FanOffConfig


class FanConfig(_StrictModel):
    minimum_running_duty_percent: Percent
    override: OverrideConfig


class FansConfig(_StrictModel):
    cpufan: FanConfig
    sysfan: FanConfig


class DiscoveryConfig(_StrictModel):
    enabled: StrictBool = True
    prefix: str = Field(min_length=1)


class TLSConfig(_StrictModel):
    ca_file: str
    certificate_file: str | None = None
    key_file: str | None = None

    @model_validator(mode="after")
    def cert_and_key_together(self) -> TLSConfig:
        if (self.certificate_file is None) != (self.key_file is None):
            raise ValueError("certificate_file and key_file must be configured together")
        return self


class MQTTConfig(_StrictModel):
    enabled: StrictBool
    broker: str = Field(pattern=r"^(?:tcp|ssl)://[^\s/]+:\d{1,5}$")
    client_id: str = Field(min_length=1)
    username: str | None = None
    password_file: str | None = None
    topic_prefix: str = Field(min_length=1)
    tls: TLSConfig | None = None
    discovery: DiscoveryConfig


class RuntimeConfig(_StrictModel):
    shutdown_action: Literal["hold", "restore_bios"]
    sensor_failure_action: Literal["max_then_exit"]


class AppConfig(_StrictModel):
    version: Literal[1]
    device: DeviceConfig
    control: ControlConfig
    sources: dict[str, SourceConfig] = Field(min_length=1)
    fans: FansConfig
    mqtt: MQTTConfig
    runtime: RuntimeConfig

    @model_validator(mode="after")
    def validate_policy_references(self) -> AppConfig:
        presets = {"quiet": 72.0, "balanced": 70.0, "cool": 68.0}
        for fan_name in ("cpufan", "sysfan"):
            fan = getattr(self.fans, fan_name)
            override = fan.override
            if override.fixed.duty_percent < fan.minimum_running_duty_percent:
                raise ValueError(f"fans.{fan_name}.override.fixed.duty_percent is below minimum running duty")
            seen: set[str] = set()
            for index, input_config in enumerate(override.inputs):
                source = self.sources.get(input_config.source)
                path = f"fans.{fan_name}.override.inputs.{index}"
                if source is None:
                    raise ValueError(f"{path} references unknown source {input_config.source}")
                if not source.enabled:
                    raise ValueError(f"{path} references disabled source {input_config.source}")
                if input_config.source in seen:
                    raise ValueError(f"{path} duplicates source {input_config.source}")
                seen.add(input_config.source)
                if override.mode == "custom" and input_config.custom is None:
                    raise ValueError(f"{path}.custom is required in custom mode")
                if override.mode in presets and source.kind != "cpu" and input_config.custom is None:
                    raise ValueError(f"{path}: non-CPU preset input requires custom curve")
                curve_min = input_config.custom.minimum_temperature_c if input_config.custom else presets.get(override.mode)
                if input_config.boost_above_c is not None and curve_min is not None and input_config.boost_above_c <= curve_min:
                    raise ValueError(f"{path}.boost_above_c must be above minimum temperature")
        return self


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False):
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ConfigError(f"duplicate key {key!r} at line {key_node.start_mark.line + 1}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def _format_validation(error: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, item['loc']))}: {item['msg']}" for item in error.errors(include_input=False, include_url=False))


def load_config(path: Path) -> AppConfig:
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
        return AppConfig.model_validate(data)
    except ConfigError:
        raise
    except (OSError, yaml.YAMLError) as error:
        raise ConfigError(str(error)) from error
    except ValidationError as error:
        raise ConfigError(_format_validation(error)) from error


_MUTABLE_PATH = re.compile(
    r"(?:control\.mode|fans\.(?:cpufan|sysfan)\.override\.(?:mode|fixed\.duty_percent|"
    r"inputs\.\d+\.(?:custom\.(?:minimum_temperature_c|minimum_duty_percent|duty_increment_percent_per_c)|boost_above_c)))\Z"
)


def apply_changes(config: AppConfig, changes: dict[str, object]) -> AppConfig:
    candidate = config.model_dump(mode="python")
    for path, value in changes.items():
        if _MUTABLE_PATH.fullmatch(path) is None:
            raise ConfigError(f"{path}: path is not remotely mutable")
        parts = path.split(".")
        target: object = candidate
        try:
            for part in parts[:-1]:
                target = target[int(part)] if isinstance(target, list) else target[part]  # type: ignore[index]
            if not isinstance(target, dict) or parts[-1] not in target:
                raise KeyError(path)
            target[parts[-1]] = value
        except (KeyError, IndexError, ValueError, TypeError) as error:
            raise ConfigError(f"{path}: path does not identify an existing setting") from error
    try:
        return AppConfig.model_validate(candidate, context={"normalized_duration": True})
    except ValidationError as error:
        raise ConfigError(_format_validation(error)) from error
