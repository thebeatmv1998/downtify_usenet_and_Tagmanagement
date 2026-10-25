"""Refuse server-side HTTP requests to private or reserved addresses.

Podcast feeds and episode enclosures are user-supplied URLs: a normal
account could otherwise make the server probe ``127.0.0.1``, the LAN or
a cloud metadata endpoint. :func:`validate_public_url` resolves the host
and rejects loopback/private/link-local/reserved targets, and the
helpers here follow redirects manually so every hop is checked too.

Set ``DOWNTIFY_ALLOW_PRIVATE_FEEDS=1`` for a deliberately private feed
(a NAS-hosted podcast server, say). That disables the address check for
podcast feeds only; scheme and credential checks stay on.

DNS rebinding (a public name that resolves differently between the
check and the connection) remains a residual risk without pinning the
connection to the validated address.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from contextlib import contextmanager
from typing import Any, Iterator, Optional
from urllib.parse import urljoin, urlsplit

import httpx
from loguru import logger

_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_DEFAULT_PORTS = {'http': 80, 'https': 443}
_ALLOW_ENV = 'DOWNTIFY_ALLOW_PRIVATE_FEEDS'


class UnsafeUrl(ValueError):
    """The URL points somewhere the server must not fetch."""


def private_feeds_allowed() -> bool:
    raw = os.getenv(_ALLOW_ENV, '').strip().lower()
    return raw in {'1', 'true', 'yes', 'on'}


def validate_public_url(url: str) -> str:
    """Return *url* when it is an http(s) URL on a public address.

    Raises :class:`UnsafeUrl` for other schemes, embedded credentials,
    a missing host, or a host that resolves to a loopback, private,
    link-local, multicast, reserved or unspecified address.
    """

    text = str(url or '').strip()
    if not text or len(text) > 2048:
        raise UnsafeUrl('Invalid URL')
    try:
        parts = urlsplit(text)
    except ValueError as exc:
        raise UnsafeUrl('Invalid URL') from exc
    if parts.scheme.lower() not in _DEFAULT_PORTS:
        raise UnsafeUrl('Only http and https URLs are allowed')
    if parts.username or parts.password:
        raise UnsafeUrl('URLs with credentials are not allowed')
    host = parts.hostname
    if not host:
        raise UnsafeUrl('URL has no host')
    if private_feeds_allowed():
        return text
    port = parts.port or _DEFAULT_PORTS[parts.scheme.lower()]
    try:
        infos = socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
        )
    except socket.gaierror as exc:
        raise UnsafeUrl(f'Could not resolve {host!r}') from exc
    addresses = {info[4][0] for info in infos if info[4]}
    if not addresses:
        raise UnsafeUrl(f'Could not resolve {host!r}')
    for address in addresses:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            raise UnsafeUrl('Invalid resolved address') from None
        if (
            parsed.is_private
            or parsed.is_loopback
            or parsed.is_link_local
            or parsed.is_multicast
            or parsed.is_reserved
            or parsed.is_unspecified
        ):
            logger.warning(
                'Refusing a server-side request to private address {} ({})',
                address,
                host,
            )
            raise UnsafeUrl('URL points to a private or reserved address')
    return text


def _redirect_target(current: str, response: httpx.Response) -> Optional[str]:
    if response.status_code not in _REDIRECT_CODES:
        return None
    location = response.headers.get('location', '').strip()
    if not location:
        return None
    return urljoin(current, location)


def safe_get(
    url: str,
    *,
    timeout: Any,
    headers: Optional[dict[str, str]] = None,
    max_redirects: int = 4,
) -> httpx.Response:
    """``GET`` *url*, validating every redirect target before following."""

    current = validate_public_url(url)
    for _ in range(max_redirects + 1):
        response = httpx.get(
            current,
            timeout=timeout,
            headers=headers,
            follow_redirects=False,
        )
        target = _redirect_target(current, response)
        if target is None:
            return response
        response.close()
        current = validate_public_url(target)
    raise UnsafeUrl('Too many redirects')


@contextmanager
def safe_stream(
    url: str,
    *,
    timeout: Any,
    max_redirects: int = 4,
) -> Iterator[httpx.Response]:
    """Stream *url*, checking the initial address and every redirect."""

    current = validate_public_url(url)
    client = httpx.Client(timeout=timeout, follow_redirects=False)
    try:
        for _ in range(max_redirects + 1):
            request = client.build_request('GET', current)
            response = client.send(request, stream=True)
            target = _redirect_target(current, response)
            if target is None:
                try:
                    yield response
                finally:
                    response.close()
                return
            response.close()
            current = validate_public_url(target)
        raise UnsafeUrl('Too many redirects')
    finally:
        client.close()


def response_is_safe(response: httpx.Response) -> bool:
    """Best-effort check of the address actually connected to."""

    stream = getattr(response, 'extensions', {}).get('network_stream')
    address: Any = None
    if stream is not None:
        try:
            address = stream.get_extra_info('server_addr')
        except Exception:
            address = None
    if not address:
        return True
    host = address[0] if isinstance(address, tuple) else address
    try:
        parsed = ipaddress.ip_address(str(host))
    except ValueError:
        return True
    return not (
        parsed.is_private
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_multicast
        or parsed.is_reserved
        or parsed.is_unspecified
    )
