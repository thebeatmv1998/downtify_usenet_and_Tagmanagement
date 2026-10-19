"""Reading and writing a track's full set of tags."""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

import pytest

from downtify import tag_editor

_MP3_FRAME = b'\xff\xfb\x90\x00' + b'\x00' * 413


def _png(size: int) -> bytes:
    return (
        b'\x89PNG\r\n\x1a\n'
        + struct.pack('>I', 13)
        + b'IHDR'
        + struct.pack('>II', size, size)
        + b'\x08\x06\x00\x00\x00'
    )


def _mp3(tmp_path: Path, name: str = 'a.mp3') -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_MP3_FRAME * 60)
    return path


_FIELDS = {
    'title': 'Deeper',
    'artist': 'Yves von Wartburg; Guest',
    'album': 'Glory Days',
    'album_artist': 'Yves von Wartburg',
    'track_number': 3,
    'track_total': 12,
    'disc_number': 1,
    'disc_total': 2,
    'date': '2016-11-18',
    'genre': 'Pop',
    'comment': 'ripped by hand',
    'composer': 'Someone Else',
}


def test_mp3_round_trip(tmp_path: Path) -> None:
    path = _mp3(tmp_path)

    tag_editor.write_tags(path, _FIELDS)
    tags = tag_editor.read_tags(path)

    assert tags['title'] == 'Deeper'
    assert tags['artist'] == 'Yves von Wartburg; Guest'
    assert tags['album'] == 'Glory Days'
    assert tags['album_artist'] == 'Yves von Wartburg'
    assert tags['track_number'] == 3
    assert tags['track_total'] == 12
    assert tags['disc_number'] == 1
    assert tags['disc_total'] == 2
    assert tags['date'] == '2016-11-18'
    assert tags['year'] == '2016'
    assert tags['genre'] == 'Pop'
    assert tags['comment'] == 'ripped by hand'
    assert tags['composer'] == 'Someone Else'


def test_an_emptied_field_is_removed(tmp_path: Path) -> None:
    path = _mp3(tmp_path)
    tag_editor.write_tags(path, _FIELDS)

    tag_editor.write_tags(path, {**_FIELDS, 'genre': '', 'comment': ''})
    tags = tag_editor.read_tags(path)

    assert not tags['genre']
    assert not tags['comment']
    # The others are untouched.
    assert tags['title'] == 'Deeper'


def test_cover_is_written_and_removed(tmp_path: Path) -> None:
    path = _mp3(tmp_path)

    tag_editor.write_tags(path, _FIELDS, cover=_png(600))
    assert tag_editor.read_tags(path)['has_cover'] is True

    tag_editor.write_tags(path, _FIELDS, remove_cover=True)
    assert tag_editor.read_tags(path)['has_cover'] is False


def test_write_refuses_an_unsupported_format(tmp_path: Path) -> None:
    path = tmp_path / 'a.wav'
    path.write_bytes(b'RIFF....WAVEfmt ')
    with pytest.raises(ValueError, match='Unsupported'):
        tag_editor.write_tags(path, {'title': 'x'})


def test_helpers() -> None:
    assert tag_editor._split_pair('3/12') == (3, 12)
    assert tag_editor._split_pair('3') == (3, 0)
    assert tag_editor._split_pair('') == (0, 0)
    assert tag_editor._cover_mime(_png(1)) == 'image/png'
    assert tag_editor._cover_mime(b'\xff\xd8\xff\xe0') == 'image/jpeg'
    assert tag_editor._artists({'artist': 'A; B; C'}) == ['A', 'B', 'C']
    assert (
        tag_editor._pair(
            {'track_number': 2, 'track_total': 9},
            'track_number',
            'track_total',
        )
        == '2/9'
    )


def test_read_of_a_missing_file_is_empty(tmp_path: Path) -> None:
    tags = tag_editor.read_tags(tmp_path / 'nope.mp3')
    assert not tags['title']
    assert tags['has_cover'] is False


def test_a_field_not_edited_keeps_its_other_value(tmp_path: Path) -> None:
    path = _mp3(tmp_path)
    tag_editor.write_tags(path, {**_FIELDS, 'disc_number': 2, 'disc_total': 2})
    # Editing the title alone must not blank the disc position.
    tag_editor.write_tags(path, {'title': 'Other'})
    tags: dict[str, Any] = tag_editor.read_tags(path)
    assert tags['title'] == 'Other'
    assert tags['disc_number'] == 2
