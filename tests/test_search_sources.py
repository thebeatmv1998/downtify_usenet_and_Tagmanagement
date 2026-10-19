"""Parsing MusicBrainz and Discogs responses into editor candidates."""

from __future__ import annotations

from typing import Any

import pytest

from downtify import discogs, musicbrainz


def test_musicbrainz_search_maps_recording_and_release(
    monkeypatch: Any,
) -> None:
    payload = {
        'recordings': [
            {
                'id': 'rec1',
                'title': 'Deeper',
                'artist-credit': [{'name': 'Yves von Wartburg'}],
                'releases': [
                    {
                        'id': 'rel1',
                        'title': 'Glory Days',
                        'date': '2016-11-18',
                        'artist-credit': [{'name': 'Yves von Wartburg'}],
                        'release-group': {'primary-type': 'Album'},
                    }
                ],
            }
        ]
    }
    monkeypatch.setattr(musicbrainz, '_search_cache', {})
    monkeypatch.setattr(musicbrainz, '_get', lambda *_a, **_k: payload)

    results = musicbrainz.search_recordings('Deeper')

    assert len(results) == 1
    assert results[0]['title'] == 'Deeper'
    assert results[0]['album'] == 'Glory Days'
    assert results[0]['year'] == '2016'
    assert results[0]['release_id'] == 'rel1'
    assert results[0]['release_type'] == 'album'


def test_musicbrainz_release_tracklist(monkeypatch: Any) -> None:
    payload = {
        'title': 'Glory Days',
        'date': '2016-11-18',
        'artist-credit': [{'name': 'Yves von Wartburg'}],
        'media': [
            {
                'position': 1,
                'tracks': [
                    {
                        'position': 1,
                        'title': 'Acid Protocol',
                        'length': 215000,
                        'recording': {'id': 'rec1'},
                    }
                ],
            }
        ],
    }
    monkeypatch.setattr(musicbrainz, '_release_cache', {})
    monkeypatch.setattr(musicbrainz, '_get', lambda *_a, **_k: payload)

    data = musicbrainz.release_tracklist('rel1')

    assert data is not None
    assert data['album'] == 'Glory Days'
    assert data['track_total'] == 1
    assert data['tracks'][0]['position'] == 1
    assert data['tracks'][0]['duration'] == 215.0


def test_discogs_search_parses_artist_and_album(monkeypatch: Any) -> None:
    payload = {
        'results': [
            {
                'id': 42,
                'title': 'Yves von Wartburg - Glory Days',
                'year': '2016',
                'genre': ['Pop'],
                'style': ['Synth-pop'],
            }
        ]
    }
    monkeypatch.setattr(discogs, '_search_cache', {})
    monkeypatch.setattr(discogs, '_get', lambda *_a, **_k: payload)

    results = discogs.search_releases('Glory Days', token='tok')

    assert results[0]['artist'] == 'Yves von Wartburg'
    assert results[0]['album'] == 'Glory Days'
    assert results[0]['release_id'] == '42'
    assert 'Pop' in results[0]['genre']


def test_discogs_search_needs_a_token() -> None:
    with pytest.raises(discogs.DiscogsTokenMissing):
        discogs.search_releases('x', token='')


def test_discogs_release_skips_headings(monkeypatch: Any) -> None:
    payload = {
        'title': 'Glory Days',
        'year': 2016,
        'artists': [{'name': 'Yves von Wartburg'}],
        'genres': ['Pop'],
        'tracklist': [
            {'type_': 'heading', 'title': 'Side A'},
            {'position': 'A2', 'title': 'Deeper', 'duration': '3:35'},
        ],
    }
    monkeypatch.setattr(discogs, '_release_cache', {})
    monkeypatch.setattr(discogs, '_get', lambda *_a, **_k: payload)

    data = discogs.release('7', token='')

    assert data is not None
    assert data['track_total'] == 1
    assert data['tracks'][0]['title'] == 'Deeper'
    assert data['tracks'][0]['position'] == 2
    assert data['tracks'][0]['duration'] == 215.0


def test_discogs_position_parsing() -> None:
    assert discogs._parse_position('3') == (1, 3)
    assert discogs._parse_position('A2') == (1, 2)
    assert discogs._parse_position('1-4') == (1, 4)
