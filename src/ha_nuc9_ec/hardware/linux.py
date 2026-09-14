import fcntl
import os
from pathlib import Path

from ha_nuc9_ec.hardware.base import Backend, DeviceInfo, HardwareError, PreflightError
from ha_nuc9_ec.hardware.mailbox import Mailbox
from ha_nuc9_ec.model import DutyPair


EXPECTED_BOARD = "NUC9i7QNB"
EXPECTED_BIOS = "QXCFL579.0071.2022.1130.1331"
EXPECTED_LGMR = 0xFE410001
EXPECTED_SIGNATURE = b"SPG_EC"
EXPECTED_VERSION = bytes.fromhex("244400")


class LinuxPortIO:
    def __init__(self, fd: int):
        self._fd = fd

    def read_byte(self, address: int) -> int:
        if address != 0x591:
            raise HardwareError("port read not allowed")
        try:
            data = os.pread(self._fd, 1, address)
        except OSError as exc:
            raise HardwareError(f"port read failed: {exc}") from exc
        if len(data) != 1:
            raise HardwareError("short port read")
        return data[0]

    def write_byte(self, address: int, value: int) -> None:
        index_value = address == 0x590 and 0x10 <= value <= 0x16
        data_value = address == 0x591 and (value in (0, 1, 0x0D, 0x0E, 0x0F) or 40 <= value <= 80)
        if not (index_value or data_value):
            raise HardwareError("port write not allowed")
        try:
            written = os.pwrite(self._fd, bytes((value,)), address)
        except OSError as exc:
            raise HardwareError(f"port write failed: {exc}") from exc
        if written != 1:
            raise HardwareError("short port write")


class LinuxBackend(Backend):
    def __init__(self, info: DeviceInfo, mailbox: Mailbox, port_fd: int, lock_fd: int):
        self._info = info
        self._mailbox = mailbox
        self._port_fd = port_fd
        self._lock_fd = lock_fd
        self._closed = False

    @classmethod
    def open(
        cls,
        sys_root: Path,
        lock_path: Path,
        *,
        dev_root: Path = Path("/dev"),
    ) -> "LinuxBackend":
        """Open against a mounted sysfs tree and independently selected /dev tree."""
        sys_root = Path(sys_root)
        dev_root = Path(dev_root)
        board = cls._read_text(sys_root / "class/dmi/id/board_name", "DMI board")
        bios = cls._read_text(sys_root / "class/dmi/id/bios_version", "DMI BIOS")
        if (board, bios) != (EXPECTED_BOARD, EXPECTED_BIOS):
            raise PreflightError("unexpected board/BIOS")

        lock_fd = -1
        port_fd = -1
        try:
            flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
            lock_fd = os.open(lock_path, flags, 0o600)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise PreflightError("hardware lock is already held") from exc

            with open(sys_root / "bus/pci/devices/0000:00:1f.0/config", "rb", buffering=0) as pci:
                raw_lgmr = os.pread(pci.fileno(), 4, 0x98)
            if len(raw_lgmr) != 4 or int.from_bytes(raw_lgmr, "little") != EXPECTED_LGMR:
                raise PreflightError("unexpected LGMR")

            mem_fd = os.open(dev_root / "mem", os.O_RDONLY | os.O_CLOEXEC)
            try:
                signature = os.pread(mem_fd, 6, 0xFE410400)
            finally:
                os.close(mem_fd)
            if signature != EXPECTED_SIGNATURE:
                raise PreflightError("unexpected EC signature")

            port_fd = os.open(dev_root / "port", os.O_RDWR | os.O_CLOEXEC)
            mailbox = Mailbox(LinuxPortIO(port_fd))
            version = mailbox.read_version()
            if version != EXPECTED_VERSION:
                raise PreflightError("EC version mismatch")
            info = DeviceInfo(board, bios, signature.decode("ascii"), version.hex(), EXPECTED_LGMR)
            return cls(info, mailbox, port_fd, lock_fd)
        except Exception as exc:
            if port_fd >= 0:
                os.close(port_fd)
            if lock_fd >= 0:
                os.close(lock_fd)
            if isinstance(exc, HardwareError):
                raise
            if isinstance(exc, OSError):
                raise PreflightError(f"hardware preflight failed: {exc}") from exc
            raise

    @staticmethod
    def _read_text(path: Path, label: str) -> str:
        try:
            return path.read_text().strip()
        except OSError as exc:
            raise PreflightError(f"unable to read {label}") from exc

    @property
    def duty_bounds(self) -> tuple[int, int]:
        return (40, 80)

    def probe(self) -> DeviceInfo:
        return self._info

    def set_duty(self, pair: DutyPair) -> None:
        self._mailbox.set_duty(pair)

    def restore_bios(self) -> None:
        self._mailbox.restore_bios()

    def read_rpm(self) -> tuple[int, int, int]:
        return self._mailbox.read_rpm()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        os.close(self._port_fd)
        os.close(self._lock_fd)
