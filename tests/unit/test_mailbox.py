from collections import deque

import pytest

from ha_nuc9_ec.hardware.base import HardwareError
from ha_nuc9_ec.hardware.mailbox import Mailbox
from ha_nuc9_ec.hardware.mock import MockBackend
from ha_nuc9_ec.model import DutyPair


class FakePort:
    def __init__(self):
        self.selected = 0
        self.values = {0x10: 0}
        self.responses: dict[int, deque[int]] = {}
        self.logical_writes: list[tuple[int, int]] = []
        self.corrupt_parameter_readback = False

    def read_byte(self, address: int) -> int:
        assert address == 0x591
        if self.selected in self.responses and self.responses[self.selected]:
            return self.responses[self.selected].popleft()
        value = self.values.get(self.selected, 0)
        if self.corrupt_parameter_readback and self.selected in (0x11, 0x12):
            return value ^ 1
        return value

    def write_byte(self, address: int, value: int) -> None:
        if address == 0x590:
            self.selected = value
        else:
            assert address == 0x591
            self.values[self.selected] = value
            self.logical_writes.append((self.selected, value))
            if self.selected == 0x10:
                self.values[0x10] = 0


@pytest.fixture
def fake_port():
    return FakePort()


def test_set_duty_uses_atomic_parameter_readback_sequence(fake_port):
    mailbox = Mailbox(fake_port, sleep=lambda _: None)
    mailbox.set_duty(DutyPair(80, 40))
    assert fake_port.logical_writes == [(0x11, 80), (0x12, 40), (0x10, 0x0D)]


def test_readback_mismatch_never_commits(fake_port):
    mailbox = Mailbox(fake_port, sleep=lambda _: None)
    fake_port.corrupt_parameter_readback = True
    with pytest.raises(HardwareError, match="readback"):
        mailbox.set_duty(DutyPair(80, 40))
    assert (0x10, 0x0D) not in fake_port.logical_writes


@pytest.mark.parametrize("pair", [DutyPair(40, 40), DutyPair(80, 80)])
def test_real_mailbox_accepts_inclusive_verified_bounds(fake_port, pair):
    Mailbox(fake_port, sleep=lambda _: None).set_duty(pair)


@pytest.mark.parametrize("pair", [DutyPair(39, 40), DutyPair(40, 81), DutyPair(True, 40)])
def test_real_mailbox_rejects_unverified_or_non_integer_duty(fake_port, pair):
    with pytest.raises(HardwareError, match="40..80"):
        Mailbox(fake_port, sleep=lambda _: None).set_duty(pair)
    assert fake_port.logical_writes == []


def test_restore_waits_for_idle_acknowledgement(fake_port):
    Mailbox(fake_port, sleep=lambda _: None).restore_bios()
    assert fake_port.logical_writes == [(0x10, 0x0E)]


def test_rpm_response_is_stable_and_big_endian(fake_port):
    for index, byte in zip(range(0x11, 0x17), bytes.fromhex("07d008fc0a28")):
        fake_port.responses[index] = deque([byte, byte])
    assert Mailbox(fake_port, sleep=lambda _: None).read_rpm() == (2000, 2300, 2600)
    assert fake_port.logical_writes == [(0x10, 0x0F)]


def test_unstable_response_is_rejected(fake_port):
    fake_port.responses[0x11] = deque([0x07, 0x08])
    with pytest.raises(HardwareError, match="unstable"):
        Mailbox(fake_port, sleep=lambda _: None).read_rpm()


def test_mock_supports_full_algorithm_range_and_fault_injection():
    backend = MockBackend(rpm=(1000, 1100, 1200))
    assert backend.duty_bounds == (0, 100)
    backend.set_duty(DutyPair(0, 100))
    assert backend.read_rpm() == (1000, 1100, 1200)
    backend.restore_bios()
    backend.close()
    assert backend.operations == [
        ("set_duty", DutyPair(0, 100)),
        ("read_rpm",),
        ("restore_bios",),
        ("close",),
    ]
    with pytest.raises(HardwareError, match="busy"):
        MockBackend(busy_error=True).set_duty(DutyPair(50, 50))


def test_port_os_error_is_wrapped_as_runtime_hardware_error():
    class FailingPort(FakePort):
        def write_byte(self, address, value):
            raise OSError("device vanished")

    with pytest.raises(HardwareError, match="device vanished"):
        Mailbox(FailingPort(), sleep=lambda _: None).read_rpm()
