#!/usr/bin/env python3
"""
tests/run_smoke_tests.py — run all GameManager smoke test suites via pytest.

Usage:
    python tests/run_smoke_tests.py
    GM_BASE=http://localhost:5001 python tests/run_smoke_tests.py
    python tests/run_smoke_tests.py -v          # verbose
    python tests/run_smoke_tests.py -k lonewolf # one suite only
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).parent


def main() -> None:
    # Any extra CLI args are forwarded to pytest (e.g. -v, -k, --tb=short)
    extra = sys.argv[1:]
    args: list[str] = [
        str(TESTS_DIR / "test_lonewolf_combat.py"),
        str(TESTS_DIR / "test_dnd5_combat.py"),
        "--tb=short",      # concise tracebacks by default
        "-v",              # show each test name
        *extra,
    ]
    sys.exit(pytest.main(args))


if __name__ == "__main__":
    main()
