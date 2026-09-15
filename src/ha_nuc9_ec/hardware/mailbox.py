import time
from collections.abc import Callable
from typing import Protocol

from ha_nuc9_ec.hardware.base import HardwareError
from ha_nuc9_ec.model import DutyPair


class PortIO(Protocol):
    def read_byte(self, address: int) -> int: ...

    def write_byte(self, address: int, value: int) -> None: ...


class Mailbox:
    INDEX_PORT = 0x590
    DATA_PORT = 0x591

    def __init__(
        self,
        port: PortIO,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self._port = port
        self._sleep = sleep
        self._monotonic = monotonic

    def _select(self, index: int) -> None:
        if not 0x10 <= index <= 0x16:
            raise HardwareError("mailbox index not allowed")
        self._write_byte(self.INDEX_PORT, index)

    def _write_byte(self, address: int, value: int) -> None:
        try:
            self._port.write_byte(address, value)
        except OSError as exc:
            raise HardwareError(str(exc)) from exc

    def _read_byte(self, address: int) -> int:
        try:
            return self._port.read_byte(address)
        except OSError as exc:
            raise HardwareError(str(exc)) from exc

    def read_index(self, index: int) -> int:
        self._select(index)
        return self._read_byte(self.DATA_PORT)

    def write_parameter(self, index: int, value: int) -> None:
        if index not in (0x11, 0x12) or not isinstance(value, int) or isinstance(value, bool):
            raise HardwareError("mailbox parameter not allowed")
        self._select(index)
        self._write_byte(self.DATA_PORT, value)
        self._sleep(0.01)

    def wait_idle(self) -> None:
        deadline = self._monotonic() + 1.0
        while self.read_index(0x10) != 0:
            if self._monotonic() >= deadline:
                raise HardwareError("EC mailbox busy timeout")
            self._sleep(0.01)

    def submit(self, command: int) -> None:
        if command not in (0x01, 0x0D, 0x0E, 0x0F):
            raise HardwareError("mailbox command not allowed")
        self._select(0x10)
        self._write_byte(self.DATA_PORT, command)
        self._sleep(0.01)

    def _command(self, command: int, parameters: tuple[int, ...] = ()) -> None:
        allowed = (
            (command == 0x01 and parameters == (0,))
            or (command == 0x0D and len(parameters) == 2)
            or (command in (0x0E, 0x0F) and not parameters)
        )
        if not allowed:
            raise HardwareError("mailbox command arguments not allowed")
        self.wait_idle()
        for index, value in enumerate(parameters, 0x11):
            self.write_parameter(index, value)
        if tuple(self.read_index(index) for index in range(0x11, 0x11 + len(parameters))) != parameters:
            raise HardwareError("parameter readback mismatch")
        self.submit(command)
        self.wait_idle()

    def _response(self, start: int, size: int) -> bytes:
        indexes = tuple(range(start, start + size))
        first = bytes(self.read_index(index) for index in indexes)
        second = bytes(self.read_index(index) for index in indexes)
        if first != second or self.read_index(0x10) != 0:
            raise HardwareError("unstable mailbox response")
        return first

    def read_version(self) -> bytes:
        self._command(0x01, (0,))
        return self._response(0x12, 3)

    def set_duty(self, pair: DutyPair) -> None:
        values = (pair.cpu, pair.sys)
        if any(not isinstance(value, int) or isinstance(value, bool) or not 30 <= value <= 100 for value in values):
            raise HardwareError("real hardware duty must be an integer in 30..100")
        self._command(0x0D, values)

    def restore_bios(self) -> None:
        self._command(0x0E)

    def read_rpm(self) -> tuple[int, int, int]:
        self._command(0x0F)
        raw = self._response(0x11, 6)
        return tuple(int.from_bytes(raw[offset : offset + 2], "big") for offset in (0, 2, 4))  # type: ignore[return-value]
