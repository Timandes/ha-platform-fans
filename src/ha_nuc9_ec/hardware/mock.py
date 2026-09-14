from ha_nuc9_ec.hardware.base import Backend, DeviceInfo, HardwareError
from ha_nuc9_ec.model import DutyPair


class MockBackend(Backend):
    def __init__(
        self,
        *,
        rpm: tuple[int, int, int] = (0, 0, 0),
        busy_error: bool = False,
        readback_error: bool = False,
        short_read_error: bool = False,
    ):
        self.rpm = rpm
        self.busy_error = busy_error
        self.readback_error = readback_error
        self.short_read_error = short_read_error
        self.operations: list[tuple] = []
        self._info = DeviceInfo("mock", "mock", "mock", "000000", 0)

    @property
    def duty_bounds(self) -> tuple[int, int]:
        return (0, 100)

    def _check_fault(self) -> None:
        if self.busy_error:
            raise HardwareError("injected busy error")
        if self.readback_error:
            raise HardwareError("injected readback error")
        if self.short_read_error:
            raise HardwareError("injected short-read error")

    def probe(self) -> DeviceInfo:
        self._check_fault()
        self.operations.append(("probe",))
        return self._info

    def set_duty(self, pair: DutyPair) -> None:
        values = (pair.cpu, pair.sys)
        if any(not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 100 for value in values):
            raise HardwareError("mock duty must be an integer in 0..100")
        self._check_fault()
        self.operations.append(("set_duty", pair))

    def restore_bios(self) -> None:
        self._check_fault()
        self.operations.append(("restore_bios",))

    def read_rpm(self) -> tuple[int, int, int]:
        self._check_fault()
        self.operations.append(("read_rpm",))
        return self.rpm

    def close(self) -> None:
        self.operations.append(("close",))
