"""Tests for the cookie file parser (JSON / Netscape)."""

from __future__ import annotations

import json

from tiktok_scheduler.cli import _parse_cookie_file


def test_parse_json_array():
    raw = json.dumps(
        [
            {"name": "sessionid", "value": "abc", "domain": ".tiktok.com"},
            {"name": "tt_csrf_token", "value": "def", "domain": ".tiktok.com"},
        ]
    )
    cookies = _parse_cookie_file(raw)
    assert len(cookies) == 2
    assert cookies[0]["name"] == "sessionid"


def test_parse_json_object_with_cookies_key():
    raw = json.dumps({"cookies": [{"name": "x", "value": "1", "domain": ".tiktok.com"}]})
    cookies = _parse_cookie_file(raw)
    assert cookies == [{"name": "x", "value": "1", "domain": ".tiktok.com"}]


def test_parse_netscape_format():
    raw = (
        "# Netscape HTTP Cookie File\n"
        "# This is a generated file! Do not edit.\n"
        "\n"
        ".tiktok.com\tTRUE\t/\tTRUE\t1788883935\tsessionid\t1234567890\n"
        ".tiktok.com\tTRUE\t/\tTRUE\t0\ttt_csrf_token\tdeadbeef\n"
        "www.tiktok.com\tFALSE\t/\tFALSE\t1804867634\ti18next\ten\n"
    )
    cookies = _parse_cookie_file(raw)
    assert len(cookies) == 3
    by_name = {c["name"]: c for c in cookies}
    assert by_name["sessionid"]["value"] == "1234567890"
    assert by_name["sessionid"]["domain"] == ".tiktok.com"
    assert by_name["sessionid"]["secure"] is True
    assert by_name["sessionid"]["expirationDate"] == 1788883935
    assert by_name["tt_csrf_token"]["secure"] is True
    # expires=0 means session cookie -> no expirationDate
    assert "expirationDate" not in by_name["tt_csrf_token"]
    assert by_name["i18next"]["domain"] == "www.tiktok.com"
    assert by_name["i18next"]["secure"] is False


def test_parse_netscape_skips_blank_and_comment_lines():
    raw = "\n\n# comment\n\n  \n.foo.com\tTRUE\t/\tTRUE\t0\tn\tv\n"
    cookies = _parse_cookie_file(raw)
    assert len(cookies) == 1
