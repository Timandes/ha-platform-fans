import fcntl
import os
from pathlib import Path

import pytest

from ha_nuc9_ec.hardware.base import HardwareError, PreflightError
from ha_nuc9_ec.hardware.linux import LinuxBackend, LinuxPortIO
from ha_nuc9_ec.model import DutyPair


class PreflightPort:
    version = bytes.fromhex("244400")

    def __init__(self, _fd):
        self.selected = 0
        self.values = {0x10: 0}
        self.offsets = {index: 0 for index in range(0x12, 0x15)}
        self.writes = []

    def write_byte(self, address, value):
        if address == 0x590:
            self.selected = value
        else:
            self.writes.append((self.selected, value))
            if self.selected == 0x10:
                self.values[0x10] = 0

    def read_byte(self, address):
        if self.selected == 0x10:
            return self.values[0x10]
        if self.selected in self.offsets:
            offset = self.selected - 0x12
            return self.version[offset]
        return self.values.get(self.selected, 0)


@pytest.fixture
def platform_root(tmp_path: Path):
    sys_root = tmp_path / "host/sys"
    dev_root = tmp_path / "devices"
    paths = {
        "board": sys_root / "class/dmi/id/board_name",
        "bios": sys_root / "class/dmi/id/bios_version",
        "pci": sys_root / "bus/pci/devices/0000:00:1f.0/config",
        "mem": dev_root / "mem",
        "port": dev_root / "port",
    }
    for path in paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
    paths["board"].write_text("NUC9i7QNB\n")
    paths["bios"].write_text("QXCFL579.0071.2022.1130.1331\n")
    paths["pci"].write_bytes(bytes(0x98) + bytes.fromhex("010041fe"))
    with paths["mem"].open("wb") as mem:
        mem.seek(0xFE410400)
        mem.write(b"SPG_EC")
    paths["port"].write_bytes(bytes(0x592))
    (tmp_path / "run/lock").mkdir(parents=True)
    return sys_root, dev_root, paths


def test_preflight_returns_identity_and_keeps_lock(monkeypatch, platform_root):
    sys_root, dev_root, _ = platform_root
    monkeypatch.setattr("ha_nuc9_ec.hardware.linux.LinuxPortIO", PreflightPort)
    backend = LinuxBackend.open(sys_root, sys_root.parent / "ha-nuc9-ec.lock", dev_root=dev_root)
    assert backend.probe().ec_version == "244400"
    assert backend.probe().signature == "SPG_EC"
    assert backend.duty_bounds == (30, 100)
    backend.close()


def test_lock_competition_fails(platform_root):
    sys_root, dev_root, _ = platform_root
    lock_path = sys_root.parent / "ha-nuc9-ec.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(PreflightError, match="lock"):
            LinuxBackend.open(sys_root, lock_path, dev_root=dev_root)
    finally:
        os.close(fd)


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    [("bios", b"wrong\n", "board/BIOS"), ("pci", bytes(0x9C), "LGMR"), ("mem", bytes(0x406), "signature")],
)
def test_identity_mismatch_never_opens_port(monkeypatch, platform_root, field, replacement, message):
    sys_root, dev_root, paths = platform_root
    paths[field].write_bytes(replacement)
    opened = False

    class ForbiddenPort:
        def __init__(self, _fd):
            nonlocal opened
            opened = True

    monkeypatch.setattr("ha_nuc9_ec.hardware.linux.LinuxPortIO", ForbiddenPort)
    with pytest.raises(PreflightError, match=message):
        LinuxBackend.open(sys_root, sys_root.parent / "lock", dev_root=dev_root)
    assert not opened


def test_version_mismatch_sends_query_only(monkeypatch, platform_root):
    sys_root, dev_root, _ = platform_root
    instances = []

    class WrongVersionPort(PreflightPort):
        version = bytes.fromhex("244401")

        def __init__(self, fd):
            super().__init__(fd)
            instances.append(self)

    monkeypatch.setattr("ha_nuc9_ec.hardware.linux.LinuxPortIO", WrongVersionPort)
    with pytest.raises(PreflightError, match="version"):
        LinuxBackend.open(sys_root, sys_root.parent / "lock", dev_root=dev_root)
    assert instances[0].writes == [(0x11, 0), (0x10, 0x01)]
    assert (0x10, 0x0D) not in instances[0].writes


def test_linux_backend_still_enforces_real_bounds(monkeypatch, platform_root):
    sys_root, dev_root, _ = platform_root
    monkeypatch.setattr("ha_nuc9_ec.hardware.linux.LinuxPortIO", PreflightPort)
    with LinuxBackend.open(sys_root, sys_root.parent / "lock", dev_root=dev_root) as backend:
        with pytest.raises(HardwareError, match="30..100"):
            backend.set_duty(DutyPair(29, 80))


def test_sys_root_is_the_mounted_sys_tree_not_a_filesystem_root(monkeypatch, platform_root):
    sys_root, dev_root, _ = platform_root
    monkeypatch.setattr("ha_nuc9_ec.hardware.linux.LinuxPortIO", PreflightPort)
    with LinuxBackend.open(sys_root, sys_root.parent / "lock", dev_root=dev_root) as backend:
        assert backend.probe().board_name == "NUC9i7QNB"


def test_port_io_rejects_short_read(tmp_path):
    path = tmp_path / "short-port"
    path.write_bytes(b"")
    fd = os.open(path, os.O_RDWR)
    try:
        with pytest.raises(HardwareError, match="short port read"):
            LinuxPortIO(fd).read_byte(0x591)
    finally:
        os.close(fd)


def test_port_io_rejects_short_write(monkeypatch, tmp_path):
    path = tmp_path / "port"
    path.write_bytes(bytes(0x592))
    fd = os.open(path, os.O_RDWR)
    monkeypatch.setattr(os, "pwrite", lambda _fd, _data, _offset: 0)
    try:
        with pytest.raises(HardwareError, match="short port write"):
            LinuxPortIO(fd).write_byte(0x590, 0x10)
    finally:
        os.close(fd)


def test_startup_mailbox_busy_is_temporary_and_releases_lock(monkeypatch, platform_root):
    sys_root, dev_root, _ = platform_root
    lock_path = sys_root.parent / 'lock'
    def busy(self):
        raise HardwareError('mailbox busy timeout')
    monkeypatch.setattr('ha_nuc9_ec.hardware.linux.Mailbox.read_version', busy)
    with pytest.raises(HardwareError, match='busy') as caught:
        LinuxBackend.open(sys_root, lock_path, dev_root=dev_root)
    assert not isinstance(caught.value, PreflightError)
    fd = os.open(lock_path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)


@pytest.mark.parametrize("duty", [30, 100])
def test_port_io_writes_supported_pwm_endpoints(tmp_path, duty):
    path = tmp_path / "port"
    path.write_bytes(bytes(0x592))
    fd = os.open(path, os.O_RDWR)
    try:
        LinuxPortIO(fd).write_byte(0x591, duty)
        assert os.pread(fd, 1, 0x591) == bytes([duty])
    finally:
        os.close(fd)


@pytest.mark.parametrize("duty", [29, 101])
def test_port_io_rejects_pwm_outside_supported_range(tmp_path, duty):
    path = tmp_path / "port"
    path.write_bytes(bytes(0x592))
    fd = os.open(path, os.O_RDWR)
    try:
        with pytest.raises(HardwareError, match="not allowed"):
            LinuxPortIO(fd).write_byte(0x591, duty)
        assert os.pread(fd, 1, 0x591) == b"\x00"
    finally:
        os.close(fd)
