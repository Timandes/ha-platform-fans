from __future__ import annotations

import math
from pathlib import Path

from ..config import HwmonSelector, SourceConfig, ThermalZoneSelector
from .base import SourceReadError


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
        return True
    except (OSError, ValueError):
        return False


def resolve_sysfs(source: SourceConfig, sys_root: Path) -> Path:
    root = sys_root.resolve(strict=True)
    matches: list[Path] = []
    if source.provider == "thermal_zone":
        selector = source.selector
        assert isinstance(selector, ThermalZoneSelector)
        for directory in sorted((root / "class" / "thermal").glob("thermal_zone*")):
            try:
                if (directory / "type").read_text().strip() == selector.type:
                    matches.append(directory / "temp")
            except OSError:
                continue
    elif source.provider == "hwmon":
        selector = source.selector
        assert isinstance(selector, HwmonSelector)
        for directory in sorted((root / "class" / "hwmon").glob("hwmon*")):
            try:
                if (directory / "name").read_text().strip() != selector.name:
                    continue
                if selector.label is not None and (directory / f"{selector.channel}_label").read_text().strip() != selector.label:
                    continue
                if selector.pci_address is not None:
                    device = (directory / "device").resolve(strict=True)
                    if selector.pci_address not in device.parts:
                        continue
                matches.append(directory / f"{selector.channel}_input")
            except OSError:
                continue
    else:
        raise SourceReadError("source is not a sysfs provider")
    safe = [path for path in matches if _inside(path, root) and path.is_file()]
    if not safe:
        raise SourceReadError("no matching sysfs sensor")
    if len(safe) > 1:
        raise SourceReadError("ambiguous sysfs sensor")
    return safe[0]


class SysfsReader:
    def __init__(self, source_id: str, source: SourceConfig, sys_root: Path = Path("/sys")) -> None:
        self.source_id = source_id
        self.path = resolve_sysfs(source, sys_root)

    def read(self) -> float:
        try:
            text = self.path.read_text(encoding="ascii").strip()
            if not text or any(character not in "+-0123456789." for character in text):
                raise ValueError
            value = float(text) / 1000.0
            if not math.isfinite(value):
                raise ValueError
            return value
        except (OSError, ValueError) as error:
            raise SourceReadError(f"invalid sysfs temperature in {self.path}") from error
