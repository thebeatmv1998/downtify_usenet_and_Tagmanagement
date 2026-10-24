"""Best-effort album lookup for tracks whose tags carry none.

Downloads without album metadata used to land in an ``unknown`` folder
even though Deezer or iTunes knows the release. This module asks those
two sources (in that order), caches every answer for the process lifetime
and treats placeholder values such as ``unknown`` as missing.

It deliberately has no Spotify or downloader imports, so both the
downloader and the album-merge tooling can use it without an import
cycle.
"""

from __future__ import annotations

import threading
from typing import Any, Optional

from loguru import logger

from . import deezer, itunes
from .library_catalog import is_blank_or_placeholder

_CACHE: dict[str, Optional[dict[str, Any]]] = {}
_LOCK = threading.Lock()
_CACHE_MAX = 4096


def _key(song: dict[str, Any]) -> str:
    artists = song.get('artists') or []
    artist = str(
        song.get('album_artist')
        or (artists[0] if artists else '')
        or song.get('artist')
        or ''
    ).strip()
    title = str(song.get('name') or song.get('title') or '').strip()
    if not artist or not title:
        return ''
    return f'{artist.casefold()}|{title.casefold()}'


def clear_cache() -> None:
    """Forget cached answers (used by tests)."""

    with _LOCK:
        _CACHE.clear()


def lookup_album(song: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The track's real album from Deezer/iTunes, or ``None``.

    Only called for songs whose own album tag is missing or a
    placeholder; a song that already has an album costs no request.
    """

    current = str(song.get('album_name') or '').strip()
    if current and not is_blank_or_placeholder(current):
        return None
    key = _key(song)
    if not key:
        return None
    with _LOCK:
        if key in _CACHE:
            cached = _CACHE[key]
            return dict(cached) if cached else None

    found: Optional[dict[str, Any]] = None
    for name, resolver in (
        ('Deezer', deezer.lookup_track_metadata),
        ('iTunes', itunes.lookup_track_metadata),
    ):
        try:
            data = resolver(song)
        except Exception:
            logger.opt(exception=True).debug(
                'Album lookup: {} failed for {!r}', name, song.get('name')
            )
            continue
        album = str((data or {}).get('album_name') or '').strip()
        if data and album and not is_blank_or_placeholder(album):
            found = dict(data)
            break

    with _LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.clear()
        _CACHE[key] = dict(found) if found else None
    return found
