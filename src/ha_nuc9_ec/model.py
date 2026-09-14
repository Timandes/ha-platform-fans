from dataclasses import dataclass


@dataclass(frozen=True)
class DutyPair:
    cpu: int
    sys: int


@dataclass(frozen=True)
class Sample:
    source_id: str
    celsius: float | None
    read_at: float
    error: str | None


@dataclass(frozen=True)
class CommandResult:
    request_id: str
    ok: bool
    revision: int
    error: str | None = None


@dataclass(frozen=True)
class StateSnapshot:
    state: str
    requested_mode: str
    applied_mode: str
    revision: int
    duty: DutyPair | None
    rpm: tuple[int, int, int] | None
    sources: dict[str, Sample]
    fault: str | None
    last_cycle_at: float | None
    configuration: dict
    last_success: dict[str, float]
