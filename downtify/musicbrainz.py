"""Search MusicBrainz for a release to fill a track's tags with.

MusicBrainz' web service is keyless but asks for two things in return: a
descriptive ``User-Agent`` and no more than one request a second. Both
are honoured here, so a tag-editor search never gets this server blocked.
"""

from __future__ import annotations

import os
import time
from threading import Lock
from typing import Any, Optional
from urllib.parse import quote

import httpx
from loguru import logger

_BASE = 'https://musicbrainz.org/ws/2'
_TIMEOUT = 15
#: MusicBrainz' documented limit is one request per second; a little
#: headroom keeps a burst from being throttled.
_MIN_INTERVAL = 1.1
_DEFAULT_UA = (
    'Downtify/3.4 (self-hosted music library; '
    '+https://github.com/henriquesebastiao/downtify)'
)

_lock = Lock()
_last_request = 0.0
_cooldown_until = 0.0
_search_cache: dict[str, list[dict[str, Any]]] = {}
_release_cache: dict[str, dict[str, Any]] = {}


def _user_agent() -> str:
    return os.getenv('DOWNTIFY_MUSICBRAINZ_UA', '').strip() or _DEFAULT_UA


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


def _get(path: str, params: dict[str, str]) -> Optional[dict[str, Any]]:
    global _cooldown_until
    _rate_wait()
    url = f'{_BASE}/{path}'
    try:
        resp = httpx.get(
            url,
            params=params,
            headers={
                'User-Agent': _user_agent(),
                'Accept': 'application/json',
            },
            timeout=_TIMEOUT,
        )
    except Exception:
        logger.opt(exception=True).debug('MusicBrainz request failed: {}', url)
        return None
    if resp.status_code in {429, 503}:
        with _lock:
            _cooldown_until = time.monotonic() + 5.0
        logger.warning('MusicBrainz throttled (HTTP {})', resp.status_code)
        return None
    try:
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        logger.opt(exception=True).debug('MusicBrainz response unreadable')
        return None
    return data if isinstance(data, dict) else None


def _artist_name(credits: Any) -> str:
    """The joined artist credit of a release/recording."""

    if not isinstance(credits, list):
        return ''
    names = [
        str(c.get('name') or (c.get('artist') or {}).get('name') or '').strip()
        for c in credits
        if isinstance(c, dict)
    ]
    return ' & '.join(name for name in names if name)


def _release_type(release: dict[str, Any]) -> str:
    group = release.get('release-group') or {}
    return str(group.get('primary-type') or '').strip().lower()


def search_recordings(query: str, *, limit: int = 10) -> list[dict[str, Any]]:
    """Recording matches for *query*, each with its most likely release."""

    text = str(query or '').strip()
    if not text:
        return []
    cache_key = f'{text.casefold()}|{limit}'
    with _lock:
        cached = _search_cache.get(cache_key)
    if cached is not None:
        return cached
    data = _get(
        'recording', {'query': text, 'fmt': 'json', 'limit': str(limit)}
    )
    if data is None:
        return []
    results: list[dict[str, Any]] = []
    for recording in data.get('recordings') or []:
        if not isinstance(recording, dict):
            continue
        releases = [
            r for r in recording.get('releases') or [] if isinstance(r, dict)
        ]
        if not releases:
            continue
        # Prefer a full album over a single/compilation when one is offered.
        releases.sort(key=lambda r: _release_type(r) != 'album')
        release = releases[0]
        date = str(release.get('date') or '')
        results.append({
            'source': 'musicbrainz',
            'title': str(recording.get('title') or ''),
            'artist': _artist_name(recording.get('artist-credit')),
            'album': str(release.get('title') or ''),
            'album_artist': _artist_name(release.get('artist-credit'))
            or _artist_name(recording.get('artist-credit')),
            'year': date[:4],
            'date': date,
            'genre': '',
            'release_type': _release_type(release),
            'release_id': str(release.get('id') or ''),
            'mbid': str(recording.get('id') or ''),
        })
    with _lock:
        _search_cache[cache_key] = results
    logger.info(
        'MusicBrainz search {!r}: {} result(s)', text[:80], len(results)
    )
    return results


def release_tracklist(release_id: str) -> Optional[dict[str, Any]]:
    """An album's metadata and ordered tracklist, for filling a track."""

    rid = str(release_id or '').strip()
    if not rid:
        return None
    with _lock:
        cached = _release_cache.get(rid)
    if cached is not None:
        return cached
    data = _get(
        f'release/{quote(rid)}',
        {'inc': 'recordings+artist-credits', 'fmt': 'json'},
    )
    if data is None:
        return None
    tracks: list[dict[str, Any]] = []
    for medium in data.get('media') or []:
        if not isinstance(medium, dict):
            continue
        disc = int(medium.get('position') or 1)
        for track in medium.get('tracks') or []:
            if not isinstance(track, dict):
                continue
            length = track.get('length')
            tracks.append({
                'disc': disc,
                'position': int(track.get('position') or 0),
                'title': str(track.get('title') or ''),
                'duration': round(float(length) / 1000.0, 2)
                if isinstance(length, (int, float))
                else 0.0,
                'recording_id': str(
                    (track.get('recording') or {}).get('id') or ''
                ),
            })
    date = str(data.get('date') or '')
    result = {
        'source': 'musicbrainz',
        'album': str(data.get('title') or ''),
        'album_artist': _artist_name(data.get('artist-credit')),
        'date': date,
        'year': date[:4],
        'genre': '',
        'release_type': _release_type(data),
        'track_total': len(tracks),
        'tracks': tracks,
    }
    with _lock:
        if len(_release_cache) > 1024:
            _release_cache.clear()
        _release_cache[rid] = result
    return result
