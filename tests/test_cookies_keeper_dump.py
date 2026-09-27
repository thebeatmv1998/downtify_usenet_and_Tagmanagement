"""cookies-keeper dump logic: CDP cookies → Netscape file, auth gate."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from downtify.cookies import check_cookies_file

_SPEC = importlib.util.spec_from_file_location(
    'dump_cookies',
    Path(__file__).resolve().parents[1] / 'docker' / 'cookies-keeper' / 'dump_cookies.py',
)
dump_cookies = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(dump_cookies)

_CDP_COOKIES = [
    {
        'name': 'SID', 'value': 'sid-value', 'domain': '.youtube.com',
        'path': '/', 'expires': 4102444800.0, 'secure': True,
    },
    {
        'name': 'VISITOR_INFO1_LIVE', 'value': 'v1', 'domain': '.youtube.com',
        'path': '/', 'expires': -1, 'secure': True,
    },
    {
        'name': 'NID', 'value': 'nid-value', 'domain': '.google.com',
        'path': '/', 'expires': 4102444800.0, 'secure': True,
    },
    # Unrelated cookies are dropped.
    {
        'name': 'OTHER', 'value': 'x', 'domain': '.example.com',
        'path': '/', 'expires': 4102444800.0, 'secure': False,
    },
]


def test_wanted_filters_to_youtube_and_google():
    kept = [c['name'] for c in _CDP_COOKIES if dump_cookies._wanted(c)]
    assert kept == ['SID', 'VISITOR_INFO1_LIVE', 'NID']


def test_netscape_lines_round_trip_through_downtify_validation(tmp_path):
    kept = [c for c in _CDP_COOKIES if dump_cookies._wanted(c)]
    lines = dump_cookies._netscape_lines(kept)
    assert lines[0] == '# Netscape HTTP Cookie File'
    target = tmp_path / 'cookies.txt'
    target.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    ok, warning = check_cookies_file(str(target))
    assert ok, warning


def test_session_cookies_get_expires_zero(tmp_path):
    lines = dump_cookies._netscape_lines([_CDP_COOKIES[1]])
    # CDP -1 (session cookie) becomes Netscape 0 (end of session).
    assert lines[-1].split('\t')[4] == '0'


def test_is_authenticated_needs_a_login_cookie():
    assert dump_cookies.is_authenticated(_CDP_COOKIES) is True
    assert dump_cookies.is_authenticated([_CDP_COOKIES[1]]) is False


def test_dump_skips_write_without_login(tmp_path, monkeypatch):
    monkeypatch.setattr(dump_cookies, 'OUT_FILE', tmp_path / 'cookies.txt')
    monkeypatch.setattr(
        dump_cookies, '_cdp_get_cookies', lambda: [_CDP_COOKIES[1]]
    )
    assert dump_cookies.dump() is False
    assert not (tmp_path / 'cookies.txt').exists()


def test_dump_writes_authenticated_jar_atomically(tmp_path, monkeypatch):
    out = tmp_path / 'out' / 'cookies.txt'
    monkeypatch.setattr(dump_cookies, 'OUT_FILE', out)
    monkeypatch.setattr(
        dump_cookies, '_cdp_get_cookies', lambda: _CDP_COOKIES
    )
    assert dump_cookies.dump() is True
    ok, warning = check_cookies_file(str(out))
    assert ok, warning
    content = out.read_text(encoding='utf-8')
    assert 'SID\tsid-value' in content
    assert 'OTHER' not in content
    # The tmp file is gone after the atomic replace.
    assert list(out.parent.glob('*.tmp')) == []
