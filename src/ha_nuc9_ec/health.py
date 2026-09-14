"""Hardware-free process identity and atomic control-progress snapshots."""
from __future__ import annotations

import json
import math
import os
import tempfile
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .model import StateSnapshot

DEFAULT_HEALTH_PATH = Path('/run/ha-nuc9-ec/health.json')


def process_starttime(pid: int) -> int:
    # comm may itself contain spaces and parentheses; field 22 follows it.
    return int(Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19])


@dataclass(frozen=True)
class HealthResult:
    status: str
    pid: int | None = None
    starttime: int | None = None
    instance_id: str | None = None


@lru_cache(maxsize=128)
def _instance_started_at(pid: int, starttime: int, instance_id: str) -> float:
    # One controller instance per process in production. Bounded caching also
    # supports in-process mock invocations without consulting previous JSON.
    # /proc starttime uses BOOTTIME, so it must never become a MONOTONIC deadline.
    return time.monotonic()


def write_health(path: Path, state: StateSnapshot, instance_id: str) -> None:
    pid = os.getpid()
    starttime = process_starttime(pid)
    payload = dict(pid=pid, starttime=starttime, instance_id=instance_id,
                   started_at=_instance_started_at(pid, starttime, instance_id),
                   last_cycle_at=state.last_cycle_at, state=state.state)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(payload, stream, allow_nan=False)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def check_health(path: Path, now: float) -> HealthResult:
    try:
        data = json.loads(path.read_text())
        pid, starttime, instance = data['pid'], data['starttime'], data['instance_id']
        if (type(pid) is not int or pid <= 0 or type(starttime) is not int or starttime < 0
                or not isinstance(instance, str) or not instance):
            raise ValueError('invalid identity')
        started, last = data['started_at'], data['last_cycle_at']
        for value in (started, now) if last is None else (started, last, now):
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError('invalid clock')
        result = lambda status: HealthResult(status, pid, starttime, instance)
        if data['state'] in ('stopped', 'permanent_failure'):
            return result(data['state'])
        if process_starttime(pid) != starttime:
            return result('stale')
        reference = started if last is None else last
        if reference > now:
            # The caller samples now before we open the JSON. A completed cycle
            # can be published between those operations. Recheck the clock after
            # the read; genuinely future or expired timestamps still fail.
            now = time.monotonic()
        if last is None:
            return result('starting' if data['state'] == 'starting' and 0 <= now - started < 10 else 'stale')
        return result('healthy' if data['state'] in ('bios', 'override') and 0 <= now - last < 2 else 'stale')
    except (OSError, ValueError, TypeError, KeyError, IndexError):
        return HealthResult('stale')
