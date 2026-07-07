#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    commands = [
        ("python3", "-m", "unittest", "discover", "-s", "tests/phase4"),
        ("python3", "-m", "unittest", "discover", "-s", "tests/phase5"),
        ("git", "diff", "--check"),
    ]
    failed = []
    for command in commands:
        completed = subprocess.run(command, cwd=ROOT, text=True)
        if completed.returncode:
            failed.append(command)
    if failed:
        print({"failed": failed})
        return 1
    print({"status": "passed", "commands": commands})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
