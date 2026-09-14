# Task 5 MQTT / Home Assistant report

## Status

DONE_WITH_CONCERNS. The MQTT adapter, strict command parser, discovery builder,
controller snapshot extension, and CLI lifecycle integration are implemented.
The focused and full suites pass, including a real Mosquitto 2.0.22 plain/TLS
test. Several requested broker scenarios are not yet represented by dedicated
end-to-end tests; they are itemized below for root review.

## Implementation and interfaces

- Added `build_discovery(config) -> dict[str, dict]` for one HA device containing
  the global/fan selects, fixed and input numbers, RPM, source temperature and
  last-success timestamp, target PWM, and fault entities. Input entity identity
  uses the logical source ID; its command topic carries the current validated
  input index. The adapter compares discovery topic sets and publishes empty
  retained payloads for removed entities.
- Added strict `parse_command(topic, payload, retained)`: 16 KiB limit, duplicate
  JSON-key rejection at every object level, exact envelope fields, retained
  rejection, entity-to-dot-path mapping, numeric parsing that rejects booleans
  and non-finite spellings, and deterministic scalar request IDs.
- Added `MQTTAdapter`: Paho Callback API v2, MQTT 3.1.1 clean session, offline
  retained LWT, QoS 1, retained discovery/state/availability, non-retained
  results, HA birth subscription, bounded adapter and Paho publication queues,
  independent network/worker threads, reconnect backoff, and source-specific
  availability. On each connection it enqueues discovery/state before online.
- TLS uses required peer verification and never enables insecure mode. Passwords
  are read only from `password_file`, held only by the adapter lifetime, omitted
  from telemetry, and never included in errors/logging.
- CLI validates local credential and TLS files before opening hardware. MQTT
  network failure remains within Paho reconnect handling and never requests
  Runtime/Controller stop. Controller observation is synchronous and only
  performs a bounded nonblocking queue insertion.
- `StateSnapshot.last_success` preserves the last successful monotonic sample
  time after a read failure. MQTT converts it with one wall/monotonic anchor to
  UTC ISO8601. Reload retains history only for an unchanged logical source and
  clears history when the same source ID is replaced.
- Extended `config/mock.yaml` to document disabled credential behavior.

## TDD evidence

RED command:

`UV_CACHE_DIR=/private/tmp/ha-nuc9-uv-cache /Users/timandes/.local/bin/uv run pytest tests/unit/test_discovery.py tests/unit/test_mqtt_commands.py -q`

Expected result: collection failed with `ModuleNotFoundError` for both
`ha_nuc9_ec.discovery` and `ha_nuc9_ec.mqtt` before implementation.

Additional RED for reload history:

`... uv run pytest tests/unit/test_controller.py -k reload_source_replacement -q`

Expected result: the old `cpu_package` success time remained after replacing its
selector; 1 failed.

Focused GREEN:

`UV_CACHE_DIR=/private/tmp/ha-nuc9-uv-cache MOSQUITTO_BIN=/private/tmp/mosquitto-2.0.22/src/mosquitto /Users/timandes/.local/bin/uv run pytest tests/unit/test_discovery.py tests/unit/test_mqtt_commands.py tests/unit/test_controller.py tests/integration/test_mqtt_broker.py -q`

Result: **33 passed in 2.48s**.

Required Task 5 subset before the later controller-history test:

`... uv run pytest tests/unit/test_discovery.py tests/unit/test_mqtt_commands.py tests/integration/test_mqtt_broker.py -q`

Result: **11 passed in 2.46s**.

Full suite, run once before commit:

`UV_CACHE_DIR=/private/tmp/ha-nuc9-uv-cache MOSQUITTO_BIN=/private/tmp/mosquitto-2.0.22/src/mosquitto /Users/timandes/.local/bin/uv run pytest -q`

Result: **134 passed in 7.34s**, no skips or warnings shown.

`git diff --check` and `python -m compileall -q src tests` passed.

## Real broker evidence and gaps

- Covered against a real ephemeral Mosquitto 2.0.22: initial retained state is
  observed before online; HA birth is sent through the broker and handled;
  full JSON command/result flow; a control retained before adapter subscription
  is replayed with the retained flag and rejected; real TLS handshake verifies a
  generated localhost certificate against its CA.
- Same-ID behavior is covered through Controller public API unit tests (exact
  replay, conflict, and 256-entry eviction), but not repeated end-to-end through
  Mosquitto.
- Different source stale windows are covered by policy/scheduler unit tests, but
  not observed as multiple broker availability topics in this task's broker test.
- Removed discovery publication is implemented by topic-set comparison and the
  discovery identity reorder unit test, but an empty retained removal has not
  been asserted through Mosquitto.
- Broker loss cannot stop or mutate Controller/Runtime by construction (there is
  no such callback), but a live local control cycle during broker outage is not a
  dedicated integration assertion.
- LWT is configured before connect. The current broker test observes graceful
  stop publishing offline; it does not kill an adapter process to prove broker
  generated LWT. Abrupt loss plus reconnect state-before-online ordering also
  lacks a dedicated broker assertion.

## Files changed

`src/ha_nuc9_ec/mqtt.py`, `src/ha_nuc9_ec/discovery.py`,
`src/ha_nuc9_ec/model.py`, `src/ha_nuc9_ec/controller.py`,
`src/ha_nuc9_ec/cli.py`, `config/mock.yaml`,
`tests/unit/test_mqtt_commands.py`, `tests/unit/test_discovery.py`,
`tests/unit/test_controller.py`, `tests/integration/test_mqtt_broker.py`.

## Self-review

Checked discovery identity versus mutable input ordering, HA availability forms,
state-before-online ordering, source replacement history, bounded queues,
credential redaction, controller-thread isolation, disabled MQTT behavior, TLS
verification, worker shutdown, and diff whitespace. Corrected source discovery
to avoid mixing HA's singular `availability_topic` with its availability list,
bounded Paho's internal offline queue, and cleared old last-success history when
a logical source is replaced.
