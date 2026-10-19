"""Rebuild a library's playlist M3Us from what is already on disk.

A playlist is downloaded incrementally, and its M3U is written from the
tracks that download produced. Songs the library already had are skipped,
so they were missing from the M3U even though they are on disk — the
playlist looked half-empty in Jellyfin/Plex/Navidrome.

This module re-reads each known playlist's *full* track list from its
source and rewrites the M3U with every track that resolves to a file in
the library (see :mod:`downtify.playlist_tracks`). It also records the
found tracks on their watch so the next sweep doesn't lose them again.
It is exposed as a Library Upgrade action, not part of the per-track
scan: it works per playlist, not per file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from loguru import logger

from . import m3u
from .library_catalog import LibraryContext
from .library_paths import slskd_dir_from_downloader
from .playlist_tracks import build_library_song_index, resolve_track_file

ProgressFn = Callable[[str, int, int], None]


def _organize(downloader: Any) -> bool:
    return bool(
        getattr(downloader, 'organize_by_artist', False)
        or getattr(downloader, 'organize_by_album', False)
    )


def _write_m3u(
    ctx: LibraryContext,
    downloader: Any,
    name: str,
    entries: list[dict[str, Any]],
) -> Optional[Path]:
    if not entries:
        return None
    organize = _organize(downloader)
    path, _kept = m3u.write_m3u(
        Path(ctx.download_dir),
        name,
        entries,
        playlist_subdir=None if organize else m3u.sanitize_playlist_name(name),
        slskd_dir=slskd_dir_from_downloader(downloader),
    )
    return path


def _entry_for(song: dict[str, Any], filename: str) -> dict[str, Any]:
    return {
        'filename': filename,
        'title': song.get('name') or song.get('title') or '',
        'artist': ', '.join(song.get('artists') or [])
        or str(song.get('artist') or ''),
        'duration': song.get('duration') or 0,
    }


def rebuild_playlist_m3us(
    *,
    ctx: LibraryContext,
    settings: dict[str, Any],
    downloader: Any,
    monitor_db: Optional[Any] = None,
    playlist_catalog: Optional[Any] = None,
    track_index: Optional[Any] = None,
    progress: Optional[ProgressFn] = None,
) -> dict[str, Any]:
    """Rewrite every known playlist's M3U from the library.

    Returns a report: how many playlists were looked at and rebuilt, how
    many tracks were linked back in, and any that failed.
    """

    from . import monitor  # noqa: PLC0415  (avoid an import cycle)

    tag_index = build_library_song_index(ctx)
    sources = _collect_sources(monitor_db, playlist_catalog, monitor)
    details: list[dict[str, Any]] = []
    rebuilt = 0
    linked_total = 0

    for index, source in enumerate(sources, start=1):
        name = source['name']
        if progress is not None:
            progress(name, index, len(sources))
        try:
            _name, tracks = monitor.fetch_playlist(
                source['source'], source['id']
            )
        except Exception:
            logger.opt(exception=True).warning(
                'Playlist rebuild: fetch failed for {!r}', name
            )
            details.append({'name': name, 'error': 'fetch failed'})
            continue

        subdir = m3u.sanitize_playlist_name(name)
        entries: list[dict[str, Any]] = []
        linked = 0
        for song in tracks:
            filename = resolve_track_file(
                song,
                downloader=downloader,
                track_index=track_index,
                subdir=subdir,
                tag_index=tag_index,
            )
            if not filename:
                continue
            entries.append(_entry_for(song, filename))
            linked += 1
            _remember(
                song,
                filename,
                source,
                monitor_db=monitor_db,
                playlist_catalog=playlist_catalog,
                ctx=ctx,
                order=len(entries) - 1,
            )

        path = _write_m3u(ctx, downloader, name, entries)
        linked_total += linked
        if path is not None:
            rebuilt += 1
        details.append({
            'name': name,
            'tracks': len(tracks),
            'linked': linked,
            'm3u': str(path) if path is not None else '',
        })

    logger.info(
        'Playlist rebuild: {} playlist(s), {} rebuilt, {} track(s) linked',
        len(sources),
        rebuilt,
        linked_total,
    )
    return {
        'playlists': len(sources),
        'rebuilt': rebuilt,
        'tracks_linked': linked_total,
        'details': details,
    }


def _collect_sources(
    monitor_db: Optional[Any],
    playlist_catalog: Optional[Any],
    monitor_mod: Any,
) -> list[dict[str, Any]]:
    """Deduplicated ``{name, source, id, watch_id}`` playlists to rebuild."""

    sources: list[dict[str, Any]] = []
    seen: set[str] = set()

    if monitor_db is not None:
        for watch in monitor_db.list_playlists():
            if getattr(watch, 'kind', monitor_mod.KIND_PLAYLIST) != (
                monitor_mod.KIND_PLAYLIST
            ):
                continue
            source = getattr(watch, 'source', monitor_mod.SOURCE_SPOTIFY)
            if source not in {
                monitor_mod.SOURCE_SPOTIFY,
                monitor_mod.SOURCE_YOUTUBE_MUSIC,
            }:
                continue
            if watch.name in seen:
                continue
            seen.add(watch.name)
            sources.append({
                'name': watch.name,
                'source': source,
                'id': watch.spotify_id,
                'watch_id': watch.id,
            })

    if playlist_catalog is not None:
        for row in playlist_catalog.list_playlists_with_spotify_id():
            name = str(row.get('name') or '')
            if not name or name in seen:
                continue
            seen.add(name)
            sources.append({
                'name': name,
                'source': monitor_mod.SOURCE_SPOTIFY,
                'id': str(row.get('spotify_id') or ''),
                'watch_id': None,
            })
    return sources


def _remember(
    song: dict[str, Any],
    filename: str,
    source: dict[str, Any],
    *,
    monitor_db: Optional[Any],
    playlist_catalog: Optional[Any],
    ctx: LibraryContext,
    order: int,
) -> None:
    """Record a linked track on its watch and in the playlist catalog."""

    from .library_paths import locate_library_file  # noqa: PLC0415

    full = locate_library_file(
        filename, ctx.download_dir, ctx.slskd_dir, ctx.extra_dirs
    )
    if full is None:
        return
    try:
        if monitor_db is not None and source.get('watch_id') is not None:
            track_id = song.get('song_id') or song.get('id') or ''
            if track_id:
                monitor_db.mark_track_downloaded(
                    source['watch_id'], str(track_id), filename
                )
        if playlist_catalog is not None and source.get('source') == 'spotify':
            playlist_catalog.upsert_track(
                source['name'], song, filename, full, track_order=order
            )
    except Exception:
        logger.opt(exception=True).debug(
            'Playlist rebuild: could not remember {}', filename
        )
