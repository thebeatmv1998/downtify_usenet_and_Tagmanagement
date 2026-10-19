"""Find duplicate titles and albums already on disk.

A library built from playlists, albums and singles accumulates the same
song more than once — downloaded for two playlists, tagged slightly
differently, saved under two folders. This groups what is already in the
listing so the user can review and delete the copies, without walking the
filesystem again or touching anything by itself.

Matching uses the same loose key as "already downloaded" detection
(:func:`downtify.library_catalog.song_key`), so ``Song`` and
``Song (Remastered)`` are the same title while a genuine cover by another
artist, or a distinct ``Song (Live)``, is not.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .library_catalog import album_artist_of, song_key


def _file_row(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        'file': str(entry.get('file') or ''),
        'title': str(entry.get('title') or ''),
        'artist': str(entry.get('artist') or ''),
        'album': str(entry.get('album') or ''),
        'album_artist': str(entry.get('album_artist') or ''),
        'track_number': int(entry.get('track_number') or 0),
        'duration': float(entry.get('duration') or 0.0),
        'size': int(entry.get('size') or 0),
        'codec': str(entry.get('codec') or ''),
        'folder': str(Path(str(entry.get('file') or '')).parent.as_posix()),
    }


def find_duplicate_tracks(
    entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Groups of files that are the same song (artist + title)."""

    groups: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        key = song_key(
            str(entry.get('artist') or ''), str(entry.get('title') or '')
        )
        if key == '|':
            continue
        groups.setdefault(key, []).append(entry)

    found: list[dict[str, Any]] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        rows = sorted(members, key=lambda r: str(r.get('file') or ''))
        first = rows[0]
        found.append({
            'title': str(first.get('title') or ''),
            'artist': str(first.get('artist') or ''),
            'count': len(rows),
            'size': sum(int(r.get('size') or 0) for r in rows),
            'files': [_file_row(row) for row in rows],
        })
    found.sort(key=lambda group: (-group['count'], group['title'].casefold()))
    return found


def find_duplicate_albums(
    entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Albums that are stored twice or hold the same song more than once."""

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for entry in entries:
        album = str(entry.get('album') or '').strip()
        if not album:
            continue
        artist = album_artist_of(entry)
        if not artist:
            continue
        groups.setdefault((artist.casefold(), album.casefold()), []).append(
            entry
        )

    found: list[dict[str, Any]] = []
    for _key, members in groups.items():
        folders = {
            str(Path(str(r.get('file') or '')).parent.as_posix())
            for r in members
        }
        by_song: dict[str, list[dict[str, Any]]] = {}
        for row in members:
            key = song_key(
                str(row.get('artist') or ''), str(row.get('title') or '')
            )
            if key != '|':
                by_song.setdefault(key, []).append(row)
        duplicate_titles = sorted({
            str(group[0].get('title') or '')
            for group in by_song.values()
            if len(group) > 1
        })
        multiple_folders = len(folders) > 1
        if not multiple_folders and not duplicate_titles:
            continue
        rows = sorted(members, key=lambda r: str(r.get('file') or ''))
        reasons: list[str] = []
        if multiple_folders:
            reasons.append('multiple folders')
        if duplicate_titles:
            reasons.append('repeated song')
        found.append({
            'album': str(rows[0].get('album') or ''),
            'album_artist': album_artist_of(rows[0]),
            'track_count': len(rows),
            'folders': sorted(folders),
            'reasons': reasons,
            'duplicate_titles': duplicate_titles,
            'size': sum(int(r.get('size') or 0) for r in rows),
            'files': [_file_row(row) for row in rows],
        })
    found.sort(
        key=lambda group: (
            -len(group['duplicate_titles']),
            group['album'].casefold(),
        )
    )
    return found


def find_duplicates(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """``{tracks, albums, track_count, album_count}`` for a listing."""

    tracks = find_duplicate_tracks(entries)
    albums = find_duplicate_albums(entries)
    return {
        'tracks': tracks,
        'albums': albums,
        'track_count': len(tracks),
        'album_count': len(albums),
        'duplicate_files': sum(group['count'] - 1 for group in tracks),
        'reclaimable_bytes': sum(
            group['size'] - max(int(f['size']) for f in group['files'])
            for group in tracks
        ),
    }
