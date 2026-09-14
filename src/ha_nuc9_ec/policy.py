from __future__ import annotations

import math
from collections.abc import Mapping

from .config import AppConfig, CurveConfig, FanConfig, InputConfig
from .model import DutyPair, Sample


class SourceUnavailable(RuntimeError):
    pass


_PRESETS: dict[str, CurveConfig] = {
    "quiet": CurveConfig(minimum_temperature_c=72, minimum_duty_percent=27, duty_increment_percent_per_c=2),
    "balanced": CurveConfig(minimum_temperature_c=70, minimum_duty_percent=27, duty_increment_percent_per_c=2),
    "cool": CurveConfig(minimum_temperature_c=68, minimum_duty_percent=27, duty_increment_percent_per_c=2),
}
PRESET_SOURCE_ID = "QXCFL579.0077"


def curve_duty(temp: float, minimum_temp: float, minimum: float, slope: float) -> int:
    if not all(math.isfinite(value) for value in (temp, minimum_temp, minimum, slope)):
        raise ValueError("curve values must be finite")
    return math.ceil(minimum + max(0.0, temp - minimum_temp) * slope)


def _sample(input_config: InputConfig, config: AppConfig, samples: Mapping[str, Sample], now: float) -> Sample:
    source_id = input_config.source
    sample = samples.get(source_id)
    if sample is None or sample.error is not None or sample.celsius is None:
        raise SourceUnavailable(f"source {source_id} is unavailable")
    if sample.source_id != source_id:
        raise SourceUnavailable(f"source {source_id} sample identity does not match")
    if not math.isfinite(sample.celsius) or not math.isfinite(sample.read_at) or sample.read_at > now:
        raise SourceUnavailable(f"source {source_id} has an invalid sample")
    if now - sample.read_at > config.sources[source_id].stale_after:
        raise SourceUnavailable(f"source {source_id} sample is stale")
    return sample


def _fan_duty(fan: FanConfig, config: AppConfig, samples: Mapping[str, Sample], now: float, bounds: tuple[int, int]) -> int:
    lower, upper = bounds
    override = fan.override
    if override.mode == "fixed":
        return min(upper, max(lower, fan.minimum_running_duty_percent, override.fixed.duty_percent))
    duties: list[int] = []
    boosted = False
    for input_config in override.inputs:
        sample = _sample(input_config, config, samples, now)
        if input_config.boost_above_c is not None and sample.celsius >= input_config.boost_above_c:
            boosted = True
        curve = input_config.custom or _PRESETS[override.mode]
        duties.append(curve_duty(sample.celsius, curve.minimum_temperature_c, curve.minimum_duty_percent, curve.duty_increment_percent_per_c))
    desired = upper if boosted else max(duties)
    return min(upper, max(lower, fan.minimum_running_duty_percent, desired))


def calculate(config: AppConfig, samples: Mapping[str, Sample], now: float, bounds: tuple[int, int]) -> DutyPair:
    lower, upper = bounds
    if isinstance(lower, bool) or isinstance(upper, bool) or not (0 <= lower <= upper <= 100):
        raise ValueError("bounds must be ordered integer percentages within 0..100")
    if config.control.mode != "override":
        raise ValueError("policy calculation requires override mode")
    return DutyPair(
        cpu=_fan_duty(config.fans.cpufan, config, samples, now, bounds),
        sys=_fan_duty(config.fans.sysfan, config, samples, now, bounds),
    )


class DownshiftGate:
    def __init__(self, delay_s: float):
        if not math.isfinite(delay_s) or delay_s < 0:
            raise ValueError("delay_s must be finite and non-negative")
        self.delay_s = delay_s
        self._current: DutyPair | None = None
        self._lower_since: list[float | None] = [None, None]

    def apply(self, desired: DutyPair, now: float, immediate: bool = False) -> DutyPair:
        if not math.isfinite(now):
            raise ValueError("now must be finite")
        if self._current is None or immediate:
            self._current = desired
            self._lower_since = [None, None]
            return desired
        current = [self._current.cpu, self._current.sys]
        wanted = [desired.cpu, desired.sys]
        for index in range(2):
            if wanted[index] >= current[index]:
                current[index] = wanted[index]
                self._lower_since[index] = None
            else:
                if self._lower_since[index] is None:
                    self._lower_since[index] = now
                if now - self._lower_since[index] >= self.delay_s:
                    current[index] = wanted[index]
                    self._lower_since[index] = None
        self._current = DutyPair(*current)
        return self._current
