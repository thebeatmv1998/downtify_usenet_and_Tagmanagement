"""Deduplication before album prefetch and per-track source resolution."""

from __future__ import annotations

import asyncio

import pytest
from mutagen.id3 import ID3, TIT2, TPE1

from downtify import api
from downtify.downloader import Downloader
from downtify.track_index import resolve_existing_download
from tests.test_downloader_extended import _minimal_mp3


def tagged_file(root, relative, artist='Artist', title='Song'):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    _minimal_mp3(path)
    tags = ID3()
    tags.add(TIT2(encoding=3, text=title))
    tags.add(TPE1(encoding=3, text=artist))
    tags.save(str(path))
    return path


def song(title='Song'):
    return {
        'song_id': 'youtube-id',
        'name': title,
        'artists': ['Artist'],
        'album_name': 'Different edition',
        '_usenet_prefer_album': True,
    }


@pytest.mark.parametrize(
    'relative',
    [
        'Old album/01 - Song.mp3',
        'Singles/Imported audio.mp3',
        'Artist/Original edition/renamed.mp3',
    ],
)
def test_album_skips_tagged_song_with_different_name_format_and_layout(
    tmp_path, monkeypatch, relative
):
    tagged_file(tmp_path, relative)
    downloader = Downloader(
        tmp_path, audio_format='flac', overwrite_existing_files=True
    )
    monkeypatch.setattr(
        downloader,
        '_resolve_source',
        lambda *args: pytest.fail('must not search providers'),
    )
    assert (
        downloader.download(song(), subdir='Artist/Different edition')
        == relative
    )
    assert (
        resolve_existing_download(
            downloader, song(), subdir='Artist/Different edition'
        )[0]
        == relative
    )


def test_album_prefetch_skips_library_album_before_any_nzb_search(
    tmp_path, monkeypatch
):
    tagged_file(tmp_path, 'Old layout/01.mp3', title='First')
    tagged_file(tmp_path, 'Old layout/02.mp3', title='Second')
    downloader = Downloader(
        tmp_path, usenet_settings={'enabled': True}, audio_providers=['usenet']
    )
    monkeypatch.setattr(api.state, 'downloader', downloader)
    monkeypatch.setattr(api.state, 'track_index', None)
    monkeypatch.setattr(
        api,
        'download_from_usenet',
        lambda *args, **kwargs: pytest.fail(
            'existing album must not submit NZB'
        ),
    )
    api._prefetch_usenet_albums(
        [song('First'), song('Second')], True, 'Artist/Different edition'
    )


@pytest.mark.parametrize(
    ('artist', 'title'), [('Other artist', 'Song'), ('Artist', 'Song (Live)')]
)
def test_different_artist_or_live_recording_is_not_a_duplicate(
    tmp_path, artist, title
):
    tagged_file(tmp_path, 'Import/audio.mp3', artist=artist, title=title)
    assert Downloader(tmp_path).find_existing_download(song()) is None


def test_stale_metadata_does_not_hide_a_deleted_song(tmp_path):
    path = tagged_file(tmp_path, 'Import/audio.mp3')
    downloader = Downloader(tmp_path)
    assert downloader.find_existing_download(song())
    path.unlink()
    assert downloader.find_existing_download(song()) is None


def test_existing_slskd_tags_are_checked_before_downloading(tmp_path):
    source = tmp_path / 'soulseek'
    tagged_file(source, 'peer/random-name.mp3')
    downloader = Downloader(
        tmp_path / 'library', slskd_settings={'source_dir': str(source)}
    )
    assert (
        resolve_existing_download(downloader, song())[0]
        == 'slskd/peer/random-name.mp3'
    )


def test_metadata_cache_refreshes_after_tag_edits(tmp_path):
    path = tagged_file(tmp_path, 'Import/audio.mp3')
    downloader = Downloader(tmp_path)
    assert downloader.find_existing_download(song())
    tags = ID3(str(path))
    tags['TIT2'] = TIT2(encoding=3, text='Completely different title')
    tags.save(str(path))
    assert downloader.find_existing_download(song()) is None


def test_api_marks_existing_youtube_song_done_without_invoking_downloader(
    tmp_path, monkeypatch
):
    tagged_file(tmp_path, 'Import/audio.mp3')
    downloader = Downloader(tmp_path, overwrite_existing_files=True)
    monkeypatch.setattr(
        downloader,
        'download',
        lambda *args, **kwargs: pytest.fail('must skip download'),
    )
    monkeypatch.setattr(api.state, 'downloader', downloader)
    monkeypatch.setattr(api.state, 'download_jobs', {})
    monkeypatch.setattr(api.state, 'download_semaphore', None)
    monkeypatch.setattr(api.state, 'track_index', None)
    monkeypatch.setattr(api.state, 'loop', None)

    async def broadcast(*args):
        pass

    monkeypatch.setattr(api.state.connections, 'broadcast_job', broadcast)
    assert (
        asyncio.run(api._run_download(song(), 'youtube-id'))
        == 'Import/audio.mp3'
    )
    assert api.state.download_jobs['youtube-id']['status'] == 'done'


def test_complete_album_batch_reuses_existing_imports_without_provider_calls(
    tmp_path, monkeypatch
):
    tagged_file(tmp_path, 'Original/01.mp3', title='First')
    tagged_file(tmp_path, 'Original/02.mp3', title='Second')
    downloader = Downloader(
        tmp_path,
        overwrite_existing_files=True,
        audio_providers=['usenet'],
        usenet_settings={'enabled': True},
    )
    monkeypatch.setattr(
        downloader,
        'download',
        lambda *args, **kwargs: pytest.fail('no worker download needed'),
    )
    monkeypatch.setattr(
        api,
        'download_from_usenet',
        lambda *args, **kwargs: pytest.fail('no NZB needed'),
    )
    monkeypatch.setattr(api.state, 'downloader', downloader)
    monkeypatch.setattr(api.state, 'download_jobs', {})
    monkeypatch.setattr(api.state, 'download_semaphore', None)
    monkeypatch.setattr(api.state, 'track_index', None)
    monkeypatch.setattr(api.state, 'settings', {})
    monkeypatch.setattr(api.state, 'loop', None)

    async def broadcast(*args):
        pass

    monkeypatch.setattr(api.state.connections, 'broadcast_job', broadcast)
    songs = [
        {**song(title), 'song_id': title} for title in ['First', 'Second']
    ]
    ids = [api._register_job(track, status='queued') for track in songs]
    asyncio.run(api._process_batch(songs, ids, '', False, album=True))
    assert all(
        api.state.download_jobs[job_id]['status'] == 'done' for job_id in ids
    )


def test_existing_songs_are_done_while_missing_songs_remain_waiting(
    tmp_path, monkeypatch
):
    tagged_file(tmp_path, 'Import/existing.mp3', title='Existing')
    monkeypatch.setattr(api.state, 'downloader', Downloader(tmp_path))
    monkeypatch.setattr(api.state, 'download_jobs', {})
    monkeypatch.setattr(api.state, 'track_index', None)
    messages = []

    async def broadcast(users, message):
        messages.append(message)

    monkeypatch.setattr(api.state.connections, 'broadcast_job', broadcast)
    tracks = [
        {**song(title), 'song_id': title} for title in ['Existing', 'Missing']
    ]
    ids = [api._register_job(track, status='queued') for track in tracks]
    asyncio.run(api._mark_existing_queue_jobs(tracks, ids))
    assert api.state.download_jobs[ids[0]]['status'] == 'done'
    assert api.state.download_jobs[ids[0]]['progress'] == 100
    assert api.state.download_jobs[ids[0]]['filename'] == 'Import/existing.mp3'
    assert api.state.download_jobs[ids[1]]['status'] == 'queued'
    assert [message['status'] for message in messages] == ['done']
