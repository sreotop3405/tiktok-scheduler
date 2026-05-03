"""Tests for the proxy URL → Playwright config parser."""

from __future__ import annotations

from tiktok_scheduler.tiktok.client import _parse_proxy


def test_http_no_auth():
    assert _parse_proxy("http://1.2.3.4:8080") == {"server": "http://1.2.3.4:8080"}


def test_http_with_auth():
    cfg = _parse_proxy("http://user:pass@1.2.3.4:8080")
    assert cfg == {
        "server": "http://1.2.3.4:8080",
        "username": "user",
        "password": "pass",
    }


def test_socks5_no_auth():
    assert _parse_proxy("socks5://1.2.3.4:1080") == {"server": "socks5://1.2.3.4:1080"}


def test_socks5_with_auth_splits_credentials():
    cfg = _parse_proxy("socks5://user:pass@1.2.3.4:1080")
    assert cfg == {
        "server": "socks5://1.2.3.4:1080",
        "username": "user",
        "password": "pass",
    }


def test_bare_host_port_defaults_to_http():
    assert _parse_proxy("1.2.3.4:8080") == {"server": "http://1.2.3.4:8080"}


def test_url_encoded_password_is_decoded():
    cfg = _parse_proxy("http://u:p%40ss@1.2.3.4:8080")
    assert cfg["password"] == "p@ss"
