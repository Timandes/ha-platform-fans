from abc import ABC, abstractmethod
from dataclasses import dataclass

from ha_nuc9_ec.model import DutyPair


class HardwareError(RuntimeError):
    """A bounded hardware operation or platform preflight failed."""


class PreflightError(HardwareError):
    """A permanent platform identity, access, or exclusivity check failed."""


@dataclass(frozen=True)
class DeviceInfo:
    board_name: str
    bios_version: str
    signature: str
    ec_version: str
    lgmr: int


class Backend(ABC):
    @property
    @abstractmethod
    def duty_bounds(self) -> tuple[int, int]: ...

    @abstractmethod
    def probe(self) -> DeviceInfo: ...

    @abstractmethod
    def set_duty(self, pair: DutyPair) -> None: ...

    @abstractmethod
    def restore_bios(self) -> None: ...

    @abstractmethod
    def read_rpm(self) -> tuple[int, int, int]: ...

    @abstractmethod
    def close(self) -> None: ...

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
