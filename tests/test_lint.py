"""Ruff finds no issue in src, tests and packaging (rules: pyproject.toml [tool.ruff]).

The build (setup.cmd build) runs the tests, so an issue stops the build.
To see and fix the issues: .venv\\Scripts\\python -m ruff check --fix src tests packaging
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECKED = ["src", "tests", "packaging"]


def test_ruff_finds_no_issue():
    if importlib.util.find_spec("ruff") is None:
        pytest.skip("ruff is not installed: pip install --require-hashes -r requirements-build.txt")
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--no-cache", "--output-format", "concise", *CHECKED],
        cwd=ROOT, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
