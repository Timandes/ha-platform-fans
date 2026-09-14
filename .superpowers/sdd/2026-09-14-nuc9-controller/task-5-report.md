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

## Review fix round 1

Addressed all seven Important findings from `task-5-review.md` in one follow-up
change:

1. Scalar commands now receive a fresh operation ID. The adapter separately maps
   MQTT QoS retransmission (`connection generation`, packet ID, DUP) to the same
   operation ID, while independent ABA operations and failure retries execute.
   Explicit JSON request IDs retain Controller's existing deduplication contract.
2. JSON command parsing has an explicit public dot-path allowlist. External state
   is assembled from an explicit telemetry allowlist and contains no broker,
   client ID, username, password path, CA, certificate, or key configuration.
3. Discovery command topics encode source IDs as reversible UTF-8 hex. Controller
   resolves that stable source identity to the current input index inside its
   serialized configuration transaction, rejecting a removed source. A reload
   reorder test proves the intended source is updated.
4. Replaced the shared drop-oldest queue with a coalesced latest-state slot,
   preserved connection flags, a bounded command queue, bounded result
   backpressure, connection resync state, and publish acceptance/ack checks.
   Discovery publishes only for configuration changes, birth, and connection.
   State publication uses a monotonic five-second deadline even under continuous
   command traffic. Reconnect publishes the coalesced latest state before online;
   a connection that precedes its first state waits to publish online.
5. `prepare_mqtt` reads the password and constructs a reusable verifying
   `SSLContext` before hardware opens. Client certificate/key loading passes an
   explicit empty password so encrypted keys cannot invoke an interactive prompt.
   The adapter receives that prepared object and never reparses files during
   reconnect. Bad CA and mismatched key tests return 78 and prove
   `LinuxBackend.open` was never called; errors contain no certificate contents.
6. Source-derived entity IDs and availability topics use collision-free reversible
   hex encoding. Jinja templates use JSON-escaped bracket access, preserving source
   IDs containing punctuation without collisions or invalid dot lookup.
7. The broker integration now uses a real Controller and asserts every specified
   scenario against ephemeral Mosquitto 2.0.22: explicit same-ID replay/conflict,
   scalar ABA and failure retry, HA birth discovery/state count increases, local
   Controller tick while the broker is stopped, 400 offline state updates coalesced
   to the newest revision before online on reconnect, SIGKILL of a separate adapter
   process causing broker-generated LWT, late first state before online, different
   source stale windows, and reload removal via empty retained discovery followed
   by a new subscriber receiving no deleted config. Continuous rejected command
   traffic also proves the five-second state publication is not starved.

Minor fixture findings were also fixed: broker lookup prefers `MOSQUITTO_BIN`, then
`PATH`, then the brief's local binary; all clients, subprocesses, adapters, and
controller loops use `finally` cleanup.

### Review RED evidence

`UV_CACHE_DIR=/private/tmp/ha-nuc9-uv-cache /Users/timandes/.local/bin/uv run pytest tests/unit/test_mqtt_commands.py tests/unit/test_discovery.py -q`

Result before fixes: **9 failed, 9 passed**. Failures demonstrated repeated scalar
IDs, absent public-path filtering, index-bound source commands, and colliding source
discovery IDs/templates.

The previous report's broker gaps were treated as failing acceptance criteria;
the expanded broker tests initially exposed an incorrect readiness predicate that
accepted source availability in place of global online. Tightening it to the exact
global topic produced the expected RED (`availability` absent at the asserted
position) until the test synchronized on the correct contract.

### Review GREEN evidence

Final affected regression command (the full suite was intentionally not repeated
for this review-only fix):

`UV_CACHE_DIR=/private/tmp/ha-nuc9-uv-cache MOSQUITTO_BIN=/private/tmp/mosquitto-2.0.22/src/mosquitto /Users/timandes/.local/bin/uv run pytest tests/unit/test_mqtt_commands.py tests/unit/test_mqtt_preflight.py tests/unit/test_discovery.py tests/unit/test_controller.py tests/integration/test_mqtt_broker.py tests/integration/test_mock_run.py -q`

Result: **59 passed in 16.86s**, no skips or warnings shown.

Focused real-broker run before the final combined run: `tests/integration/test_mqtt_broker.py`
reported **5 passed in 7.46s**; the final combined run contains six broker tests
after adding the late-first-state regression. `git diff --check` passed.

### Review self-check

Re-read the complete fix diff for topic identity, serialized resolution, public
payload contents, TLS reuse, queue bounds, publish retry state, connection timing,
result lifetime, cleanup, and retained deletion. Corrected a stale source-index
comment and encoded per-source availability topics consistently with discovery.
No physical hardware, NAS access, source transfer, or full-suite repetition was
performed in this fix round.

## Review fix round 2

Read `task-5-rereview.md` and fixed the remaining publication coordination and
test synchronization findings without changing the five already accepted areas.

- Publication work now captures the state sequence, connection generation, and
  birth sequence before waiting for PUBACKs. Completion advances only the exact
  captured sequence. State, birth, or reconnect events that arrive during PUBACK
  remain pending, and an old connection generation cannot mark a new connection
  online.
- Discovery reconciliation records every topic before its publish attempt. A
  successful retained deletion removes that topic individually; rejected or
  uncertain attempts remain in the conservative set. Thus a partial addition,
  disconnect, local removal, and reconnect still deletes every possibly retained
  entity.
- Added deterministic PUBACK barriers covering a state arriving during publish
  and simultaneous birth/new connection generation. Added a deterministic
  partial-publish, disconnect, remove, reconnect regression.
- Birth integration waits for both increased discovery and state counts. Outage
  readiness uses the exact global availability topic. The deleted-discovery
  newcomer waits for SUBACK and a delivered probe before asserting absence.
  TLS and newcomer cleanup now runs in `finally`.
- The qemu-sensitive single-instance fixture now gives each process phase its
  own bounded timeout: first ready 3s, second rejection 3s, first stop 2s, third
  ready 3s, and third stop 2s. Product timeouts are unchanged.

### Round 2 RED evidence

The rereview's focused reproduction against `4419821` observed an old revision-0
announce blocked on PUBACK. Publishing revision 1 during that barrier left
`_latest.revision == 1` but incorrectly cleared `_state_dirty`; this is the exact
race encoded by `test_puback_barrier_preserves_newer_state_sequence`. The same
unconditional flag clear affected birth and reconnect generations. The partial
discovery regression models a first attempted retained entity followed by a
failed publish, disconnect, removal, and reconnect; the old all-or-nothing topic
set could not schedule its deletion. Root's Linux rereview also supplied the
birth RED: discovery count increased while the immediately asserted state count
remained 1.

### Round 2 GREEN evidence

Deterministic barriers plus the platform fixture:

`UV_CACHE_DIR=/private/tmp/ha-nuc9-uv-cache /Users/timandes/.local/bin/uv run pytest tests/unit/test_mqtt_adapter.py tests/integration/test_mock_run.py::test_mock_single_instance_lock_and_release -q`

Result: **4 passed in 0.34s**.

Real Mosquitto integration after synchronization fixes:

`UV_CACHE_DIR=/private/tmp/ha-nuc9-uv-cache MOSQUITTO_BIN=/private/tmp/mosquitto-2.0.22/src/mosquitto /Users/timandes/.local/bin/uv run pytest tests/integration/test_mqtt_broker.py -q`

Result: **6 passed in 14.43s**.

Final affected Mac regression:

`UV_CACHE_DIR=/private/tmp/ha-nuc9-uv-cache MOSQUITTO_BIN=/private/tmp/mosquitto-2.0.22/src/mosquitto /Users/timandes/.local/bin/uv run pytest tests/unit/test_mqtt_adapter.py tests/unit/test_mqtt_commands.py tests/unit/test_mqtt_preflight.py tests/unit/test_discovery.py tests/unit/test_controller.py tests/integration/test_mqtt_broker.py tests/integration/test_mock_run.py -q`

Result: **62 passed in 15.65s**, no skips or warnings shown. `git diff --check`
passed. The full suite was not repeated, as requested.

The agent shell had no `docker` command, so the Linux amd64 testbase command was
not run here. Root confirmed it will run the affected broker/mock-runtime tests
on the frozen follow-up commit using the configured private Docker CLI/socket.

### Round 2 self-review

Checked each event update and completion under the adapter lock, verified online
is guarded by the captured generation, and verified uncertain discovery attempts
survive until confirmed deletion. Reviewed all edited fixtures for exact-topic
synchronization, SUBACK/delivery observability, and failure cleanup. No hardware,
NAS, external network, or broad suite was used.
