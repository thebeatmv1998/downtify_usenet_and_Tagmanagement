"""Server-side URL safety (SSRF guard for user-supplied feed URLs)."""

from __future__ import annotations

import socket

import pytest

from downtify import url_safety


def _resolve_to(monkeypatch, *addresses: str):
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                '',
                (address, port),
            )
            for address in addresses
        ]

    monkeypatch.setattr(socket, 'getaddrinfo', fake_getaddrinfo)


@pytest.mark.parametrize(
    'url',
    [
        'file:///etc/passwd',
        'ftp://example.com/feed.xml',
        'http://user:pass@example.com/feed.xml',
        'http:///feed.xml',
        'http://127.0.0.1/feed.xml',
        'http://localhost/feed.xml',
        'http://169.254.169.254/latest/meta-data/',
        'http://10.0.0.5/feed.xml',
        'http://192.168.1.1/feed.xml',
        'http://[::1]/feed.xml',
        'http://0.0.0.0/feed.xml',
    ],
)
def test_private_and_unsafe_urls_are_refused(monkeypatch, url):
    if 'localhost' in url:
        _resolve_to(monkeypatch, '127.0.0.1')
    with pytest.raises(url_safety.UnsafeUrl):
        url_safety.validate_public_url(url)


def test_a_public_host_is_allowed(monkeypatch):
    _resolve_to(monkeypatch, '93.184.216.34')
    assert (
        url_safety.validate_public_url('https://example.com/feed.xml')
        == 'https://example.com/feed.xml'
    )


def test_a_name_resolving_to_private_is_refused(monkeypatch):
    _resolve_to(monkeypatch, '93.184.216.34', '10.1.2.3')
    with pytest.raises(url_safety.UnsafeUrl):
        url_safety.validate_public_url('https://feeds.example.com/rss')


def test_private_feeds_can_be_enabled_explicitly(monkeypatch):
    monkeypatch.setenv('DOWNTIFY_ALLOW_PRIVATE_FEEDS', '1')
    assert (
        url_safety.validate_public_url('http://192.168.1.20:8000/feed.xml')
        == 'http://192.168.1.20:8000/feed.xml'
    )
    # The scheme check still applies.
    with pytest.raises(url_safety.UnsafeUrl):
        url_safety.validate_public_url('file:///etc/passwd')


def test_redirects_to_private_addresses_are_refused(monkeypatch):
    import httpx

    calls: list[str] = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return httpx.Response(
            302,
            headers={'location': 'http://127.0.0.1/secret'},
            request=httpx.Request('GET', url),
        )

    monkeypatch.setattr(httpx, 'get', fake_get)
    with pytest.raises(url_safety.UnsafeUrl):
        url_safety.safe_get('https://example.com/feed', timeout=5)
    assert calls == ['https://example.com/feed']
