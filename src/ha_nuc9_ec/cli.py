from __future__ import annotations

import argparse
import asyncio
import fcntl
import os
import signal
from dataclasses import asdict, replace
import json
import sys
import time
import uuid
from pathlib import Path

from .config import ConfigError, load_config
from .health import DEFAULT_HEALTH_PATH, check_health, write_health
from .model import Sample, StateSnapshot
from .policy import SourceUnavailable, calculate


def _discover(sys_root: Path) -> None:
    root = sys_root.resolve(strict=True)
    print("# Discovered selectors; sysfs paths are resolved and cached at startup.")
    for directory in sorted((root / "class" / "thermal").glob("thermal_zone*")):
        try:
            sensor_type = (directory / "type").read_text().strip()
            if (directory / "temp").is_file():
                print(f"thermal_zone: {{type: {sensor_type}}}")
        except OSError:
            continue
    for directory in sorted((root / "class" / "hwmon").glob("hwmon*")):
        try:
            name = (directory / "name").read_text().strip()
        except OSError:
            continue
        for input_path in sorted(directory.glob("temp[1-9]*_input")):
            channel = input_path.name.removesuffix("_input")
            fields = [f"name: {name}", f"channel: {channel}"]
            try:
                fields.append(f"label: '{(directory / f'{channel}_label').read_text().strip()}'")
            except OSError:
                pass
            try:
                pci = next(part for part in reversed((directory / "device").resolve(strict=True).parts) if part.startswith("0000:"))
                fields.append(f"pci_address: '{pci}'")
            except (OSError, StopIteration):
                pass
            print("hwmon: {" + ", ".join(fields) + "}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ha-nuc9-ec")
    commands = parser.add_subparsers(dest="command", required=True)
    health = commands.add_parser('health', help='read controller health without accessing hardware')
    health.add_argument('--health-path', type=Path, default=DEFAULT_HEALTH_PATH)
    validate = commands.add_parser("validate", help="validate configuration without opening hardware")
    validate.add_argument("config", type=Path, nargs="?")
    validate.add_argument("--config", dest="config_option", type=Path)
    evaluate = commands.add_parser("evaluate", help="evaluate override policy from a JSON sample snapshot")
    evaluate.add_argument("config", type=Path)
    evaluate.add_argument("samples", type=Path, help="JSON object keyed by source ID")
    evaluate.add_argument("--now", type=float, default=None)
    evaluate.add_argument("--bounds", nargs=2, type=int, metavar=("MIN", "MAX"), default=(40, 80))
    discover = commands.add_parser("discover", help="list stable local sysfs temperature selectors")
    discover.add_argument("--sys-root", type=Path, default=Path("/sys"))
    run = commands.add_parser("run", help="run the fan controller", description="SIGHUP reloads file policy/sources; device/MQTT changes require process restart.")
    run.add_argument("config", type=Path, nargs="?")
    run.add_argument("--config", dest="config_option", type=Path)
    run.add_argument("--backend", choices=("mock", "linux"), default="linux")
    run.add_argument("--sys-root", type=Path, default=Path("/sys"))
    run.add_argument("--lock-path", type=Path)
    run.add_argument("--health-path", type=Path, help="Linux process health snapshot (default: /run/ha-nuc9-ec/health.json)")
    return parser


async def _run(args, config, config_path, publish_health=lambda state: None) -> int:
    from .controller import Controller, PermanentFailure, TemporaryFailure
    from .hardware.base import HardwareError, PreflightError
    from .hardware.linux import LinuxBackend
    from .hardware.mock import MockBackend
    from .runtime import Runtime, source_factory
    from .mqtt import MQTTAdapter, prepare_mqtt

    backend = None
    lock_fd = None
    installed = []
    mqtt_adapter = None
    unsubscribe = None
    unsubscribe_health = None
    loop = asyncio.get_running_loop()
    try:
        # Local credentials are permanent configuration and must fail before
        # opening any hardware path. Network reachability remains reconnectable.
        prepared_mqtt = prepare_mqtt(config.mqtt)
        if args.backend == 'mock':
            lock_path = args.lock_path or Path('/tmp/ha-nuc9-ec-mock.lock')
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise PreflightError('hardware lock is already held') from error
            backend = MockBackend()
        else:
            backend = await asyncio.to_thread(LinuxBackend.open, args.sys_root,
                                               args.lock_path or Path('/run/lock/ha-nuc9-ec.lock'))
        controller = Controller(config, backend, time.monotonic)
        unsubscribe_health = controller.subscribe(publish_health)
        runtime = Runtime(controller, reader_factory=source_factory(args.sys_root, mock=args.backend == 'mock'))
        mqtt_adapter = MQTTAdapter(
            config.mqtt,
            lambda changes, request_id: asyncio.run_coroutine_threadsafe(
                controller.change(changes, request_id), loop),
            device_id=config.device.id,
            prepared=prepared_mqtt,
        )
        unsubscribe = controller.subscribe(mqtt_adapter.publish_state)
        mqtt_adapter.publish_state(controller.snapshot())
        mqtt_adapter.start()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, runtime.request_stop, sig.name)
            installed.append(sig)
        loop.add_signal_handler(signal.SIGHUP, runtime.reload_event.set)
        installed.append(signal.SIGHUP)
        await runtime.run(config_path=config_path,
                          on_ready=lambda state: print(json.dumps({'event': 'ready', 'backend': args.backend, 'state': state.state}), flush=True),
                          on_reload=lambda result: print(json.dumps({'event': 'reload', **asdict(result)}), flush=True))
        return 0
    except (ConfigError, PreflightError, PermanentFailure, OSError, ValueError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 78
    except (HardwareError, TemporaryFailure) as error:
        print(f'error: {error}', file=sys.stderr)
        return 75
    finally:
        if unsubscribe_health is not None:
            unsubscribe_health()
        if unsubscribe is not None:
            unsubscribe()
        if mqtt_adapter is not None:
            mqtt_adapter.stop()
        for sig in installed:
            loop.remove_signal_handler(sig)
        if lock_fd is not None:
            os.close(lock_fd)
        if isinstance(backend, MockBackend):
            operations = [[item[0], *[asdict(value) if hasattr(value, '__dataclass_fields__') else value for value in item[1:]]] for item in backend.operations]
            print(json.dumps({'backend': 'mock', 'operations': operations}), flush=True)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    health_path = None
    health_lock_fd = None
    instance_id = uuid.uuid4().hex
    initial = StateSnapshot('starting', 'bios', 'unknown', 0, None, None, {}, None, None, {}, {})
    def publish_health(state):
        if health_path is not None:
            write_health(health_path, state, instance_id)
    try:
        if args.command == 'health':
            result = check_health(args.health_path, time.monotonic())
            print(json.dumps(asdict(result)))
            return 0 if result.status in ('healthy', 'starting') else 1
        if args.command == 'run':
            if sys.platform == 'linux':
                candidate = args.health_path or DEFAULT_HEALTH_PATH
                candidate.parent.mkdir(parents=True, exist_ok=True)
                # The health slot has its own lifetime lock, acquired before
                # configuration or hardware preflight. A losing startup must
                # never replace another process's progress or terminal state.
                health_lock_fd = os.open(str(candidate) + '.lock',
                                         os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
                try:
                    fcntl.flock(health_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise ConfigError('health slot lock is already held') from error
                health_path = candidate
            elif args.backend != 'mock' or args.health_path is not None:
                raise ConfigError('process health requires Linux; non-Linux mock runs omit --health-path')
            publish_health(initial)
        if args.command == "discover":
            _discover(args.sys_root)
            return 0
        config_path = getattr(args, 'config_option', None) or args.config
        if config_path is None:
            raise ConfigError('a configuration path is required')
        config = load_config(config_path)
        if args.command == 'run':
            code = asyncio.run(_run(args, config, config_path, publish_health))
            publish_health(replace(initial, state='permanent_failure' if code == 78 else 'stopped'))
            return code
        if args.command == "validate":
            print("configuration is valid")
            return 0
        raw = json.loads(args.samples.read_text(encoding="utf-8"))
        samples = {
            source_id: Sample(source_id, item.get("celsius"), item["read_at"], item.get("error"))
            for source_id, item in raw.items()
        }
        result = calculate(config, samples, time.monotonic() if args.now is None else args.now, tuple(args.bounds))
        print(json.dumps({"cpu": result.cpu, "sys": result.sys}, separators=(",", ":")))
        return 0
    except (ConfigError, SourceUnavailable, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        if health_path is not None:
            try:
                publish_health(replace(initial, state='permanent_failure'))
            except OSError:
                pass
        print(f"error: {error}", file=sys.stderr)
        return 78 if args.command == "run" else 2
    finally:
        if health_lock_fd is not None:
            os.close(health_lock_fd)


if __name__ == "__main__":
    raise SystemExit(main())
