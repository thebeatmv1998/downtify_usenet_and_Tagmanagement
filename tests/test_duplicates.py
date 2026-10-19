"""Finding duplicate titles and albums in a library listing."""

from __future__ import annotations

from typing import Any

from downtify import duplicates


def _entry(
    file: str, title: str, artist: str, **extra: Any
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        'file': file,
        'title': title,
        'artist': artist,
        'album': 'Album',
        'album_artist': '',
        'track_number': 1,
        'duration': 200.0,
        'size': 100,
        'codec': 'mp3',
    }
    entry.update(extra)
    return entry


def test_the_same_song_twice_is_a_duplicate() -> None:
    entries = [
        _entry('a/Artist - Song.mp3', 'Song', 'Artist', size=100),
        _entry('b/Artist - Song.mp3', 'Song', 'Artist', size=140),
        _entry('c/Artist - Other.mp3', 'Other', 'Artist'),
    ]

    found = duplicates.find_duplicate_tracks(entries)

    assert len(found) == 1
    assert found[0]['count'] == 2
    assert found[0]['title'] == 'Song'
    assert [f['file'] for f in found[0]['files']] == [
        'a/Artist - Song.mp3',
        'b/Artist - Song.mp3',
    ]


def test_a_remaster_suffix_is_still_the_same_song() -> None:
    entries = [
        _entry('a/Artist - Song.mp3', 'Song', 'Artist'),
        _entry(
            'b/Artist - Song (Remastered).mp3', 'Song (Remastered)', 'Artist'
        ),
    ]
    assert duplicates.find_duplicate_tracks(entries)


def test_a_live_version_is_a_different_recording() -> None:
    entries = [
        _entry('a/Artist - Song.mp3', 'Song', 'Artist'),
        _entry('b/Artist - Song (Live).mp3', 'Song (Live)', 'Artist'),
    ]
    assert duplicates.find_duplicate_tracks(entries) == []


def test_a_cover_by_another_artist_is_not_a_duplicate() -> None:
    entries = [
        _entry('a/Artist - Song.mp3', 'Song', 'Artist'),
        _entry('b/Other - Song.mp3', 'Song', 'Other'),
    ]
    assert duplicates.find_duplicate_tracks(entries) == []


def test_an_album_in_two_folders_is_a_duplicate() -> None:
    entries = [
        _entry('Artist/Album/01 - One.mp3', 'One', 'Artist', track_number=1),
        _entry('Artist/Album/02 - Two.mp3', 'Two', 'Artist', track_number=2),
        _entry(
            'Artist/Album (1)/01 - One.mp3',
            'One',
            'Artist',
            track_number=1,
        ),
    ]

    found = duplicates.find_duplicate_albums(entries)

    assert len(found) == 1
    assert found[0]['album'] == 'Album'
    assert 'multiple folders' in found[0]['reasons']
    assert found[0]['track_count'] == 3


def test_an_album_holding_the_same_song_twice_is_a_duplicate() -> None:
    entries = [
        _entry('Artist/Album/01 - One.mp3', 'One', 'Artist', track_number=1),
        _entry('Artist/Album/09 - One.mp3', 'One', 'Artist', track_number=9),
        _entry('Artist/Album/02 - Two.mp3', 'Two', 'Artist', track_number=2),
    ]

    found = duplicates.find_duplicate_albums(entries)

    assert len(found) == 1
    assert found[0]['duplicate_titles'] == ['One']
    assert 'repeated song' in found[0]['reasons']


def test_a_clean_album_is_not_reported() -> None:
    entries = [
        _entry('Artist/Album/01 - One.mp3', 'One', 'Artist', track_number=1),
        _entry('Artist/Album/02 - Two.mp3', 'Two', 'Artist', track_number=2),
    ]
    assert duplicates.find_duplicate_albums(entries) == []


def test_find_duplicates_summary() -> None:
    entries = [
        _entry('a/S.mp3', 'Song', 'Artist', size=100),
        _entry('b/S.mp3', 'Song', 'Artist', size=140),
    ]

    report = duplicates.find_duplicates(entries)

    assert report['track_count'] == 1
    assert report['duplicate_files'] == 1
    # Three of these are byte-identical files in the test; the summary
    # reports what one copy of each duplicate would reclaim.
    assert report['reclaimable_bytes'] == 100
