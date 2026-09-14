"""Generic s6 progress monitor. Never imports or accesses controller hardware.

Run with all capabilities dropped via setpriv (see container service scripts).
An exited process is s6's responsibility; only a confirmed stalled instance dies.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import subprocess
import time

from ha_nuc9_ec.health import DEFAULT_HEALTH_PATH, check_health, process_starttime


def service_status(service: Path) -> tuple[bool, bool, int]:
    result = subprocess.run(['s6-svstat', '-o', 'up,wantedup,pid', str(service)],
                            capture_output=True, text=True, check=True, timeout=0.5)
    up, wanted, pid = result.stdout.split()
    if up not in ('true', 'false') or wanted not in ('true', 'false'):
        raise ValueError('invalid s6 status')
    return up == 'true', wanted == 'true', int(pid)


def identity_and_staleness_still_match(path, snapshot, pidfd) -> bool:
    try:
        # Signal 0 verifies that the pinned process has not exited.
        signal.pidfd_send_signal(pidfd, 0)
        if process_starttime(snapshot.pid) != snapshot.starttime:
            return False
        fresh = check_health(path, time.monotonic())
        return fresh.status == 'stale' and (
            fresh.pid, fresh.starttime, fresh.instance_id) == (
                snapshot.pid, snapshot.starttime, snapshot.instance_id)
    except (OSError, ValueError, TypeError, IndexError):
        return False


def check_once(path: Path, read_status) -> bool:
    pidfd = None
    try:
        up, wanted, pid = read_status()
        if not up or not wanted or pid <= 0:
            return False
        snapshot = check_health(path, time.monotonic())
        if snapshot.status != 'stale' or snapshot.pid != pid:
            return False
        pidfd = os.pidfd_open(pid)
        if not identity_and_staleness_still_match(path, snapshot, pidfd):
            return False
        # Recheck supervisor intent immediately before the destructive action.
        if read_status() != (True, True, pid):
            return False
        signal.pidfd_send_signal(pidfd, signal.SIGKILL)
        return True
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    finally:
        if pidfd is not None:
            os.close(pidfd)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--health-path', type=Path, default=DEFAULT_HEALTH_PATH)
    parser.add_argument('--service-dir', type=Path, required=True)
    args = parser.parse_args()
    while True:
        check_once(args.health_path, lambda: service_status(args.service_dir))
        time.sleep(1)


if __name__ == '__main__':
    main()
