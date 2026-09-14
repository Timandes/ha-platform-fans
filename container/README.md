# Container build and local acceptance

Production: `docker compose up --build -d` uses `config/container.yaml` (BIOS,
MQTT disabled). It explicitly maps `/dev/port` read/write, `/dev/mem` read-only,
host `/sys` read-only and the shared `/run/lock` directory, with `SYS_RAWIO`.
Review the actual host's access requirements before starting it; no privileged
fallback is provided. For enabled MQTT, add a read-only password-file bind to
`/run/secrets/mqtt_password` as shown in `compose.yaml`. Disabled MQTT requires
neither a password file nor a broker.

Hardware-free acceptance:

```sh
docker compose -f compose.mock.yaml up --build -d
uv run pytest tests/container/test_lifecycle.py -q
docker compose -f compose.mock.yaml down
```

The dedicated mock project starts in override using synthetic sources. Tests
kill/freeze only its mock process and create isolated, disposable MQTT/bad-config
containers. They require Docker, Compose, and the test-only Mosquitto image whose
digest is pinned in the test. `DOCKER_BIN` selects the CLI; `DOCKER_HOST` and
`DOCKER_CONFIG` follow standard Docker semantics. `NUC9_TEST_SHARE` selects a
writable directory visible at the same path to both pytest and the Docker daemon
(default `/private/tmp/ha-nuc9-vm-share`, prepared for the local Lima environment).
No host ports are published. The test suite must be invoked with the mock project
already running; it intentionally fails when that prerequisite is missing.

Python 3.12.13 and the amd64 base manifest, s6-overlay 3.2.3.2 archives, apt package
versions, and runtime wheel hashes are pinned. `container/requirements.txt` is
exported with `uv export --frozen --no-dev --no-emit-project --format requirements-txt`.
The source tree and small CLI launcher are copied directly; no project wheel or
floating build-system resolver is used. The base's pip 25.0.1 installs only
hash-verified binary wheels. No compiler is installed. Archive downloads are
verified before extraction; there is no downloaded shell execution.

The read-only root needs executable `/run` tmpfs for s6-generated scripts;
`/tmp` remains noexec. Service definitions live in `/etc/s6-overlay/s6-rc.d` and
bundle membership in `/etc/s6-overlay/user-bundles.d/user/contents.d` (s6 3.2.3.2).
The compiled services appear under `/run/service`. `health-monitor` depends on
`nuc9-controller` so it stops first. The monitor uses setpriv to drop every
capability and set NoNewPrivs before importing the hardware-free health code.
HEALTHCHECK only reads health. Permanent exit 78 leaves the service down and
unhealthy until an explicit container restart; exit 75 has a five-second delay,
with a seven-second finish timeout. Normal Docker stop has a 15-second grace.
Neither finish nor the monitor restores BIOS or performs hardware operations.

Linux health identity reads the kernel thread-group leader's
`/proc/PID/task/PID/stat` field 22. This preserves strict cross-process identity
under native Linux and user-mode emulators that synthesize `/proc/self/stat`.
Mock/emulated acceptance does not replace the required NUC9 hardware release
checks, including output bounds and simultaneous 80/80 operation.
