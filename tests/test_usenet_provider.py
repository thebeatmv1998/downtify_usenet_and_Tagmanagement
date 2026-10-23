"""Newznab parsing/scoring and the SABnzbd download flow."""

from __future__ import annotations

from typing import Any

import pytest

import downtify.usenet_provider as usenet
from downtify.usenet_provider import (
    SabnzbdClient,
    _rank_usenet_candidates,
    _score_usenet_candidate,
    _usenet_search_queries,
    download_from_usenet,
    parse_newznab_xml,
)

_SONG = {
    'name': 'Track',
    'artists': ['Artist'],
    'album_name': 'Album',
    'duration': 200_000,
}

_NEWZNAB_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
 xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">
 <channel>
  <item>
   <title>Artist - Track 2023 FLAC</title>
   <guid>https://idx.example/getnzb/aaa</guid>
   <link>https://idx.example/getnzb/aaa</link>
   <newznab:attr name="size" value="25000000"/>
   <newznab:attr name="grabs" value="12"/>
   <newznab:attr name="password" value="0"/>
   <newznab:attr name="duration" value="201"/>
  </item>
  <item>
   <title>Artist - Track (password protected)</title>
   <guid>https://idx.example/getnzb/bbb</guid>
   <newznab:attr name="password" value="1"/>
  </item>
  <item>
   <title>Size From Element</title>
   <guid>https://idx.example/getnzb/ccc</guid>
   <size>12345</size>
  </item>
 </channel>
</rss>
"""


def test_parse_newznab_xml_extracts_items():
    rows = parse_newznab_xml(_NEWZNAB_XML, 'idx')
    assert len(rows) == 3
    first = rows[0]
    assert first['indexer'] == 'idx'
    assert first['title'] == 'Artist - Track 2023 FLAC'
    assert first['nzb_url'] == 'https://idx.example/getnzb/aaa'
    assert first['size'] == 25_000_000
    assert first['grabs'] == 12
    assert first['passworded'] == 0
    assert first['duration'] == 201
    # <size> element works when the newznab attr is missing.
    assert rows[2]['size'] == 12345


def test_parse_newznab_xml_bad_xml_returns_empty():
    assert parse_newznab_xml(b'not xml', 'idx') == []


def test_usenet_search_queries_variants():
    queries = _usenet_search_queries({
        'artists': ['4D4M'],
        'name': 'Loud Enough - Radio Mix',
        'album_name': 'Album',
    })
    assert queries[0] == '4D4M Loud Enough'
    assert 'Loud Enough 4D4M' in queries
    assert '4D4M Album' in queries
    assert 'Loud Enough' in queries


def test_score_good_release():
    release = {
        'title': 'Artist - Track 2023 FLAC',
        'size': 25_000_000,
        'grabs': 12,
        'passworded': 0,
        'duration': 201,
    }
    scored = _score_usenet_candidate(
        _SONG, release, target_duration=200, settings={}
    )
    assert scored is not None
    score, reasons, _kind = scored
    assert score >= 5
    assert 'ext_flac' in reasons
    assert 'grabs' in reasons


def test_score_excludes_passworded_attr():
    release = {
        'title': 'Artist - Track FLAC',
        'passworded': 1,
    }
    assert _score_usenet_candidate(_SONG, release, target_duration=200) is None


def test_score_excludes_password_keyword_in_title():
    release = {'title': 'Artist - Track FLAC password', 'passworded': 0}
    assert _score_usenet_candidate(_SONG, release, target_duration=200) is None


def test_score_extension_bonus_prefers_flac():
    base = {'passworded': 0, 'size': 25_000_000, 'duration': 200}
    flac = _score_usenet_candidate(
        _SONG,
        {**base, 'title': 'Artist - Track 2023 FLAC'},
        target_duration=200,
        settings={},
    )
    plain = _score_usenet_candidate(
        _SONG,
        {**base, 'title': 'Artist - Track 2023'},
        target_duration=200,
        settings={},
    )
    assert flac is not None
    assert plain is not None
    assert flac[0] > plain[0]


def test_score_rejects_implausible_size():
    release = {
        'title': 'Artist - Track FLAC',
        'passworded': 0,
        'size': 100_000,  # way too small for a 200 s track
        'duration': 200,
    }
    assert _score_usenet_candidate(_SONG, release, target_duration=200) is None


def test_rank_usenet_candidates_orders_by_score_then_grabs():
    releases = [
        {'title': 'Artist - Track 2023', 'grabs': 50, 'passworded': 0},
        {'title': 'Artist - Track 2023 FLAC', 'grabs': 1, 'passworded': 0},
    ]
    ranked = _rank_usenet_candidates(_SONG, releases, {})
    assert ranked[0]['title'] == 'Artist - Track 2023 FLAC'


def _settings(tmp_path, **overrides) -> dict[str, Any]:
    settings = {
        'enabled': True,
        'indexers': [
            {
                'name': 'idx',
                'base_url': 'http://idx.example',
                'api_key': 'key',
                'enabled': True,
            }
        ],
        'sabnzbd': {
            'base_url': 'http://sab.example',
            'api_key': 'key',
            'category': 'music',
            'complete_dir': str(tmp_path),
        },
        'poll_interval_seconds': 1,
        'poll_max_attempts': 3,
        'cleanup': False,
    }
    settings.update(overrides)
    return settings


def _release(title: str = 'Artist - Track 2023 FLAC') -> dict[str, Any]:
    return {
        'indexer': 'idx',
        'title': title,
        'nzb_url': 'https://idx.example/getnzb/aaa',
        'size': 25_000_000,
        'grabs': 12,
        'passworded': 0,
        'duration': 201,
    }


def _mock_sabnzbd(monkeypatch, history_slot, queued: list):
    monkeypatch.setattr(usenet.time, 'sleep', lambda s: None)
    monkeypatch.setattr(
        SabnzbdClient, 'add_url', lambda self, url, name: 'nzo1'
    )
    monkeypatch.setattr(
        SabnzbdClient,
        'queue_slot',
        lambda self, nzo: {'nzo_id': nzo, 'percentage': 50},
    )
    monkeypatch.setattr(
        SabnzbdClient, 'history_slot', lambda self, nzo: history_slot
    )
    monkeypatch.setattr(
        SabnzbdClient,
        'delete_history',
        lambda self, nzo: queued.append(nzo),
    )


def test_download_flow_queue_history_file(tmp_path, monkeypatch):
    release_dir = tmp_path / 'Artist - Track 2023 FLAC'
    release_dir.mkdir()
    audio = release_dir / 'Artist - Track.flac'
    audio.write_bytes(b'\xff\xfb' + b'\x00' * 4096)

    monkeypatch.setattr(
        usenet, '_search_indexers', lambda indexers, query: [_release()]
    )
    _mock_sabnzbd(
        monkeypatch,
        {
            'nzo_id': 'nzo1',
            'status': 'Completed',
            'storage': str(release_dir),
        },
        queued=[],
    )
    monkeypatch.setattr(
        usenet, 'verify_downloaded_file_matches_spotify', lambda p, s: True
    )

    found = download_from_usenet(dict(_SONG), _settings(tmp_path))
    assert found == audio


def test_download_flow_returns_none_when_sabnzbd_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(
        usenet, '_search_indexers', lambda indexers, query: [_release()]
    )
    _mock_sabnzbd(
        monkeypatch,
        {'nzo_id': 'nzo1', 'status': 'Failed', 'storage': ''},
        queued=[],
    )
    assert download_from_usenet(dict(_SONG), _settings(tmp_path)) is None


def test_verify_failure_tries_next_candidate(tmp_path, monkeypatch):
    good = tmp_path / 'good.flac'
    good.write_bytes(b'\xff\xfb' + b'\x00' * 4096)
    bad = tmp_path / 'bad.flac'
    bad.write_bytes(b'\xff\xfb' + b'\x00' * 4096)

    releases = [
        _release('Artist - Track 2023 FLAC'),
        _release('Artist - Track 2023 MP3 320'),
    ]
    monkeypatch.setattr(
        usenet, '_search_indexers', lambda indexers, query: releases
    )
    files = [bad, good]
    monkeypatch.setattr(
        usenet, '_find_completed_file', lambda *a, **k: files.pop(0)
    )
    verify_calls = []

    def _verify(path, song):
        verify_calls.append(path)
        return path == good

    monkeypatch.setattr(
        usenet, 'verify_downloaded_file_matches_spotify', _verify
    )
    _mock_sabnzbd(
        monkeypatch,
        {'nzo_id': 'nzo1', 'status': 'Completed', 'storage': ''},
        queued=[],
    )

    found = download_from_usenet(dict(_SONG), _settings(tmp_path))
    assert found == good
    assert verify_calls == [bad, good]
    assert not bad.exists()


def test_download_flow_all_indexers_empty_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(usenet, '_search_indexers', lambda indexers, query: [])
    assert download_from_usenet(dict(_SONG), _settings(tmp_path)) is None


def test_download_from_usenet_disabled_returns_none(tmp_path):
    settings = _settings(tmp_path, enabled=False)
    assert download_from_usenet(dict(_SONG), settings) is None


def test_download_from_usenet_without_sabnzbd_returns_none(tmp_path):
    settings = _settings(tmp_path)
    settings['sabnzbd'] = {'base_url': '', 'api_key': ''}
    assert download_from_usenet(dict(_SONG), settings) is None


def test_download_from_usenet_without_enabled_indexer_returns_none(tmp_path):
    settings = _settings(tmp_path)
    settings['indexers'] = [
        {
            'name': 'idx',
            'base_url': 'http://idx.example',
            'api_key': 'key',
            'enabled': False,
        }
    ]
    assert download_from_usenet(dict(_SONG), settings) is None


def test_cleanup_schedules_history_delete(tmp_path, monkeypatch):
    audio = tmp_path / 'Artist - Track.flac'
    audio.write_bytes(b'\xff\xfb' + b'\x00' * 4096)

    monkeypatch.setattr(
        usenet, '_search_indexers', lambda indexers, query: [_release()]
    )
    _mock_sabnzbd(
        monkeypatch,
        {'nzo_id': 'nzo1', 'status': 'Completed', 'storage': str(tmp_path)},
        queued=[],
    )
    monkeypatch.setattr(
        usenet, 'verify_downloaded_file_matches_spotify', lambda p, s: True
    )
    timers = []
    monkeypatch.setattr(
        usenet,
        '_schedule_cleanup',
        lambda client, nzo_id: timers.append(nzo_id),
    )

    settings = _settings(tmp_path, cleanup=True)
    found = download_from_usenet(dict(_SONG), settings)
    assert found == audio
    assert len(timers) == 1


def test_download_timeout_deadline_aborts_before_search(tmp_path, monkeypatch):
    settings = _settings(tmp_path, download_timeout_seconds=60)
    searched: list[int] = []
    monkeypatch.setattr(
        usenet,
        '_search_indexers',
        lambda indexers, query: searched.append(1) or [],
    )
    # A deadline in the past must abort before any indexer is contacted.
    monkeypatch.setattr(usenet, '_usenet_deadline', lambda s: 0.0)
    assert download_from_usenet(dict(_SONG), settings) is None
    assert not searched


def test_wait_for_sabnzbd_stops_when_deadline_passed(tmp_path, monkeypatch):
    _mock_sabnzbd(
        monkeypatch,
        {'nzo_id': 'nzo1', 'status': 'Downloading', 'percentage': 10},
        queued=[],
    )
    client = SabnzbdClient(_settings(tmp_path))
    assert (
        usenet._wait_for_sabnzbd(
            client, 'nzo1', _settings(tmp_path), deadline=0.0
        )
        is None
    )


def test_cancel_removes_the_sabnzbd_job_it_queued(tmp_path, monkeypatch):
    monkeypatch.setattr(
        usenet, '_search_indexers', lambda indexers, query: [_release()]
    )
    _mock_sabnzbd(
        monkeypatch,
        {'nzo_id': 'nzo1', 'status': 'Completed', 'storage': ''},
        queued=[],
    )
    cancelled: list[str] = []

    def _wait(*args, **kwargs):
        raise usenet.DownloadCancelled()

    monkeypatch.setattr(usenet, '_wait_for_sabnzbd', _wait)
    monkeypatch.setattr(
        SabnzbdClient, 'cancel_job', lambda self, nzo: cancelled.append(nzo)
    )

    with pytest.raises(usenet.DownloadCancelled):
        download_from_usenet(dict(_SONG), _settings(tmp_path))
    assert cancelled == ['nzo1']


def test_sabnzbd_cancel_job_clears_queue_and_history(tmp_path):
    client = SabnzbdClient(_settings(tmp_path))
    calls: list[dict[str, Any]] = []
    client._api = lambda params: calls.append(params) or {}  # type: ignore[method-assign]

    client.cancel_job('nzo1')

    assert [call['mode'] for call in calls] == ['queue', 'history']
    assert all(call['name'] == 'delete' for call in calls)
    assert all(call['value'] == 'nzo1' for call in calls)


def test_stop_category_removes_old_jobs_but_not_other_categories(tmp_path):
    client = SabnzbdClient(_settings(tmp_path))
    calls = []

    def request(params):
        calls.append(params)
        if params.get('name') == 'delete':
            return {'status': True}
        return {
            'queue': {
                'slots': [
                    {'nzo_id': 'old-music-job', 'cat': 'music'},
                    {'nzo_id': 'sonarr-job', 'cat': 'tv'},
                ]
            }
        }

    client._api = request
    assert client.cancel_category_queue() == {'removed': 1, 'errors': []}
    assert calls[1]['value'] == 'old-music-job'
    assert len(calls) == 2


def test_stop_category_reports_sabnzbd_rejection(tmp_path):
    client = SabnzbdClient(_settings(tmp_path))

    def request(params):
        if params.get('name') == 'delete':
            return {'status': False, 'error': 'Access denied'}
        return {'queue': {'slots': [{'nzo_id': 'job', 'cat': 'music'}]}}

    client._api = request
    assert client.cancel_category_queue() == {
        'removed': 0,
        'errors': ['Access denied'],
    }


def test_newznab_client_falls_back_to_prowlarr_endpoint(monkeypatch):
    calls: list[str] = []

    class FakeResp:
        def __init__(self, status):
            self.status_code = status
            self.content = b''

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f'HTTP {self.status_code}')

    def fake_get(url, **kwargs):
        calls.append(url)
        # Classic /api missing (Prowlarr), /api/v1/search answers.
        return FakeResp(404 if url.endswith('/api') else 200)

    monkeypatch.setattr(usenet.httpx, 'get', fake_get)
    client = usenet.NewznabClient({
        'name': 'prowlarr',
        'base_url': 'http://prowlarr:9696',
        'api_key': 'k',
    })
    resp = client._get({'t': 'search'})
    assert resp.status_code == 200
    assert calls == [
        'http://prowlarr:9696/api',
        'http://prowlarr:9696/api/v1/search',
    ]


def test_newznab_client_explicit_endpoint_used_verbatim(monkeypatch):
    calls: list[str] = []

    class FakeResp:
        status_code = 200
        content = b''

        def raise_for_status(self):
            return None

    monkeypatch.setattr(
        usenet.httpx, 'get', lambda url, **kw: calls.append(url) or FakeResp()
    )
    client = usenet.NewznabClient({
        'name': 'prowlarr',
        'base_url': 'http://prowlarr:9696/api/v1/search',
        'api_key': 'k',
    })
    assert client.endpoints() == ['http://prowlarr:9696/api/v1/search']
    client._get({'t': 'search'})
    assert calls == ['http://prowlarr:9696/api/v1/search']


def test_parse_prowlarr_results_keeps_usenet_only():
    rows = usenet.parse_prowlarr_results(
        [
            {
                'protocol': 'usenet',
                'indexer': 'NZBGeek',
                'title': 'Artist - Track FLAC',
                'downloadUrl': '/1/download?token=abc',
                'size': 25_000_000,
            },
            {
                'protocol': 'torrent',
                'indexer': 'SomeTracker',
                'title': 'Artist - Track FLAC',
                'downloadUrl': '/9/download?token=def',
                'size': 25_000_000,
            },
            {'protocol': 'usenet', 'title': 'no url'},
        ],
        'prowlarr',
    )
    assert len(rows) == 1
    row = rows[0]
    assert row['indexer'] == 'NZBGeek'
    assert row['nzb_url'] == '/1/download?token=abc'
    assert row['size'] == 25_000_000
    assert row['passworded'] == 0


def test_parse_prowlarr_results_bad_payload_returns_empty():
    assert usenet.parse_prowlarr_results('nope', 'prowlarr') == []
    assert usenet.parse_prowlarr_results(None, 'prowlarr') == []


def test_prowlarr_client_uses_native_endpoint_and_header(monkeypatch):
    calls: list[tuple[str, dict, dict]] = []

    class FakeResp:
        status_code = 200
        content = b'nzb-bytes'

        def raise_for_status(self):
            return None

        def json(self):
            return [
                {
                    'protocol': 'usenet',
                    'indexer': 'Geek',
                    'title': 'Artist - Track FLAC',
                    'downloadUrl': 'http://prowlarr:9696/1/download?token=x',
                    'size': 25_000_000,
                }
            ]

    def fake_get(url, params=None, headers=None, **kwargs):
        if isinstance(params, dict):
            params = list(params.items())
        calls.append((url, params or [], headers or {}))
        return FakeResp()

    monkeypatch.setattr(usenet.httpx, 'get', fake_get)
    client = usenet.NewznabClient({
        'name': 'Prowlarr',
        'base_url': 'http://prowlarr:9696',
        'api_key': 'pk',
        'type': 'prowlarr',
    })
    rows = client.search('artist track')  # noqa: F841
    assert calls == [
        (
            'http://prowlarr:9696/api/v1/search',
            [
                ('query', 'artist track'),
                ('categories', '3000'),
                ('categories', '3010'),
                ('apikey', 'pk'),
                ('limit', '100'),
            ],
            {'X-Api-Key': 'pk'},
        )
    ]
    assert rows[0]['title'] == 'Artist - Track FLAC'
    # The proxied download URL is fetched with the API key both as header
    # and query param (Prowlarr's download route accepts either).
    assert client.fetch_nzb(rows[0]['nzb_url']) == b'nzb-bytes'
    assert calls[-1][0] == 'http://prowlarr:9696/1/download?token=x'
    assert calls[-1][2] == {'X-Api-Key': 'pk'}
    assert calls[-1][1] == [('apikey', 'pk')]


def test_configured_bitrate_bounds_filter_releases():
    song = dict(_SONG)

    def _release(size):
        return {
            'title': 'Artist - Track',
            'size': size,
            'duration': 200,
            'passworded': 0,
            'grabs': 0,
        }

    # 120 kbps implied bitrate: within the default plausibility window...
    assert (
        usenet._score_usenet_candidate(
            song, _release(3_000_000), target_duration=200, settings={}
        )
        is not None
    )
    # ...but rejected when the user demands at least 320 kbps...
    assert (
        usenet._score_usenet_candidate(
            song,
            _release(3_000_000),
            target_duration=200,
            settings={'min_bitrate': 320},
        )
        is None
    )
    # ...or caps the maximum at 100 kbps.
    assert (
        usenet._score_usenet_candidate(
            song,
            _release(3_000_000),
            target_duration=200,
            settings={'max_bitrate': 100},
        )
        is None
    )


_ALBUM_RELEASE = {
    'indexer': 'idx',
    'title': 'Artist - Album 2023 FLAC',
    'nzb_url': 'https://idx.example/getnzb/album',
    'size': 300_000_000,
    'grabs': 20,
    'passworded': 0,
    'duration': 0,
}


def test_album_release_rejected_without_prefer_albums():
    # Default: album-only releases don't match a track search at all.
    assert (
        _score_usenet_candidate(
            _SONG, _ALBUM_RELEASE, target_duration=200, settings={}
        )
        is None
    )


def test_album_release_scores_with_prefer_albums():
    settings = {'prefer_albums': True}
    scored = _score_usenet_candidate(
        _SONG, _ALBUM_RELEASE, target_duration=200, settings=settings
    )
    assert scored is not None
    score, reasons, kind = scored
    assert kind == 'album'
    assert 'album' in reasons
    ranked = _rank_usenet_candidates(_SONG, [_ALBUM_RELEASE], settings)
    assert ranked[0]['match_kind'] == 'album'


def test_album_download_intent_overrides_the_setting():
    settings = {'prefer_albums': False}
    assert _rank_usenet_candidates(_SONG, [_ALBUM_RELEASE], settings) == []
    ranked = _rank_usenet_candidates(
        {**_SONG, '_usenet_prefer_album': True},
        [_ALBUM_RELEASE],
        settings,
    )
    assert ranked[0]['match_kind'] == 'album'


def test_album_cache_key_uses_album_artist_for_featured_tracks():
    main = {
        'name': 'Track',
        'artists': ['Main Artist'],
        'album_artist': 'Main Artist',
        'album_name': 'Album',
    }
    featured = {
        'name': 'Other Track',
        'artists': ['Featured Artist'],
        'album_artist': 'Main Artist',
        'album_name': 'Album',
    }
    assert usenet._album_cache_key(main) == usenet._album_cache_key(featured)


def test_album_releases_rank_ahead_of_tracks_when_preferred():
    settings = {'prefer_albums': True}
    ranked = _rank_usenet_candidates(
        _SONG,
        [_release(), _ALBUM_RELEASE],
        settings,
    )
    assert ranked[0]['match_kind'] == 'album'
    assert ranked[1]['match_kind'] == 'track'


def _album_folder(tmp_path):
    """An SABnzbd-style completed album folder with tagged tracks."""
    from mutagen.id3 import ID3, TIT2, TPE1

    from tests.test_downloader_extended import _minimal_mp3

    album = tmp_path / 'Artist - Album 2023 FLAC' / 'Artist - Album'
    album.mkdir(parents=True)
    wanted = album / '04 - Track.mp3'
    other = album / '05 - Other Song.mp3'
    for path, title in ((wanted, 'Track'), (other, 'Other Song')):
        _minimal_mp3(path)
        tags = ID3()
        tags.add(TIT2(encoding=3, text=title))
        tags.add(TPE1(encoding=3, text='Artist'))
        tags.save(str(path))
    return album, wanted, other


def test_pick_track_in_album_matches_by_tags(tmp_path):
    album, wanted, other = _album_folder(tmp_path)
    found = usenet._pick_track_in_album(album, dict(_SONG), {})
    assert found == wanted
    assert found != other


def test_download_prefers_album_and_extracts_track(tmp_path, monkeypatch):
    album, wanted, other = _album_folder(tmp_path)
    settings = _settings(tmp_path, prefer_albums=True)
    settings['extensions'] = ['flac', 'mp3']

    monkeypatch.setattr(
        usenet,
        '_search_indexers',
        lambda indexers, query: [_ALBUM_RELEASE, _release()],
    )
    # Queue the album (first-ranked candidate); its storage is the album dir.
    monkeypatch.setattr(
        SabnzbdClient,
        'add_url',
        lambda self, url, name: (
            'nzo-album' if 'Album' in name else 'nzo-track'
        ),
    )
    monkeypatch.setattr(
        SabnzbdClient,
        'queue_slot',
        lambda self, nzo: {'nzo_id': nzo, 'percentage': 100},
    )
    monkeypatch.setattr(
        SabnzbdClient,
        'history_slot',
        lambda self, nzo: {
            'nzo_id': nzo,
            'status': 'Completed',
            'storage': str(album),
        },
    )
    monkeypatch.setattr(
        SabnzbdClient, 'delete_history', lambda self, nzo: None
    )
    monkeypatch.setattr(
        usenet, 'verify_downloaded_file_matches_spotify', lambda p, s: True
    )

    found = download_from_usenet(dict(_SONG), settings)
    assert found == wanted


@pytest.fixture(autouse=True)
def _reset_album_cache():
    usenet.configure_album_cache(None)
    usenet._NZB_FOLDERS.clear()
    usenet._NZB_CLAIMS.clear()
    yield
    usenet.configure_album_cache(None)
    usenet._NZB_FOLDERS.clear()
    usenet._NZB_CLAIMS.clear()


def test_album_only_prefetch_caches_the_release_without_picking_a_track(
    tmp_path, monkeypatch
):
    album, _wanted, _other = _album_folder(tmp_path)
    settings = _settings(tmp_path, prefer_albums=False)

    monkeypatch.setattr(
        usenet,
        '_search_indexers',
        lambda indexers, query: [_ALBUM_RELEASE, _release()],
    )
    monkeypatch.setattr(
        SabnzbdClient, 'add_url', lambda self, url, name: 'nzo-album'
    )
    monkeypatch.setattr(
        SabnzbdClient,
        'queue_slot',
        lambda self, nzo: {'nzo_id': nzo, 'percentage': 100},
    )
    monkeypatch.setattr(
        SabnzbdClient,
        'history_slot',
        lambda self, nzo: {
            'nzo_id': nzo,
            'status': 'Completed',
            'storage': str(album),
        },
    )
    monkeypatch.setattr(
        SabnzbdClient, 'delete_history', lambda self, nzo: None
    )
    monkeypatch.setattr(
        usenet,
        '_usenet_semaphore',
        lambda _settings: (_ for _ in ()).throw(
            AssertionError('album prefetch must bypass the parallel limit')
        ),
    )

    key = usenet.album_cache_key(dict(_SONG))
    folder = download_from_usenet(
        dict(_SONG),
        settings,
        album_only=True,
        album_keys=[key, 'other-key'],
    )

    assert folder == album
    assert usenet._album_cache_get(key) == album
    assert usenet._album_cache_get('other-key') == album
    assert usenet._nzb_folder(_ALBUM_RELEASE['nzb_url']) == album


def test_nzb_claim_allows_only_one_owner(tmp_path):
    state, folder, event = usenet._claim_nzb('https://idx/album.nzb')
    assert (state, folder) == ('owner', None)
    assert event is not None

    state, folder, waiting = usenet._claim_nzb('https://idx/album.nzb')
    assert state == 'wait'
    assert folder is None
    assert waiting is event

    usenet._finish_nzb_claim('https://idx/album.nzb', event, tmp_path)
    state, folder, _ = usenet._claim_nzb('https://idx/album.nzb')
    assert state == 'cached'
    assert folder == tmp_path

    usenet._forget_nzb_folders(tmp_path)
    assert usenet._claim_nzb('https://idx/album.nzb')[0] == 'owner'


def test_album_downloaded_once_and_reused_for_more_songs(
    tmp_path, monkeypatch
):
    album, wanted, other = _album_folder(tmp_path)
    settings = _settings(tmp_path, prefer_albums=True)

    searches: list[str] = []
    monkeypatch.setattr(
        usenet,
        '_search_indexers',
        lambda indexers, query: searches.append(query) or [_ALBUM_RELEASE],
    )
    monkeypatch.setattr(
        SabnzbdClient, 'add_url', lambda self, url, name: 'nzo1'
    )
    monkeypatch.setattr(
        SabnzbdClient,
        'queue_slot',
        lambda self, nzo: {'nzo_id': nzo, 'percentage': 100},
    )
    monkeypatch.setattr(
        SabnzbdClient,
        'history_slot',
        lambda self, nzo: {
            'nzo_id': nzo,
            'status': 'Completed',
            'storage': str(album),
        },
    )
    monkeypatch.setattr(
        SabnzbdClient, 'delete_history', lambda self, nzo: None
    )
    monkeypatch.setattr(
        usenet, 'verify_downloaded_file_matches_spotify', lambda p, s: True
    )

    # A different song from the same album.
    song2 = {
        'name': 'Other Song',
        # A featured credit must not give the same album another cache key.
        'artists': ['Featured Artist'],
        'album_artist': 'Artist',
        'album_name': 'Album',
        'duration': 200_000,
    }
    first = download_from_usenet(dict(_SONG), settings)
    assert first == wanted
    # The album folder is now cached for artist+album — the second song of
    # the same album must not touch the indexers or SABnzbd again.
    monkeypatch.setattr(
        SabnzbdClient,
        'add_url',
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError('album must not be downloaded twice')
        ),
    )
    second = download_from_usenet(dict(song2), settings)
    assert second == other
    assert len(searches) >= 1


def test_album_cache_survives_restart(tmp_path):
    album, _wanted, _other = _album_folder(tmp_path)
    cache_file = tmp_path / 'usenet_albums.json'
    usenet.configure_album_cache(cache_file)
    usenet._album_cache_put(usenet._album_cache_key(dict(_SONG)), album)
    assert cache_file.is_file()

    # A fresh process loads the same mapping.
    usenet.configure_album_cache(cache_file)
    assert (
        usenet._album_cache_get(usenet._album_cache_key(dict(_SONG))) == album
    )


def test_album_cache_drops_stale_folders(tmp_path):
    usenet.configure_album_cache(None)
    key = usenet._album_cache_key(dict(_SONG))
    usenet._album_cache_put(key, tmp_path / 'gone')
    assert usenet._album_cache_get(key) is None


def test_clear_album_cache_removes_only_folders_under_complete_dir(
    tmp_path,
):
    complete = tmp_path / 'usenet'
    inside = complete / 'Artist - Album'
    outside = tmp_path / 'outside'
    inside.mkdir(parents=True)
    outside.mkdir()
    usenet.configure_album_cache(tmp_path / 'albums.json')
    usenet._album_cache_put('inside', inside)
    usenet._album_cache_put('outside', outside)

    assert (
        usenet.clear_album_cache(complete_dir=complete, delete_folders=True)
        == 1
    )
    assert not inside.exists()
    assert outside.exists()
    assert usenet._album_cache_get('inside') is None
    assert usenet._album_cache_get('outside') is None


def test_cleanup_albums_removes_the_cached_folder(tmp_path, monkeypatch):
    complete = tmp_path / 'usenet'
    album = complete / 'Artist - Album'
    album.mkdir(parents=True)
    usenet.configure_album_cache(tmp_path / 'albums.json')
    key = usenet.album_cache_key(dict(_SONG))
    usenet._album_cache_put(key, album)

    class _NowTimer:
        daemon = False

        def __init__(self, _interval, fn, args=()):
            self.fn = fn
            self.args = args

        def start(self):
            self.fn(*self.args)

    monkeypatch.setattr(usenet.threading, 'Timer', _NowTimer)

    assert usenet.cleanup_albums([key], complete) == 1
    assert not album.exists()
    assert usenet._album_cache_get(key) is None


def test_delete_all_history_removes_every_entry(monkeypatch):
    calls: list[dict] = []

    def fake_api(self, params):
        calls.append(params)
        if params.get('name') == 'delete':
            return {}
        return {
            'history': {
                'slots': [
                    {'nzo_id': 'a'},
                    {'nzo_id': 'b'},
                    {},
                ]
            }
        }

    monkeypatch.setattr(SabnzbdClient, '_api', fake_api)
    client = SabnzbdClient({
        'sabnzbd': {
            'base_url': 'http://sab:8080',
            'api_key': 'k',
        }
    })
    assert client.delete_all_history() == 2
    deletes = [c for c in calls if c.get('name') == 'delete']
    assert sorted(c['value'] for c in deletes) == ['a', 'b']
    assert all(c['del_files'] == '1' for c in deletes)


def test_newznab_fallback_translates_params_for_prowlarr(monkeypatch):
    calls: list[tuple] = []

    class FakeResp:
        def __init__(self, status):
            self.status_code = status
            self.content = b''

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f'HTTP {self.status_code}')

    def fake_get(url, params=None, headers=None, **kwargs):
        calls.append((url, params))
        return FakeResp(404 if url.endswith('/api') else 200)

    monkeypatch.setattr(usenet.httpx, 'get', fake_get)
    client = usenet.NewznabClient({
        'name': 'prowlarr',
        'base_url': 'http://prowlarr:9696',
        'api_key': 'pk',
    })
    client.search('artist track')
    assert calls[0] == (
        'http://prowlarr:9696/api',
        {
            't': 'search',
            'q': 'artist track',
            'cat': '3000,3010',
            'apikey': 'pk',
            'extended': '1',
            'limit': '100',
        },
    )
    # The retry speaks Prowlarr's REST dialect, not Newznab's — otherwise
    # Prowlarr returns unfiltered results (or 400 on the categories value).
    assert calls[1] == (
        'http://prowlarr:9696/api/v1/search',
        [
            ('query', 'artist track'),
            ('categories', '3000'),
            ('categories', '3010'),
            ('apikey', 'pk'),
            ('limit', '100'),
        ],
    )
