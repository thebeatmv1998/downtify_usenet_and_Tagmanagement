"""Search Discogs for a release to fill a track's tags with.

Discogs' database search needs a personal access token; the release
lookup itself is public. Both need a descriptive ``User-Agent`` and are
rate-limited, so requests are paced and cached the same way the
MusicBrainz module does it.
"""

from __future__ import annotations

import re
import time
from threading import Lock
from typing import Any, Optional
from urllib.parse import quote

import httpx
from loguru import logger

_BASE = 'https://api.discogs.com'
_TIMEOUT = 15
_MIN_INTERVAL = 1.1
_USER_AGENT = (
    'Downtify/3.4 (self-hosted music library; '
    '+https://github.com/henriquesebastiao/downtify)'
)

_lock = Lock()
_last_request = 0.0
_cooldown_until = 0.0
_search_cache: dict[str, list[dict[str, Any]]] = {}
_release_cache: dict[str, dict[str, Any]] = {}


class DiscogsTokenMissing(ValueError):
    """Raised when a search is attempted with no token configured."""


def _rate_wait() -> None:
    global _last_request
    with _lock:
        now = time.monotonic()
        wait = max(
            _cooldown_until - now,
            _MIN_INTERVAL - (now - _last_request),
        )
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()


def _get(path: str, params: dict[str, str], token: str) -> Optional[dict]:
    global _cooldown_until
    _rate_wait()
    headers = {'User-Agent': _USER_AGENT, 'Accept': 'application/json'}
    if token:
        headers['Authorization'] = f'Discogs token={token}'
    url = f'{_BASE}/{path}'
    try:
        resp = httpx.get(url, params=params, headers=headers, timeout=_TIMEOUT)
    except Exception:
        logger.opt(exception=True).debug('Discogs request failed: {}', url)
        return None
    if resp.status_code == 429:
        with _lock:
            _cooldown_until = time.monotonic() + 5.0
        logger.warning('Discogs throttled (HTTP 429)')
        return None
    if resp.status_code in {401, 403}:
        logger.warning(
            'Discogs refused the request (HTTP {})', resp.status_code
        )
        raise DiscogsTokenMissing(
            'Discogs rejected the token — check the personal access token '
            'in Settings.'
        )
    try:
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        logger.opt(exception=True).debug('Discogs response unreadable')
        return None
    return data if isinstance(data, dict) else None


def _split_title(title: str) -> tuple[str, str]:
    """Discogs lists a release as ``Artist - Album``."""

    artist, sep, album = str(title or '').partition(' - ')
    if not sep:
        return '', str(title or '').strip()
    return artist.strip(), album.strip()


def search_releases(
    query: str, *, token: str, limit: int = 10
) -> list[dict[str, Any]]:
    """Release matches for *query* (needs a Discogs token)."""

    text = str(query or '').strip()
    if not text:
        return []
    if not str(token or '').strip():
        raise DiscogsTokenMissing(
            'Discogs search needs a personal access token. Add one in '
            'Settings, or search MusicBrainz instead.'
        )
    cache_key = f'{text.casefold()}|{limit}'
    with _lock:
        cached = _search_cache.get(cache_key)
    if cached is not None:
        return cached
    data = _get(
        'database/search',
        {'q': text, 'type': 'release', 'per_page': str(limit)},
        token,
    )
    if data is None:
        return []
    results: list[dict[str, Any]] = []
    for row in data.get('results') or []:
        if not isinstance(row, dict):
            continue
        artist, album = _split_title(row.get('title') or '')
        genre = ', '.join([
            *(row.get('genre') or []),
            *(row.get('style') or []),
        ])
        year = str(row.get('year') or '')
        results.append({
            'source': 'discogs',
            'title': '',
            'artist': artist,
            'album': album,
            'album_artist': artist,
            'year': year[:4],
            'date': year,
            'genre': genre,
            'release_type': '',
            'release_id': str(row.get('id') or ''),
            'format': ', '.join(row.get('format') or []),
            'country': str(row.get('country') or ''),
        })
    with _lock:
        _search_cache[cache_key] = results
    logger.info('Discogs search {!r}: {} result(s)', text[:80], len(results))
    return results


def _parse_position(value: Any) -> tuple[int, int]:
    """Discogs positions: ``'3'``, ``'A2'``, ``'1-4'`` (disc-track)."""

    text = str(value or '').strip()
    if '-' in text:
        disc, _, track = text.partition('-')
        return max(1, int(re.sub(r'\D', '', disc) or 1)), int(
            re.sub(r'\D', '', track) or 0
        )
    digits = re.sub(r'\D', '', text)
    return 1, int(digits) if digits else 0


def release(release_id: str, *, token: str) -> Optional[dict[str, Any]]:
    """A release's metadata and ordered tracklist."""

    rid = str(release_id or '').strip()
    if not rid:
        return None
    with _lock:
        cached = _release_cache.get(rid)
    if cached is not None:
        return cached
    data = _get(f'releases/{quote(rid)}', {}, token)
    if data is None:
        return None
    tracks: list[dict[str, Any]] = []
    for row in data.get('tracklist') or []:
        if not isinstance(row, dict):
            continue
        if str(row.get('type_') or '') == 'heading':
            continue
        disc, position = _parse_position(row.get('position'))
        duration = str(row.get('duration') or '')
        seconds = 0.0
        parts = duration.split(':')
        if len(parts) == 2:
            try:
                seconds = int(parts[0]) * 60 + int(parts[1])
            except ValueError:
                seconds = 0.0
        tracks.append({
            'disc': disc,
            'position': position,
            'title': str(row.get('title') or ''),
            'duration': seconds,
        })
    artists = data.get('artists') or []
    artist = ' & '.join(
        str(a.get('name') or '').strip()
        for a in artists
        if isinstance(a, dict) and a.get('name')
    )
    year = str(data.get('year') or '')
    genre = ', '.join([
        *(data.get('genres') or []),
        *(data.get('styles') or []),
    ])
    result = {
        'source': 'discogs',
        'album': str(data.get('title') or ''),
        'album_artist': artist,
        'date': year,
        'year': year[:4],
        'genre': genre,
        'release_type': '',
        'track_total': len(tracks),
        'tracks': tracks,
    }
    with _lock:
        if len(_release_cache) > 1024:
            _release_cache.clear()
        _release_cache[rid] = result
    return result
