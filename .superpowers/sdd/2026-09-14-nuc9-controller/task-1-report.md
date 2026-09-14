# Task 1 Report: Strict configuration and policy entry point

## Result

Implemented the Python 3.12 package foundation, strict Pydantic configuration model, duplicate-key-safe YAML loading, atomic runtime changes, pure policy calculation, independent downshift gating, CLI validation/evaluation, generated JSON Schema, example configuration, dependency lock, and focused unit tests.

Commit target: `feat(config): add validated fan policies and source definitions`.

## Interfaces delivered

- `ha_nuc9_ec.config.load_config(path: Path) -> AppConfig`
- `ha_nuc9_ec.config.apply_changes(config, changes) -> AppConfig`
- Frozen `ha_nuc9_ec.model.DutyPair` and `Sample` dataclasses
- `ha_nuc9_ec.policy.calculate(config, samples, now, bounds) -> DutyPair`
- `ha_nuc9_ec.policy.DownshiftGate(delay_s).apply(desired, now, immediate=False)`
- `ha-nuc9-ec validate CONFIG`
- `ha-nuc9-ec evaluate CONFIG SAMPLES [--now ...] [--bounds MIN MAX]`

Source intervals are normalized to floating-point seconds in `SourceConfig`; Task 2 can consume `poll_interval` and `stale_after` directly. Selectors are provider-specific Pydantic models: `ThermalZoneSelector`, `HwmonSelector`, and `SmartctlSelector`.

## Validation behavior

- Every configuration object uses `extra="forbid"`.
- YAML duplicate keys fail before Pydantic validation.
- Durations require a complete numeric token ending in `ms` or `s`; internal normalized values remain seconds during atomic revalidation.
- Integer percentages reject booleans and values outside 0..100.
- Curve values reject NaN/infinity and non-positive slopes.
- Provider/selector combinations, `skip_standby`, source staleness windows, source references, disabled references, duplicate fan inputs, fixed/minimum consistency, custom/preset input requirements, and boost ordering are validated across the whole model.
- `fan_off.enabled=true` reports that stopping has not been verified on this device.
- Runtime changes only accept `control.mode`, fan override modes/fixed duties, and existing input curve/boost scalar paths. Candidate validation occurs before returning a new object; the old object is never mutated.
- CLI validation errors contain locations/messages but omit rejected input values, preventing secret echo.

## Policy behavior

- Custom curves use the specified ceiling formula.
- Automatic fan inputs are evaluated independently and combined by maximum required duty.
- Final fan output applies bounds and the configured minimum-running floor.
- Quiet/balanced/cool CPU defaults are identified as BIOS source `QXCFL579.0077`, with 72/70/68 C minimum temperatures, 27% base duty, and 2%/C slope. Explicit per-input curves override presets.
- Every referenced automatic sample must be present, error-free, finite, identity-matched, non-future, and within its own `stale_after`.
- Boost at or above threshold returns the supplied upper bound.
- Fixed mode ignores input samples and boost.
- Downshift timers are independent per CPU/SYS group; upshifts and explicit immediate applications bypass delay, and sustained lower targets apply the latest value.

## TDD and verification evidence

Initial required test run failed with `ModuleNotFoundError: No module named 'ha_nuc9_ec'`, after dependency installation and before production modules existed. After implementation and correction cycles:

```text
UV_CACHE_DIR=/private/tmp/ha-nuc9-uv-cache uv run pytest tests/unit/test_config.py tests/unit/test_policy.py -q
29 passed in 0.08s
```

Additional checks performed:

- `uv run ha-nuc9-ec validate config/example.yaml` -> `configuration is valid`
- `uv build` produced both sdist and wheel successfully; generated `dist/` artifacts were removed afterward.
- Generated `config/schema.json` from `AppConfig.model_json_schema()`.
- Reloaded `config/example.yaml` through `load_config` and checked all 18 object schema definitions have `additionalProperties: false`; duration input is represented as a string in the schema.
- `git diff --check` completed without whitespace errors before final commit preparation.

## Concerns and follow-up boundaries

- `calculate` intentionally rejects BIOS mode because there is no application-computed PWM in BIOS mode.
- `calculate` returns only `DutyPair`, so the later controller must detect a boost event when deciding to call `DownshiftGate.apply(..., immediate=True)`. The Task 1 interface has no metadata field to carry the boost reason.
- Source read timeout constants used for cross-validation are 100 ms for thermal zones, 200 ms for hwmon, and 5 s for smartctl, matching the design. Task 2 should keep those limits aligned if they become named shared constants.
- The CLI evaluate snapshot is a JSON object keyed by source ID with `celsius`, `read_at`, and optional `error`; it is a diagnostic entry point and does not access hardware.
