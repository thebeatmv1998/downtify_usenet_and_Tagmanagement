"""Managed storage for the YouTube ``cookies.txt`` used by yt-dlp.

Cookies are what get age-restricted (explicit) tracks and bot-challenged
downloads working, but until now the only way to supply them was the
``DOWNTIFY_COOKIES_FILE`` environment variable plus a bind mount — a
non-starter on Docker Desktop for Windows, and a frequent source of
"downloads just fail" reports (henriquesebastiao/downtify#280).

This module keeps an uploaded ``cookies.txt`` inside the ``/data``
volume, so it survives container updates the same way ``settings.json``
and the Playlist Monitor database do. The environment variable still
wins when it's set: a deployment that mounts its own cookie file stays
in charge, and the web UI shows the upload controls as locked.
"""

from __future__ import annotations

import contextlib
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from loguru import logger

#: Environment variable that pins a cookie file, bypassing this store.
ENV_VAR = 'DOWNTIFY_COOKIES_FILE'

#: Cookie jars are a few KB at most; anything larger isn't a cookies.txt.
MAX_COOKIES_BYTES = 2 * 1024 * 1024

#: Netscape cookie lines have exactly these 7 tab-separated fields.
_NETSCAPE_FIELD_COUNT = 7

_USEFUL_DOMAINS = ('youtube.com', 'google.com')

#: First line browsers' export extensions write on a Netscape cookie jar.
_NETSCAPE_HEADERS = (
    '# Netscape HTTP Cookie File',
    '# HTTP Cookie File',
)


class InvalidCookiesFile(ValueError):
    """Raised when an upload isn't a usable Netscape cookies.txt."""


def _cookie_lines(text: str) -> list[list[str]]:
    """Split *text* into its Netscape cookie records.

    Comments and blank lines are dropped. ``#HttpOnly_`` is a real
    prefix browsers' export extensions write on otherwise normal
    records, so it's unwrapped rather than treated as a comment.
    """

    records: list[list[str]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith('#HttpOnly_'):
            line = line[len('#HttpOnly_') :]
        elif line.startswith('#'):
            continue
        fields = line.split('\t')
        if len(fields) == _NETSCAPE_FIELD_COUNT:
            records.append(fields)
    return records


def validate_cookies(content: bytes) -> list[str]:
    """Validate an uploaded cookie jar, returning non-fatal warnings.

    Raises :class:`InvalidCookiesFile` when the upload can't work at all
    — that's far kinder than accepting it and letting every download
    fail later with an opaque yt-dlp error.
    """

    if not content.strip():
        raise InvalidCookiesFile('The file is empty.')
    if len(content) > MAX_COOKIES_BYTES:
        raise InvalidCookiesFile(
            f'The file is larger than '
            f'{MAX_COOKIES_BYTES // (1024 * 1024)} MB, so it is not a '
            f'cookies.txt.'
        )
    try:
        text = content.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise InvalidCookiesFile(
            'The file is not UTF-8 text. Export it as a Netscape '
            'cookies.txt, not as a binary or spreadsheet file.'
        ) from exc

    records = _cookie_lines(text)
    if not records:
        raise InvalidCookiesFile(
            'No cookies found. The file must be in Netscape format — one '
            'cookie per line, fields separated by tabs. Use a browser '
            'extension such as "Get cookies.txt LOCALLY" to export it.'
        )

    warnings: list[str] = []
    domains = {fields[0].lstrip('.').lower() for fields in records}
    useful_records = [
        fields
        for fields in records
        if any(
            fields[0].lstrip('.').lower().endswith(useful)
            for useful in _USEFUL_DOMAINS
        )
    ]
    if not any(
        domain.endswith(useful)
        for domain in domains
        for useful in _USEFUL_DOMAINS
    ):
        warnings.append(
            'No youtube.com or google.com cookies found in this file. '
            'Export it from a tab logged into YouTube, otherwise it '
            "won't help with age-restricted tracks."
        )
    expiry_warning = _expiry_warning(useful_records)
    if expiry_warning is not None:
        warnings.append(expiry_warning)
    return warnings


def _expiry_warning(records: list[list[str]]) -> Optional[str]:
    """Warning when every relevant cookie in *records* has expired.

    Expiry ``0`` marks a session cookie — browsers don't persist those
    to the exported file, so they're treated as still valid. Expired
    session cookies can't be told apart from live ones by the file
    alone, but fixed-expiry cookies (which carry YouTube's actual login
    state) can be, and if all of those are in the past the jar won't
    authenticate anything.
    """

    if not records:
        return None
    now = datetime.now(tz=timezone.utc).timestamp()
    expiries: list[float] = []
    for fields in records:
        try:
            expiry = float(fields[4])
        except ValueError:
            # An unparsable expiry is a broken record, not an expired one.
            continue
        if expiry > 0:
            expiries.append(expiry)
    if not expiries:
        # Only session cookies — nothing checkable, assume the best.
        return None
    if any(expiry > now for expiry in expiries):
        return None
    return (
        'All youtube.com/google.com cookies in this file have expired. '
        'Export a fresh cookies.txt while still logged into YouTube, '
        "otherwise downloads will keep failing with YouTube's "
        '"Sign in to confirm you\'re not a bot" check.'
    )


def check_cookies_file(path: Path | str) -> tuple[bool, Optional[str]]:
    """Check the cookie jar at *path*, returning ``(ok, warning)``.

    ``ok`` is ``False`` when the file can't work at all (missing,
    unreadable, or not a Netscape cookies.txt) — *warning* then holds
    the reason. A usable-but-suspicious file returns ``True`` with a
    warning describing the suspicion (no YouTube cookies, everything
    expired). The file content is never logged; it holds session tokens.
    """

    file_path = Path(path)
    try:
        content = file_path.read_bytes()
    except OSError as exc:
        return False, f'Cookies file {file_path} is not readable: {exc}'
    try:
        warnings = validate_cookies(content)
    except InvalidCookiesFile as exc:
        return False, f'Cookies file {file_path} is invalid: {exc}'
    return True, warnings[0] if warnings else None


def default_cookiefile() -> str:
    """Cookie file for ad-hoc yt-dlp runs, or ``''`` when none exists.

    Mirrors :meth:`CookiesStore.active_path` for callers that have no
    store instance (the provider search helpers): the environment
    variable wins, otherwise the uploaded file at its default location
    under ``DATABASE_DIR``. Only existing files are returned so the
    result can be passed straight to yt-dlp's ``cookiefile``.
    """

    env_path = os.getenv(ENV_VAR, '').strip()
    if env_path and Path(env_path).is_file():
        return env_path
    store_path = Path(os.getenv('DATABASE_DIR', '/data')) / 'cookies.txt'
    if store_path.is_file():
        return str(store_path)
    return ''


class CookiesStore:
    """The uploaded ``cookies.txt``, plus the env var that overrides it."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    @staticmethod
    def env_override() -> str:
        """``DOWNTIFY_COOKIES_FILE``, or ``''`` when unset."""
        return os.getenv(ENV_VAR, '').strip()

    def is_locked(self) -> bool:
        """Whether the env var owns cookie configuration (UI read-only)."""
        return bool(self.env_override())

    def active_path(self) -> Optional[Path]:
        """The cookie file yt-dlp should use, if any.

        The environment variable wins so an existing deployment's bind
        mount keeps working exactly as before.
        """
        env_path = self.env_override()
        if env_path:
            return Path(env_path)
        return self._path if self._path.is_file() else None

    def status(self) -> dict[str, Any]:
        """Shape consumed by ``GET /api/cookies`` and the settings UI."""
        env_path = self.env_override()
        if env_path:
            return {
                'configured': Path(env_path).is_file(),
                'source': 'env',
                'locked': True,
                'path': env_path,
                'size': None,
                'updated_at': None,
            }
        if self._path.is_file():
            stat = self._path.stat()
            return {
                'configured': True,
                'source': 'upload',
                'locked': False,
                'path': str(self._path),
                'size': stat.st_size,
                'updated_at': datetime.fromtimestamp(
                    stat.st_mtime, tz=timezone.utc
                ).isoformat(),
            }
        return {
            'configured': False,
            'source': None,
            'locked': False,
            'path': None,
            'size': None,
            'updated_at': None,
        }

    def save(self, content: bytes) -> list[str]:
        """Validate and persist *content*, replacing any previous file.

        Returns the non-fatal warnings from :func:`validate_cookies`.
        """

        warnings = validate_cookies(content)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-replace so a failed write can't leave a truncated
        # jar behind that would break every subsequent download.
        tmp_path = self._path.with_name(f'{self._path.name}.tmp')
        tmp_path.write_bytes(content)
        # Session tokens: keep it out of reach of other users on the host.
        # Best-effort — filesystems without POSIX permissions (a Windows
        # bind mount, most notably) must not fail the upload itself.
        with contextlib.suppress(OSError):
            tmp_path.chmod(0o600)
        tmp_path.replace(self._path)
        logger.info('Stored uploaded cookies file at {}', self._path)
        return warnings

    def delete(self) -> bool:
        """Remove the uploaded file. ``False`` when there was none."""
        if not self._path.is_file():
            return False
        self._path.unlink()
        logger.info('Removed uploaded cookies file at {}', self._path)
        return True
