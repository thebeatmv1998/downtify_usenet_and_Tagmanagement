"""Pacing, caching and fallbacks for the tag-source metadata lookups."""

from __future__ import annotations

from typing import Any, Optional

import httpx
import pytest

from downtify import deezer, itunes


class _Resp:
    def __init__(self, status: int, payload: Optional[dict] = None) -> None:
        self.status_code = status
        self._payload = payload or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                'bad status',
                request=None,
                response=self,  # type: ignore[arg-type]
            )

    def json(self) -> dict[str, Any]:
        return self._payload


@pytest.fixture(autouse=True)
def _clean_itunes(monkeypatch: Any):
    """Every test starts with fresh caches and no cooldown/downtime."""

    monkeypatch.setattr(itunes, '_search_cache', {})
    monkeypatch.setattr(itunes, '_genre_cache', {})
    monkeypatch.setattr(itunes, '_artwork_cache', {})
    monkeypatch.setattr(itunes, '_cooldown_until', 0.0)
    monkeypatch.setattr(itunes, '_down_until', 0.0)
    monkeypatch.setattr(itunes, '_consecutive_blocks', 0)


def test_query_pair_drops_the_duplicate_for_a_single(tmp_path: Any) -> None:
    # album == title is the "filed as its own single" case; asking the
    # same query twice only burns the rate budget.
    assert itunes._query_pair('Yves', 'Acid Protocol', 'Acid Protocol') == (
        'Yves Acid Protocol',
        '',
    )
    assert itunes._query_pair('Yves', 'Deeper', 'Glory Days') == (
        'Yves Deeper',
        'Yves Deeper Glory Days',
    )


def test_a_successful_search_is_cached(monkeypatch: Any) -> None:
    calls: list[str] = []

    def _get(url: str, **_kwargs: Any) -> _Resp:
        calls.append(url)
        return _Resp(200, {'results': [{'trackName': 'Deeper'}]})

    monkeypatch.setattr(itunes, '_rate_wait', lambda: None)
    monkeypatch.setattr(itunes.httpx, 'get', _get)

    first = itunes._search_itunes('Yves Deeper')
    second = itunes._search_itunes('Yves Deeper')

    assert first == second == [{'trackName': 'Deeper'}]
    assert len(calls) == 1


def test_a_refusal_backs_off_and_then_skips_itunes(monkeypatch: Any) -> None:
    calls: list[str] = []

    def _get(url: str, **_kwargs: Any) -> _Resp:
        calls.append(url)
        return _Resp(403)

    monkeypatch.setattr(itunes, '_rate_wait', lambda: None)
    monkeypatch.setattr(itunes.httpx, 'get', _get)
    monkeypatch.setattr(itunes, '_BLOCK_LIMIT', 2)
    monkeypatch.setattr(itunes, '_COOLDOWN_SECONDS', 1000.0)
    monkeypatch.setattr(itunes, '_DOWN_SECONDS', 1000.0)

    assert itunes._search_itunes('a') == []
    assert itunes._search_itunes('b') == []
    assert itunes.is_available() is False

    seen = len(calls)
    assert itunes._search_itunes('c') == []
    # Down: no request is attempted at all until the cooldown passes.
    assert len(calls) == seen


def test_a_success_clears_the_block_counter(monkeypatch: Any) -> None:
    monkeypatch.setattr(itunes, '_rate_wait', lambda: None)
    monkeypatch.setattr(itunes, '_consecutive_blocks', 2)
    monkeypatch.setattr(
        itunes.httpx, 'get', lambda *_a, **_k: _Resp(200, {'results': []})
    )

    itunes._search_itunes('ok')

    assert itunes._consecutive_blocks == 0


def test_deezer_lookup_reads_album_and_track_position(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(
        deezer,
        '_best_track_row',
        lambda *_a, **_k: {'id': 123, 'album': {'title': 'Glory Days'}},
    )
    monkeypatch.setattr(
        deezer,
        'track_from_id',
        lambda _id: {
            'album_name': 'Glory Days',
            'artists': ['Little Mix'],
            'year': '2016',
            'release_date': '2016-11-18',
            'track_number': 3,
        },
    )

    found = deezer.lookup_track_metadata({
        'artists': ['Little Mix'],
        'name': 'Touch',
    })

    assert found is not None
    assert found['album_name'] == 'Glory Days'
    assert found['album_artist'] == 'Little Mix'
    assert found['track_number'] == 3


def test_deezer_lookup_returns_none_without_a_match(monkeypatch: Any) -> None:
    monkeypatch.setattr(deezer, '_best_track_row', lambda *_a, **_k: None)
    assert (
        deezer.lookup_track_metadata({'artists': ['X'], 'name': 'Y'}) is None
    )
