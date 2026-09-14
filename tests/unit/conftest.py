from pathlib import Path

import pytest

from ha_nuc9_ec.config import load_config


@pytest.fixture
def example_path() -> Path:
    return Path(__file__).parents[2] / "config" / "example.yaml"


@pytest.fixture
def example_config(example_path):
    return load_config(example_path)
