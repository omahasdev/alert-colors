import tomllib
from pathlib import Path

import pytest

from alert_colors import http
from alert_colors.config import App
from alert_colors.http import LOOPBACK, REMOTE, NetworkPolicy, UnsafeRequestError, check_url

ROOT = Path(__file__).resolve().parents[1]
LOOPBACK_URL = "http://127.0.0.1:54235/razer/chromasdk"


@pytest.mark.parametrize("url", [
    "file:///C:/Windows/win.ini",
    "ftp://example.com/x",
    "data:application/json,{}",
    "http://example.com/",            # http needs allow_http
    "https://user:pw@example.com/",   # credentials in the URL
])
def test_refused_urls(url):
    with pytest.raises(UnsafeRequestError):
        check_url(url, REMOTE)


@pytest.mark.parametrize("host", ["127.0.0.1", "[::1]", "10.0.0.1", "192.168.1.1", "169.254.169.254", "0.0.0.0"])
def test_local_addresses_refused_for_remote(host):
    with pytest.raises(UnsafeRequestError):
        check_url(f"https://{host}/", REMOTE)


def test_local_addresses_allowed_only_on_request():
    check_url("https://192.168.1.1/", NetworkPolicy(allow_private_network=True))


def test_loopback_policy_allows_only_loopback():
    check_url(LOOPBACK_URL, LOOPBACK)
    with pytest.raises(UnsafeRequestError):
        check_url("http://192.168.1.1/", LOOPBACK)


def test_redirects_are_refused():
    handler = http._NoRedirect()
    with pytest.raises(UnsafeRequestError):
        handler.redirect_request(None, None, 302, "Found", {}, LOOPBACK_URL)


def test_response_size_limit(monkeypatch):
    limit = 10

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size):
            return b"[" + b"1," * size + b"1]"

    monkeypatch.setattr(http, "check_url", lambda url, policy: None)
    monkeypatch.setattr(http._OPENER, "open", lambda req, timeout: FakeResponse())
    with pytest.raises(UnsafeRequestError, match="larger"):
        http.request_json("https://example.com/", timeout_s=1, policy=NetworkPolicy(max_response_bytes=limit))


@pytest.mark.parametrize("name", ["..\escape.log", "C:\Windows\System32\evil.log"])
def test_log_file_must_stay_in_config_folder(tmp_path, name):
    app = App(tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8")), tmp_path)
    with pytest.raises(ValueError, match="inside"):
        app.log_path(name)
