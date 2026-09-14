from __future__ import annotations

import json
import math
import subprocess
import threading
from pathlib import Path

from ..config import SmartctlSelector, SourceConfig
from .base import SourceReadError, SourceSkipped


class StandbySkip(SourceSkipped):
    pass


class SmartctlReader:
    def __init__(self, source_id: str, source: SourceConfig, *, executable: str | Path = "smartctl", timeout: float = 5.0) -> None:
        selector = source.selector
        if not isinstance(selector, SmartctlSelector):
            raise SourceReadError("smartctl reader requires a smartctl selector")
        self.source_id = source_id
        self.source = source
        self.selector = selector
        self.executable = str(executable)
        self.timeout = timeout
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None

    def read(self) -> float:
        arguments = [self.executable, "--json", "--attributes"]
        if self.selector.device_type is not None:
            arguments += ["--device", self.selector.device_type]
        if self.source.skip_standby:
            arguments += ["--nocheck", "standby,3,5"]
        arguments.append(self.selector.device)
        try:
            process = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except OSError as error:
            raise SourceReadError(f"could not start smartctl: {error}") from error
        with self._lock:
            self._process = process
        try:
            try:
                stdout, stderr = process.communicate(timeout=self.timeout)
            except subprocess.TimeoutExpired as error:
                process.kill()
                process.wait()
                raise SourceReadError("smartctl timed out") from error
        finally:
            with self._lock:
                if self._process is process:
                    self._process = None
        try:
            payload = json.loads(stdout)
        except (json.JSONDecodeError, UnboundLocalError) as error:
            raise SourceReadError(f"invalid smartctl JSON: {stderr.strip()}") from error
        if self.source.skip_standby and process.returncode == 3:
            raise StandbySkip("device is in standby")
        if self.source.skip_standby and process.returncode == 5:
            raise SourceReadError("smartctl standby check is not supported")
        if process.returncode & 0b111:
            raise SourceReadError(f"smartctl command failed with status {process.returncode}")
        value = payload.get("temperature", {}).get("current")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise SourceReadError("smartctl JSON has no valid temperature.current")
        return float(value)

    def close(self) -> None:
        with self._lock:
            process = self._process
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
