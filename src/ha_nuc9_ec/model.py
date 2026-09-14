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
