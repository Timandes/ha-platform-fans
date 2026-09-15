from __future__ import annotations

import math
from collections.abc import Mapping
from fractions import Fraction

from .config import CPU_PRESETS, AppConfig, FanConfig, InputConfig
from .model import DutyPair, Sample


class SourceUnavailable(RuntimeError):
    pass


def curve_duty(temp: float, minimum_temp: float, minimum: float, slope: float, *, upper: int = 100) -> int:
    if not all(math.isfinite(value) for value in (temp, minimum_temp, minimum, slope)):
        raise ValueError("curve values must be finite")
    delta = max(0.0, temp - minimum_temp)
    if math.isfinite(delta):
        desired = minimum + delta * slope
    else:
        # A positive overflowing difference can still yield a small duty with
        # a tiny slope. Preserve that result instead of saturating the delta.
        desired = Fraction(minimum) + (Fraction(temp) - Fraction(minimum_temp)) * Fraction(slope)
    # Clamp before ceil: finite inputs can overflow the float multiply/add.
    # Saturating each input commutes with the fan's maximum and final clamp.
    return math.ceil(min(upper, desired))


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


def _input_duty(fan: FanConfig, item: InputConfig, config: AppConfig,
                samples: Mapping[str, Sample], now: float, upper: int) -> tuple[int, bool]:
    sample = _sample(item, config, samples, now)
    boosted = item.boost_above_c is not None and sample.celsius >= item.boost_above_c
    curve = item.custom or CPU_PRESETS[fan.override.mode]
    duty = curve_duty(sample.celsius, curve.minimum_temperature_c,
                      curve.minimum_duty_percent, curve.duty_increment_percent_per_c, upper=upper)
    return duty, boosted


def _fan_duty(fan: FanConfig, config: AppConfig, samples: Mapping[str, Sample], now: float, bounds: tuple[int, int]) -> int:
    lower, upper = bounds
    override = fan.override
    if override.mode == "fixed":
        return min(upper, max(lower, fan.minimum_running_duty_percent, override.fixed.duty_percent))
    duties: list[int] = []
    boosted = False
    for input_config in override.inputs:
        duty, boost = _input_duty(fan, input_config, config, samples, now, upper)
        boosted |= boost
        duties.append(duty)
    desired = upper if boosted else max(duties)
    return min(upper, max(lower, fan.minimum_running_duty_percent, desired))


def calculate(config: AppConfig, samples: Mapping[str, Sample], now: float, bounds: tuple[int, int]) -> DutyPair:
    lower, upper = bounds
    if not math.isfinite(now):
        raise SourceUnavailable("policy now must be finite")
    if type(lower) is not int or type(upper) is not int or not (0 <= lower <= upper <= 100):
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


class UpshiftGate:
    """Each fan/input must remain above that fan's confirmed target on its own."""

    def __init__(self):
        self._higher_since: dict[tuple[str, str], float] = {}

    def confirmed(self, previous: DutyPair, current: DutyPair) -> None:
        # Any new target changes the comparison baseline. Do not reuse a wait
        # accumulated against a different PWM (including a downward change).
        for name, before, after in (('cpufan', previous.cpu, current.cpu),
                                    ('sysfan', previous.sys, current.sys)):
            if before != after:
                self._higher_since = {key: value for key, value in self._higher_since.items()
                                      if key[0] != name}

    def observe(self, config: AppConfig, sample: Sample, now: float,
                bounds: tuple[int, int], current: DutyPair) -> None:
        # Source callbacks can outnumber coalesced control ticks. A low/failed
        # sample must break continuity even if a later high sample replaces it.
        for name, target in (('cpufan', current.cpu), ('sysfan', current.sys)):
            fan = getattr(config.fans, name)
            if fan.override.mode == 'fixed':
                continue
            for item in fan.override.inputs:
                if item.source != sample.source_id or item.increase_delay is None:
                    continue
                key = (name, item.source)
                try:
                    duty, boost = _input_duty(fan, item, config, {sample.source_id: sample}, now, bounds[1])
                except SourceUnavailable:
                    self._higher_since.pop(key, None)
                    continue
                if duty <= target or boost:
                    self._higher_since.pop(key, None)

    def apply(self, config: AppConfig, samples: Mapping[str, Sample], now: float,
              bounds: tuple[int, int], current: DutyPair) -> DutyPair:
        # Validate every required source first; stale/missing data still reaches
        # Controller's immediate max_then_exit path, never hidden by a timer.
        desired = calculate(config, samples, now, bounds)
        values = []
        lower, upper = bounds
        for name, target, raw_target in (('cpufan', current.cpu, desired.cpu),
                                         ('sysfan', current.sys, desired.sys)):
            fan = getattr(config.fans, name)
            if fan.override.mode == 'fixed':
                values.append(raw_target)
                continue
            duties = []
            boosted = False
            for item in fan.override.inputs:
                duty, boost = _input_duty(fan, item, config, samples, now, upper)
                key = (name, item.source)
                boosted |= boost
                if boost or duty <= target or item.increase_delay is None:
                    self._higher_since.pop(key, None)
                    duties.append(duty)
                    continue
                since = self._higher_since.setdefault(key, now)
                duties.append(duty if now - since >= item.increase_delay else target)
            requested = upper if boosted else max(duties)
            values.append(min(upper, max(lower, fan.minimum_running_duty_percent, requested)))
        return DutyPair(*values)
