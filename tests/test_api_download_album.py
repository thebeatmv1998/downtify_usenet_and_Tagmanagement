"""Tests for the whole-album download endpoint.

Regression coverage for the bug where albums were downloaded one track at
a time via ``/api/download/url``, which re-resolves each video's catalog
metadata independently and can drift to a different release than the one
the user actually picked (e.g. a "best of" compilation's tracks resolving
back to their original studio albums). ``/api/download/album`` must instead
download every track from the same, already-resolved tracklist.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from downtify import api
from downtify.api import _songs_for_album_download, download_album_endpoint

# The route functions take the request; these tests call them directly.
_REQUEST = Request({'type': 'http', 'headers': []})

ALBUM_URL = 'https://music.youtube.com/browse/MPREb_test123'


# ── _songs_for_album_download ─────────────────────────────────────────────────


def test_songs_for_album_download_rejects_non_album_url(monkeypatch):
    monkeypatch.setattr(
        api.providers, 'parse_youtube_url', lambda url: ('track', 'abc')
    )
    with pytest.raises(HTTPException) as exc_info:
        _songs_for_album_download('https://music.youtube.com/watch?v=abc')
    assert exc_info.value.status_code == 400


def test_songs_for_album_download_rejects_unrecognized_url(monkeypatch):
    monkeypatch.setattr(api.providers, 'parse_youtube_url', lambda url: None)
    with pytest.raises(HTTPException) as exc_info:
        _songs_for_album_download('https://example.com/not-youtube')
    assert exc_info.value.status_code == 400


def test_songs_for_album_download_rejects_empty_tracklist(monkeypatch):
    monkeypatch.setattr(
        api.providers,
        'parse_youtube_url',
        lambda url: ('album', 'MPREb_x'),
    )
    monkeypatch.setattr(
        api.providers, 'album_tracks_from_browse_id', lambda bid: []
    )
    with pytest.raises(HTTPException) as exc_info:
        _songs_for_album_download(ALBUM_URL)
    assert exc_info.value.status_code == 404


def test_songs_for_album_download_returns_resolved_tracks(monkeypatch):
    songs = [{'song_id': 'a'}, {'song_id': 'b'}]
    monkeypatch.setattr(
        api.providers,
        'parse_youtube_url',
        lambda url: ('album', 'MPREb_x'),
    )
    monkeypatch.setattr(
        api.providers, 'album_tracks_from_browse_id', lambda bid: songs
    )
    assert _songs_for_album_download(ALBUM_URL) == songs


# ── download_album_endpoint ────────────────────────────────────────────────────


def test_download_album_endpoint_requires_downloader(monkeypatch):
    monkeypatch.setattr(api.state, 'downloader', None)
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(download_album_endpoint(_REQUEST, url=ALBUM_URL))
    assert exc_info.value.status_code == 500


def test_download_album_endpoint_keeps_shared_album_metadata(monkeypatch):
    songs = [
        {'song_id': 'good-1', 'album_name': 'Monetine', 'name': 'Song 1'},
        {'song_id': 'good-2', 'album_name': 'Monetine', 'name': 'Song 2'},
        {'song_id': 'bad-1', 'album_name': 'Monetine', 'name': 'Song 3'},
    ]
    monkeypatch.setattr(api, '_songs_for_album_download', lambda url: songs)
    monkeypatch.setattr(api.state, 'downloader', object())
    monkeypatch.setattr(api.state, 'download_jobs', {})

    seen_songs = []

    async def fake_run_download(
        song, job_id, subdir=None, delay_seconds=0, **_kwargs
    ):
        seen_songs.append(song)
        if song['song_id'] == 'bad-1':
            raise RuntimeError('boom')
        return f'{song["song_id"]}.mp3'

    monkeypatch.setattr(api, '_run_download', fake_run_download)

    result = asyncio.run(download_album_endpoint(_REQUEST, url=ALBUM_URL))

    assert result == {'good-1': 'good-1.mp3', 'good-2': 'good-2.mp3'}
    # every track was downloaded using the exact, shared album metadata -
    # never re-resolved one video at a time (the bug this endpoint fixes)
    assert all(s['album_name'] == 'Monetine' for s in seen_songs)


def test_prefetch_only_fetches_repeated_albums_once(monkeypatch):
    calls: list[tuple] = []

    class _Downloader:
        usenet_settings = {'enabled': True}
        audio_providers = ['usenet']

        def existing_filename_for(self, _song, subdir=None):
            return None

    def _download(song, settings, **kwargs):
        calls.append((song, settings, kwargs))

    monkeypatch.setattr(api.state, 'downloader', _Downloader())
    monkeypatch.setattr(api, 'download_from_usenet', _download)
    songs = [
        {
            'song_id': 'a1',
            'album_name': 'Album A',
            'album_artist': 'Artist',
        },
        {
            'song_id': 'a2',
            'album_name': 'Album A',
            'album_artist': 'Artist',
        },
        {
            'song_id': 'b1',
            'album_name': 'Album B',
            'album_artist': 'Artist',
        },
    ]

    api._prefetch_usenet_albums(songs, False, None)

    assert len(calls) == 1
    assert calls[0][2]['album_only'] is True
    assert calls[0][2]['album_keys'] == ['artistalbuma']


def test_prefetch_skips_an_album_that_is_already_present(monkeypatch):
    calls: list[tuple] = []

    class _Downloader:
        usenet_settings = {'enabled': True}
        audio_providers = ['usenet']

        def existing_filename_for(self, _song, subdir=None):
            return 'Artist/Album/01 - Song.mp3'

    monkeypatch.setattr(api.state, 'downloader', _Downloader())
    monkeypatch.setattr(
        api, 'download_from_usenet', lambda *a, **k: calls.append((a, k))
    )
    songs = [
        {
            'song_id': 'a1',
            'album_name': 'Album A',
            'album_artist': 'Artist',
        },
        {
            'song_id': 'a2',
            'album_name': 'Album A',
            'album_artist': 'Artist',
        },
    ]

    api._prefetch_usenet_albums(songs, True, None)

    assert calls == []


def test_same_album_songs_get_one_album_artist():
    songs = [
        {'album_name': 'Album', 'artists': ['Artist']},
        {'album_name': 'Album', 'artists': ['Artist']},
    ]
    api._fill_album_artists(songs)
    assert [song['album_artist'] for song in songs] == ['Artist', 'Artist']

    mixed = [
        {'album_name': 'Hits', 'artists': ['One']},
        {'album_name': 'Hits', 'artists': ['Two']},
    ]
    api._fill_album_artists(mixed)
    assert all('album_artist' not in song for song in mixed)


def test_album_batch_marks_songs_for_one_usenet_release(monkeypatch):
    registered: list[dict] = []

    def _register(song, **_kwargs):
        registered.append(dict(song))
        return song['song_id']

    async def _broadcast(_message):
        return None

    class _Task:
        def add_done_callback(self, _callback):
            return None

    def _spawn(coro, **_kwargs):
        coro.close()
        return _Task()

    monkeypatch.setattr(api, '_register_job', _register)
    monkeypatch.setattr(api, 'spawn_task', _spawn)
    monkeypatch.setattr(api.state.connections, 'broadcast', _broadcast)
    songs = [
        {
            'song_id': '1',
            'album_name': 'Album',
            'album_artist': 'Artist',
            'artists': ['Artist'],
        }
    ]

    asyncio.run(
        api._submit_playlist_batch(songs, '', generate_m3u=False, album=True)
    )

    assert registered[0]['_usenet_prefer_album'] is True
    assert api._album_subdir_for_songs(songs) == 'Artist/Album'


def test_playlist_batch_reuses_an_album_only_when_it_recurs(monkeypatch):
    registered: list[dict] = []

    def _register(song, **_kwargs):
        registered.append(dict(song))
        return song['song_id']

    async def _broadcast(_message):
        return None

    class _Task:
        def add_done_callback(self, _callback):
            return None

    def _spawn(coro, **_kwargs):
        coro.close()
        return _Task()

    monkeypatch.setattr(api, '_register_job', _register)
    monkeypatch.setattr(api, 'spawn_task', _spawn)
    monkeypatch.setattr(api.state.connections, 'broadcast', _broadcast)
    songs = [
        {
            'song_id': 'a1',
            'album_name': 'Album A',
            'album_artist': 'Artist',
        },
        {
            'song_id': 'a2',
            'album_name': 'Album A',
            'album_artist': 'Artist',
        },
        {
            'song_id': 'b1',
            'album_name': 'Album B',
            'album_artist': 'Artist',
        },
    ]

    asyncio.run(
        api._submit_playlist_batch(songs, '', generate_m3u=False, album=False)
    )

    assert registered[0]['_usenet_prefer_album'] is True
    assert registered[1]['_usenet_prefer_album'] is True
    assert '_usenet_prefer_album' not in registered[2]


def test_download_album_endpoint_skips_songs_without_id(monkeypatch):
    monkeypatch.setattr(
        api, '_songs_for_album_download', lambda url: [{'name': 'no id'}]
    )
    monkeypatch.setattr(api.state, 'downloader', object())
    monkeypatch.setattr(api.state, 'download_jobs', {})

    async def fake_run_download(
        song, job_id, subdir=None, delay_seconds=0, **_kwargs
    ):
        raise AssertionError(
            'should not attempt to download a song with no id'
        )

    monkeypatch.setattr(api, '_run_download', fake_run_download)

    result = asyncio.run(download_album_endpoint(_REQUEST, url=ALBUM_URL))
    assert result == {}
