"""Detecting and merging an album a library keeps as separate singles."""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any, Optional

from mutagen.id3 import APIC, ID3, TALB, TDRC, TIT2, TPE1, TPE2, TRCK

from downtify import album_merge, cover_sources
from downtify.library_catalog import LibraryContext
from downtify.library_metadata_cache import LibraryMetadataCache

# Same playable-looking MP3 stream the library-upgrade tests use.
_MP3_FRAME = b'\xff\xfb\x90\x00' + b'\x00' * 413


def _png(size: int) -> bytes:
    return (
        b'\x89PNG\r\n\x1a\n'
        + struct.pack('>I', 13)
        + b'IHDR'
        + struct.pack('>II', size, size)
        + b'\x08\x06\x00\x00\x00'
    )


def _write_track(
    path: Path, *, title: str, album: str, number: str = '', cover_px: int = 0
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_MP3_FRAME * 40)
    tags = ID3()
    tags.add(TIT2(encoding=3, text=title))
    tags.add(TPE1(encoding=3, text='Little Mix'))
    tags.add(TPE2(encoding=3, text='Little Mix'))
    if album:
        tags.add(TALB(encoding=3, text=album))
    tags.add(TDRC(encoding=3, text='2016'))
    if number:
        tags.add(TRCK(encoding=3, text=number))
    if cover_px:
        tags.add(
            APIC(
                encoding=3,
                mime='image/png',
                type=3,
                desc='Cover',
                data=_png(cover_px),
            )
        )
    tags.save(str(path), v2_version=4)
    return path


def _context(tmp_path: Path) -> LibraryContext:
    return LibraryContext(
        download_dir=tmp_path / 'downloads',
        metadata_cache=LibraryMetadataCache(tmp_path / 'lib.db'),
    )


def _glory_days() -> dict[str, Any]:
    numbers = {'Shout Out to My Ex': 1, 'Touch': 2}
    return numbers


def _fake_resolver(song: dict[str, Any], _spotify_id: str) -> Optional[dict]:
    numbers = _glory_days()
    title = song.get('name') or ''
    if title not in numbers:
        return None
    return {
        'album_name': 'Glory Days',
        'album_artist': 'Little Mix',
        'year': '2016',
        'release_date': '2016-11-18',
        'track_number': numbers[title],
        'genre': 'Pop',
    }


def test_split_singles_are_detected_from_the_tag_sources(
    tmp_path: Path,
) -> None:
    ctx = _context(tmp_path)
    _write_track(
        ctx.download_dir / 'Little Mix' / 'Shout Out to My Ex.mp3',
        title='Shout Out to My Ex',
        album='Shout Out to My Ex',
    )
    _write_track(
        ctx.download_dir / 'Little Mix' / 'Touch.mp3',
        title='Touch',
        album='Touch',
    )

    plans = album_merge.detect_split_albums(ctx, resolver=_fake_resolver)

    assert len(plans) == 1
    plan = plans[0]
    assert plan.album == 'Glory Days'
    assert plan.album_artist == 'Little Mix'
    assert [t.title for t in plan.tracks] == ['Shout Out to My Ex', 'Touch']
    assert [t.track_number for t in plan.tracks] == [1, 2]


def test_tracks_with_no_album_tag_are_still_candidates(
    tmp_path: Path,
) -> None:
    # A library where the split singles carry no album tag at all (common
    # for YouTube/slskd downloads) must still be asked about.
    ctx = _context(tmp_path)
    _write_track(
        ctx.download_dir / 'Little Mix' / 'Shout Out to My Ex.mp3',
        title='Shout Out to My Ex',
        album='',
    )
    _write_track(
        ctx.download_dir / 'Little Mix' / 'Touch.mp3',
        title='Touch',
        album='',
    )

    plans = album_merge.detect_split_albums(ctx, resolver=_fake_resolver)

    assert len(plans) == 1
    assert plans[0].album == 'Glory Days'


def test_a_healthy_artist_is_not_a_candidate(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    # Both tracks already share one album tag: nothing to merge.
    _write_track(
        ctx.download_dir / 'Little Mix' / 'a.mp3',
        title='Shout Out to My Ex',
        album='Glory Days',
        number='1',
    )
    _write_track(
        ctx.download_dir / 'Little Mix' / 'b.mp3',
        title='Touch',
        album='Glory Days',
        number='2',
    )

    assert album_merge.detect_split_albums(ctx, resolver=_fake_resolver) == []


def test_apply_merge_moves_retags_and_writes_a_folder_cover(
    tmp_path: Path, monkeypatch: Any
) -> None:
    ctx = _context(tmp_path)
    _write_track(
        ctx.download_dir / 'Little Mix' / 'Shout Out to My Ex.mp3',
        title='Shout Out to My Ex',
        album='Shout Out to My Ex',
    )
    _write_track(
        ctx.download_dir / 'Little Mix' / 'Touch.mp3',
        title='Touch',
        album='Touch',
    )
    plans = album_merge.detect_split_albums(ctx, resolver=_fake_resolver)

    monkeypatch.setattr(
        album_merge.cover_sources, 'best_cover', lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        album_merge.cover_sources,
        'default_cover',
        lambda: cover_sources.CoverCandidate(
            source='default', data=_png(600), width=600
        ),
    )

    report = album_merge.apply_merge(
        [plan.to_dict() for plan in plans], ctx=ctx, downloader=None
    )

    assert report['moved'] == 2
    assert report['failed'] == []
    album_dir = ctx.download_dir / 'Little Mix' / 'Glory Days'
    first = album_dir / '01 - Shout Out to My Ex.mp3'
    second = album_dir / '02 - Touch.mp3'
    assert first.is_file()
    assert second.is_file()
    assert not (ctx.download_dir / 'Little Mix' / 'Touch.mp3').exists()

    tags = ID3(str(first))
    assert str(tags['TALB']) == 'Glory Days'
    assert str(tags['TPE2']) == 'Little Mix'
    assert str(tags['TRCK']) == '1'
    assert tags.getall('APIC')
    assert (album_dir / 'cover.png').is_file()


def test_apply_merge_reports_a_missing_file(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    ctx.download_dir.mkdir(parents=True, exist_ok=True)
    report = album_merge.apply_merge(
        [
            {
                'album': 'Glory Days',
                'album_artist': 'Little Mix',
                'tracks': [{'file': 'gone.mp3', 'title': 'X'}],
            }
        ],
        ctx=ctx,
        downloader=None,
    )

    assert report['moved'] == 0
    assert report['failed'] == ['gone.mp3']
