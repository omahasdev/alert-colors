"""user_env.py with a fake winreg and a fake broadcast: no test touches the registry of this PC."""

import ctypes
import os
import sys

import pytest

from alert_colors import user_env

REAL_GET = user_env.get  # conftest replaces user_env.get in each test; this is the real one
NAME = "ALERT_COLORS_TEST_VARIABLE"
VALUE = "test-value"


class FakeKey:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeWinreg:
    """The part of winreg that user_env.py uses, on a dict."""

    HKEY_CURRENT_USER = "HKCU"
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        self.values = {}
        self.opened = []
        self.fail_open = False

    def OpenKey(self, root, path, reserved=0, access=0):  # noqa: N802
        if self.fail_open:
            raise PermissionError("access denied")
        self.opened.append((root, path, access))
        return FakeKey()

    def QueryValueEx(self, key, name):  # noqa: N802
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value):  # noqa: N802
        self.values[name] = value

    def DeleteValue(self, key, name):  # noqa: N802
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]


@pytest.fixture
def registry(monkeypatch):
    fake = FakeWinreg()
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv(NAME, "before")  # pytest then restores the absence of the name
    monkeypatch.delenv(NAME)
    return fake


@pytest.fixture
def announcements(monkeypatch):
    calls = []
    monkeypatch.setattr(user_env, "_announce", lambda: calls.append(True))
    return calls


def test_get_prefers_the_process_environment(registry, monkeypatch):
    registry.values[NAME] = "from the registry"
    monkeypatch.setenv(NAME, VALUE)
    assert REAL_GET(NAME) == VALUE
    assert registry.opened == []


def test_get_reads_the_user_environment_when_the_process_has_no_value(registry):
    registry.values[NAME] = VALUE
    assert REAL_GET(NAME) == VALUE
    assert registry.opened == [(FakeWinreg.HKEY_CURRENT_USER, user_env.ENVIRONMENT_KEY, 0)]


def test_get_gives_none_for_a_missing_or_empty_registry_value(registry):
    assert REAL_GET(NAME) is None
    registry.values[NAME] = ""
    assert REAL_GET(NAME) is None


def test_get_gives_none_when_the_registry_key_cannot_open(registry):
    registry.fail_open = True
    assert REAL_GET(NAME) is None


def test_get_never_reads_a_registry_outside_windows(registry, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    registry.values[NAME] = VALUE
    assert REAL_GET(NAME) is None
    assert registry.opened == []


def test_put_saves_in_the_registry_and_in_this_process(registry, announcements):
    user_env.put(NAME, VALUE)
    assert registry.values[NAME] == VALUE
    assert os.environ[NAME] == VALUE
    assert registry.opened == [(FakeWinreg.HKEY_CURRENT_USER, user_env.ENVIRONMENT_KEY, FakeWinreg.KEY_SET_VALUE)]
    assert announcements == [True]


def test_delete_removes_the_value_and_announces_it(registry, announcements, monkeypatch):
    registry.values[NAME] = VALUE
    monkeypatch.setenv(NAME, VALUE)
    assert user_env.delete(NAME) is True
    assert NAME not in registry.values and NAME not in os.environ
    assert announcements == [True]


def test_delete_of_a_missing_value_is_false_and_silent(registry, announcements):
    assert user_env.delete(NAME) is False
    assert announcements == []


def test_announce_broadcasts_the_environment_change(monkeypatch):
    calls = []

    class FakeUser32:
        def SendMessageTimeoutW(self, *args):  # noqa: N802
            calls.append(args)
            return 1

    class FakeWindll:
        user32 = FakeUser32()

    monkeypatch.setattr(ctypes, "windll", FakeWindll(), raising=False)
    user_env._announce()
    (args,) = calls
    assert args[:6] == (user_env.HWND_BROADCAST, user_env.WM_SETTINGCHANGE, 0, user_env.ENVIRONMENT_KEY,
                        user_env.SMTO_ABORTIFHUNG, user_env.BROADCAST_TIMEOUT_MS)
