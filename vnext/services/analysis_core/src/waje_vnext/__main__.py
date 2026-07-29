"""Minimal independent command entrypoint for Gate 0."""

from __future__ import annotations

import argparse
import json
from typing import Sequence

from .bootstrap import health_snapshot


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m waje_vnext")
    parser.add_argument("command", choices=("health",))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.command == "health":
        print(json.dumps(dict(health_snapshot()), sort_keys=True))
        return 0
    raise AssertionError("argparse only admits supported commands")


if __name__ == "__main__":
    raise SystemExit(main())
