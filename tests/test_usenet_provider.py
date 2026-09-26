"""Newznab parsing/scoring and the SABnzbd download flow."""

from __future__ import annotations

from typing import Any

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
    score, reasons = scored
    assert score >= 5
    assert 'ext_flac' in reasons
    assert 'grabs' in reasons


def test_score_excludes_passworded_attr():
    release = {
        'title': 'Artist - Track FLAC',
        'passworded': 1,
    }
    assert (
        _score_usenet_candidate(_SONG, release, target_duration=200) is None
    )


def test_score_excludes_password_keyword_in_title():
    release = {'title': 'Artist - Track FLAC password', 'passworded': 0}
    assert (
        _score_usenet_candidate(_SONG, release, target_duration=200) is None
    )


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
    assert (
        _score_usenet_candidate(_SONG, release, target_duration=200) is None
    )


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
    monkeypatch.setattr(
        usenet, '_search_indexers', lambda indexers, query: []
    )
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
