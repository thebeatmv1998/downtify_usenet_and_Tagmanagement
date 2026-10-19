"""Match a playlist's tracks to files already in the library.

Downtify downloads a playlist incrementally: a track the library already
holds is skipped, so an M3U built from the download that just ran would
list only the newly fetched songs. These helpers resolve a playlist's
*full* track list against the library — by Spotify id, by the file the
output template would name, or by an artist/title tag match — so every
track that is already there lands in the M3U, whatever it was downloaded
for (another playlist, an album, a single).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .library_catalog import LibraryContext, list_library_entries, song_key
from .library_paths import locate_library_file, slskd_dir_from_downloader
from .track_index import normalize_spotify_track_id


def build_library_song_index(ctx: LibraryContext) -> dict[str, str]:
    """``artist|title`` (folded) → library path, from the library listing."""

    index: dict[str, str] = {}
    for entry in list_library_entries(ctx):
        key = song_key(entry.get('artist') or '', entry.get('title') or '')
        if key == '|':
            continue
        stored = str(entry.get('file') or '')
        if stored and key not in index:
            index[key] = stored
    return index


def song_lookup_key(song: dict[str, Any]) -> str:
    """The ``artist|title`` key :func:`build_library_song_index` uses."""

    artists = song.get('artists')
    if isinstance(artists, list) and artists:
        artist = artists[0]
    else:
        artist = song.get('artist') or song.get('subtitle') or ''
    title = song.get('name') or song.get('title') or ''
    return song_key(str(artist or ''), str(title or ''))


def resolve_track_file(
    song: dict[str, Any],
    *,
    downloader: Any,
    track_index: Optional[Any] = None,
    subdir: Optional[str] = None,
    tag_index: Optional[dict[str, str]] = None,
) -> Optional[str]:
    """A library path already holding *song*, or ``None``.

    Tried in order of certainty: the Spotify id the track index knows,
    the path the output template names, then a whole-library artist/title
    match. The last one is what picks up a song downloaded for a
    different playlist or as part of an album.
    """

    download_dir = Path(downloader.download_dir)
    slskd_dir = slskd_dir_from_downloader(downloader)

    def _exists(stored: Optional[str]) -> Optional[str]:
        if not stored:
            return None
        if locate_library_file(stored, download_dir, slskd_dir):
            return stored
        return None

    if track_index is not None:
        tid = normalize_spotify_track_id(song)
        if tid:
            hit = _exists(track_index.lookup(tid))
            if hit:
                return hit

    hit = _exists(downloader.existing_filename_for(song, subdir=subdir))
    if hit:
        return hit

    if tag_index:
        key = song_lookup_key(song)
        if key != '|':
            hit = _exists(tag_index.get(key))
            if hit:
                return hit

    # No index to consult: fall back to a whole-library filename scan.
    if tag_index is None:
        found = downloader.find_existing_download(song, subdir=subdir)
        if found:
            return _exists(found)
    return None


def resolve_playlist_tracks(
    tracks: list[dict[str, Any]],
    *,
    downloader: Any,
    track_index: Optional[Any] = None,
    subdir: Optional[str] = None,
    tag_index: Optional[dict[str, str]] = None,
) -> list[tuple[dict[str, Any], str]]:
    """``(song, library path)`` for every track of *tracks* on disk."""

    found: list[tuple[dict[str, Any], str]] = []
    for song in tracks:
        filename = resolve_track_file(
            song,
            downloader=downloader,
            track_index=track_index,
            subdir=subdir,
            tag_index=tag_index,
        )
        if filename:
            found.append((song, filename))
    return found
