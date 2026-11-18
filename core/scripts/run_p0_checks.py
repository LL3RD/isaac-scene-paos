#!/usr/bin/env python3
"""Run the deterministic P0 contract checks without an LLM or Isaac runtime."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    return subprocess.call(
        [sys.executable, "-m", "unittest", "discover", "-s", str(ROOT / "tests"), "-p", "test_*.py", "-v"],
        cwd=ROOT,
    )


if __name__ == "__main__":
    raise SystemExit(main())
