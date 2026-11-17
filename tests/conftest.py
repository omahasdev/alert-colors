"""Fixtures for all tests."""

import os

import pytest


@pytest.fixture(autouse=True)
def no_saved_tokens(monkeypatch):
    """The tests never read the registry of this PC: only the process environment."""
    monkeypatch.setattr("alert_colors.user_env.get", lambda name: os.environ.get(name))


@pytest.fixture(autouse=True)
def no_running_app(monkeypatch):
    """The tests never see the installed app of this PC: `devices` and `preview` run."""
    monkeypatch.setattr("alert_colors.stop_signal.running", lambda name: False)
    monkeypatch.setattr("alert_colors.stop_signal.mark_running", lambda name: None)
