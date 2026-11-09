"""Fixtures for all tests."""

import os

import pytest


@pytest.fixture(autouse=True)
def no_saved_tokens(monkeypatch):
    """The tests never read the registry of this PC: only the process environment."""
    monkeypatch.setattr("alert_colors.user_env.get", lambda name: os.environ.get(name))
