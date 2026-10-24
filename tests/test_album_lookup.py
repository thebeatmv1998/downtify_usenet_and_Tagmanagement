"""Album lookups for downloads whose tags carry no album."""

from __future__ import annotations

from typing import Any

import pytest

from downtify import album_lookup


@pytest.fixture(autouse=True)
def _clear_cache():
    album_lookup.clear_cache()
    yield
    album_lookup.clear_cache()


def _song(**values: Any) -> dict[str, Any]:
    song = {'name': 'Track', 'artists': ['Artist'], 'album_name': ''}
    song.update(values)
    return song


def test_a_missing_album_is_looked_up_and_cached(monkeypatch):
    calls: list[str] = []

    def _deezer(song):
        calls.append('deezer')
        return {
            'album_name': 'Real Album',
            'album_artist': 'Artist',
            'year': '2020',
            'track_number': 3,
        }

    monkeypatch.setattr(album_lookup.deezer, 'lookup_track_metadata', _deezer)
    monkeypatch.setattr(
        album_lookup.itunes,
        'lookup_track_metadata',
        lambda song: pytest.fail('iTunes must not be asked after Deezer'),
    )

    first = album_lookup.lookup_album(_song(album_name='unknown'))
    second = album_lookup.lookup_album(_song(album_name=''))

    assert first
    assert first['album_name'] == 'Real Album'
    assert second == first
    assert calls == ['deezer']


def test_a_song_with_a_real_album_costs_no_request(monkeypatch):
    monkeypatch.setattr(
        album_lookup.deezer,
        'lookup_track_metadata',
        lambda song: pytest.fail('must not look up a tagged album'),
    )
    assert album_lookup.lookup_album(_song(album_name='Real Album')) is None


def test_itunes_is_the_fallback_when_deezer_has_nothing(monkeypatch):
    monkeypatch.setattr(
        album_lookup.deezer, 'lookup_track_metadata', lambda song: None
    )
    monkeypatch.setattr(
        album_lookup.itunes,
        'lookup_track_metadata',
        lambda song: {'album_name': 'From iTunes'},
    )
    found = album_lookup.lookup_album(_song())
    assert found == {'album_name': 'From iTunes'}


def test_a_placeholder_answer_is_not_accepted(monkeypatch):
    monkeypatch.setattr(
        album_lookup.deezer,
        'lookup_track_metadata',
        lambda song: {'album_name': 'unknown'},
    )
    monkeypatch.setattr(
        album_lookup.itunes, 'lookup_track_metadata', lambda song: None
    )
    assert album_lookup.lookup_album(_song()) is None


def test_a_failed_source_does_not_break_the_other(monkeypatch):
    def _boom(song):
        raise RuntimeError('offline')

    monkeypatch.setattr(album_lookup.deezer, 'lookup_track_metadata', _boom)
    monkeypatch.setattr(
        album_lookup.itunes,
        'lookup_track_metadata',
        lambda song: {'album_name': 'Still Found'},
    )
    assert album_lookup.lookup_album(_song())['album_name'] == 'Still Found'
