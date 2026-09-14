from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .config import ConfigError, load_config
from .model import Sample
from .policy import SourceUnavailable, calculate


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ha-nuc9-ec")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="validate configuration without opening hardware")
    validate.add_argument("config", type=Path)
    evaluate = commands.add_parser("evaluate", help="evaluate override policy from a JSON sample snapshot")
    evaluate.add_argument("config", type=Path)
    evaluate.add_argument("samples", type=Path, help="JSON object keyed by source ID")
    evaluate.add_argument("--now", type=float, default=None)
    evaluate.add_argument("--bounds", nargs=2, type=int, metavar=("MIN", "MAX"), default=(40, 80))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "validate":
            print("configuration is valid")
            return 0
        raw = json.loads(args.samples.read_text(encoding="utf-8"))
        samples = {
            source_id: Sample(source_id, item.get("celsius"), item["read_at"], item.get("error"))
            for source_id, item in raw.items()
        }
        result = calculate(config, samples, time.monotonic() if args.now is None else args.now, tuple(args.bounds))
        print(json.dumps({"cpu": result.cpu, "sys": result.sys}, separators=(",", ":")))
        return 0
    except (ConfigError, SourceUnavailable, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
