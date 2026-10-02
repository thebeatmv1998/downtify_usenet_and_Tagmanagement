#!/usr/bin/env python3
"""Dump YouTube cookies from the running Chromium into a cookies.txt.

Connects to the browser's DevTools protocol (CDP) and writes the
YouTube/Google cookies in Netscape format — the file Downtify's
``DOWNTIFY_COOKIES_FILE`` points at. Designed for the cookies-keeper
container: a real Chromium with a persistent profile (one manual login
via its web UI), plus this loop refreshing the jar so Downtify always
has a live session.

The output file is only replaced when the jar actually authenticates
(contains one of the session cookies YouTube sets at login); a browser
that is not logged in must never clobber a still-working file.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

CDP_URL = os.getenv('CDP_URL', 'http://127.0.0.1:9222')
OUT_FILE = Path(os.getenv('OUT_FILE', '/out/cookies.txt'))
# Cookies YouTube only sets for an authenticated session.
_AUTH_COOKIE_NAMES = {
    'SID', 'SSID', 'HSID', 'SAPISID',
    '__Secure-1PSID', '__Secure-3PSID',
    '__Secure-1PAPISID', '__Secure-3PAPISID',
}
_KEEP_DOMAINS = ('youtube.com', 'google.com')


def _cdp_get_cookies() -> list[dict]:
    """All browser cookies via the DevTools protocol."""
    with urllib.request.urlopen(f'{CDP_URL}/json/version', timeout=10) as resp:
        version = json.load(resp)
    ws_url = version['webSocketDebuggerUrl']

    # Small dependency (websocket-client): the browser-level socket is the
    # only way to call Storage.getCookies without attaching to a page.
    from websocket import create_connection

    ws = create_connection(ws_url, timeout=15)
    try:
        ws.send(json.dumps({'id': 1, 'method': 'Storage.getCookies'}))
        while True:
            message = json.loads(ws.recv())
            if message.get('id') == 1:
                result = message.get('result') or {}
                return result.get('cookies') or []
    finally:
        ws.close()


def _wanted(cookie: dict) -> bool:
    domain = str(cookie.get('domain') or '').lstrip('.').lower()
    return domain.endswith(_KEEP_DOMAINS)


def _netscape_lines(cookies: list[dict]) -> list[str]:
    lines = ['# Netscape HTTP Cookie File', '']
    for c in sorted(cookies, key=lambda c: (c.get('domain') or '', c.get('name') or '')):
        domain = str(c.get('domain') or '')
        expires = c.get('expires') or 0
        # CDP reports session cookies as ~-1 / huge floats; Netscape uses
        # 0 for "expires at end of session".
        try:
            expires = int(float(expires))
        except (TypeError, ValueError):
            expires = 0
        if expires < 0 or expires > 253402300799:
            expires = 0
        lines.append('\t'.join([
            domain,
            'TRUE' if domain.startswith('.') else 'FALSE',
            str(c.get('path') or '/'),
            'TRUE' if c.get('secure') else 'FALSE',
            str(expires),
            str(c.get('name') or ''),
            str(c.get('value') or ''),
        ]))
    return lines


def is_authenticated(cookies: list[dict]) -> bool:
    names = {str(c.get('name') or '') for c in cookies}
    return bool(names & _AUTH_COOKIE_NAMES)


def dump() -> bool:
    """Refresh OUT_FILE; True when a new jar was written."""
    cookies = [c for c in _cdp_get_cookies() if _wanted(c)]
    if not is_authenticated(cookies):
        print(
            f'no authenticated YouTube session yet ({len(cookies)} cookies '
            'scanned) — keeping the existing file',
            flush=True,
        )
        return False
    payload = '\n'.join(_netscape_lines(cookies)) + '\n'
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(OUT_FILE.parent), suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as tmp:
            tmp.write(payload)
        os.replace(tmp_name, OUT_FILE)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    print(
        f'{len(cookies)} YouTube/Google cookies -> {OUT_FILE}',
        flush=True,
    )
    return True


def main() -> int:
    interval = int(os.getenv('DUMP_INTERVAL_SECONDS', '600'))
    while True:
        try:
            dump()
        except Exception as exc:
            print(f'dump failed: {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
        time.sleep(max(60, interval))


if __name__ == '__main__':
    raise SystemExit(main())
