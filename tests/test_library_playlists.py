"""Resolving playlist tracks against the library and rebuilding M3Us."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from mutagen.id3 import ID3, TALB, TDRC, TIT2, TPE1, TPE2, TRCK

from downtify import library_playlists, monitor
from downtify.library_catalog import LibraryContext
from downtify.library_metadata_cache import LibraryMetadataCache
from downtify.playlist_tracks import (
    build_library_song_index,
    resolve_track_file,
)

_MP3_FRAME = b'\xff\xfb\x90\x00' + b'\x00' * 413


def _write_track(path: Path, *, title: str, artist: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_MP3_FRAME * 40)
    tags = ID3()
    tags.add(TIT2(encoding=3, text=title))
    tags.add(TPE1(encoding=3, text=artist))
    tags.add(TPE2(encoding=3, text=artist))
    tags.add(TALB(encoding=3, text='Album'))
    tags.add(TDRC(encoding=3, text='2020'))
    tags.add(TRCK(encoding=3, text='1'))
    tags.save(str(path), v2_version=4)
    return path


def _context(tmp_path: Path) -> LibraryContext:
    return LibraryContext(
        download_dir=tmp_path / 'downloads',
        metadata_cache=LibraryMetadataCache(tmp_path / 'lib.db'),
    )


class _StubDownloader:
    """The bits of Downloader playlist resolution touches."""

    organize_by_artist = False
    organize_by_album = False
    slskd_settings: dict[str, Any] = {}

    def __init__(self, download_dir: Path) -> None:
        self.download_dir = str(download_dir)

    @staticmethod
    def existing_filename_for(
        _song: dict[str, Any], subdir: Optional[str] = None
    ) -> Optional[str]:
        return None

    @staticmethod
    def find_existing_download(
        _song: dict[str, Any], subdir: Optional[str] = None
    ) -> Optional[str]:
        return None


class _StubMonitorDB:
    def __init__(self, watch: SimpleNamespace) -> None:
        self._watch = watch
        self.marked: list[tuple[int, str, str]] = []

    def list_playlists(self) -> list[SimpleNamespace]:
        return [self._watch]

    def mark_track_downloaded(
        self, playlist_id: int, track_id: str, filename: str
    ) -> None:
        self.marked.append((playlist_id, track_id, filename))


def test_resolve_track_file_falls_back_to_artist_and_title(
    tmp_path: Path,
) -> None:
    ctx = _context(tmp_path)
    _write_track(
        ctx.download_dir / 'Albums' / 'Deep Cut.mp3',
        title='Deep Cut',
        artist='The Band',
    )
    tag_index = build_library_song_index(ctx)
    downloader = _StubDownloader(ctx.download_dir)

    song = {'name': 'Deep Cut', 'artists': ['The Band'], 'song_id': 'yt123'}

    assert (
        resolve_track_file(song, downloader=downloader, tag_index=tag_index)
        == 'Albums/Deep Cut.mp3'
    )


def test_rebuild_playlist_m3u_includes_songs_already_on_disk(
    tmp_path: Path, monkeypatch: Any
) -> None:
    ctx = _context(tmp_path)
    _write_track(
        ctx.download_dir / 'Albums' / 'Older Song.mp3',
        title='Older Song',
        artist='The Band',
    )
    downloader = _StubDownloader(ctx.download_dir)
    watch = SimpleNamespace(
        id=1,
        name='My Mix',
        spotify_id='pl1',
        kind=monitor.KIND_PLAYLIST,
        source=monitor.SOURCE_SPOTIFY,
    )
    db = _StubMonitorDB(watch)
    tracks = [
        {'song_id': 'yt123', 'name': 'Older Song', 'artists': ['The Band']},
        {'song_id': 'yt999', 'name': 'Not Downloaded', 'artists': ['Nobody']},
    ]
    monkeypatch.setattr(
        monitor, 'fetch_playlist', lambda *_a, **_k: ('My Mix', tracks)
    )

    report = library_playlists.rebuild_playlist_m3us(
        ctx=ctx,
        settings={'generate_m3u': True},
        downloader=downloader,
        monitor_db=db,
    )

    assert report['playlists'] == 1
    assert report['rebuilt'] == 1
    assert report['tracks_linked'] == 1
    m3u_file = ctx.download_dir / 'My Mix' / 'My Mix.m3u'
    assert m3u_file.is_file()
    assert 'Older Song.mp3' in m3u_file.read_text(encoding='utf-8')
    assert 'Not Downloaded' not in m3u_file.read_text(encoding='utf-8')
    # The found track is remembered on its watch so the next sweep keeps it.
    assert db.marked == [(1, 'yt123', 'Albums/Older Song.mp3')]
