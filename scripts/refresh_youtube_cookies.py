"""Refresh Downtify's YouTube cookies.txt from a logged-in local browser.

Reads the YouTube/Google cookies out of your browser's cookie store and
writes them in Netscape format where the Downtify container can read
them. Point Downtify at the output file once (``DOWNTIFY_COOKIES_FILE``);
Downtify re-reads it on every download, so scheduled runs keep it fresh
without touching the web UI.

Typical Unraid setup (PC + Task Scheduler, daily):

    python refresh_youtube_cookies.py \
        --out \\\\TOWER\\appdata\\downtify\\cookies-auto.txt

    # Container variable:
    DOWNTIFY_COOKIES_FILE=/data/cookies-auto.txt

Firefox is the most reliable source on Windows (Chrome ≥127 encrypts
cookies with App-Bound Encryption that third-party readers often cannot
open). The script tries the requested browser, then the others, and
validates the result before overwriting the target.
"""

from __future__ import annotations

import argparse
import sys
from http.cookiejar import Cookie
from pathlib import Path

try:
    import browser_cookie3
except ImportError:  # pragma: no cover
    print(
        'browser-cookie3 missing: pip install browser-cookie3',
        file=sys.stderr,
    )
    raise SystemExit(1)

# Cookies YouTube/Google actually authenticate with; everything else in
# the browser jar is noise we don't want to copy.
_KEEP_DOMAINS = ('youtube.com', 'google.com')


def _wanted(cookie: Cookie) -> bool:
    domain = (cookie.domain or '').lstrip('.').lower()
    return domain.endswith(_KEEP_DOMAINS)


def _netscape_lines(cookies: list[Cookie]) -> list[str]:
    lines = ['# Netscape HTTP Cookie File', '']
    for c in sorted(cookies, key=lambda c: (c.domain, c.name)):
        domain = c.domain or ''
        lines.append('\t'.join([
            domain,
            'TRUE' if domain.startswith('.') else 'FALSE',
            c.path or '/',
            'TRUE' if c.secure else 'FALSE',
            str(int(c.expires or 0)),
            c.name,
            c.value or '',
        ]))
    return lines


def _collect(browser: str) -> list[Cookie]:
    load = getattr(browser_cookie3, browser)
    jar = load()
    return [c for c in jar if _wanted(c)]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0]
    )
    parser.add_argument(
        '--out',
        required=True,
        help='Target cookies.txt path (e.g. a Unraid share path)',
    )
    parser.add_argument(
        '--browser',
        default='firefox',
        choices=['firefox', 'chrome', 'edge', 'chromium', 'opera', 'brave'],
        help='Browser to read first (falls back to the others)',
    )
    parser.add_argument(
        '--min-cookies',
        type=int,
        default=10,
        help='Fewer YouTube/Google cookies than this counts as a failed read',
    )
    args = parser.parse_args()

    browsers = [args.browser] + [
        b for b in ('firefox', 'chrome', 'edge', 'brave', 'opera', 'chromium')
        if b != args.browser
    ]
    cookies: list[Cookie] = []
    used: str = ''
    errors: list[str] = []
    for browser in browsers:
        try:
            cookies = _collect(browser)
        except Exception as exc:
            errors.append(f'{browser}: {type(exc).__name__}: {exc}')
            continue
        if len(cookies) >= args.min_cookies:
            used = browser
            break
        errors.append(f'{browser}: only {len(cookies)} YouTube/Google cookies')

    if not cookies or len(cookies) < args.min_cookies:
        print('No usable cookies found:', file=sys.stderr)
        for line in errors:
            print(f'  {line}', file=sys.stderr)
        print(
            'Log into youtube.com in the browser and make sure the cookie '
            'store is readable (Chrome on Windows may block third-party '
            'readers — Firefox works).',
            file=sys.stderr,
        )
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        '\n'.join(_netscape_lines(cookies)) + '\n', encoding='utf-8'
    )
    names = sorted({c.name for c in cookies})
    print(f'{len(cookies)} cookies from {used} -> {out}')
    print(f'auth cookies present: {", ".join(names[:12])}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
