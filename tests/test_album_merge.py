"""Detecting and merging an album a library keeps as separate singles."""

from __future__ import annotations

import struct
import threading
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


def _write_track(  # noqa: PLR0913
    path: Path,
    *,
    title: str,
    album: str,
    number: str = '',
    cover_px: int = 0,
    frames: int = 40,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_MP3_FRAME * frames)
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

    progress = []
    report = album_merge.apply_merge(
        [plan.to_dict() for plan in plans],
        ctx=ctx,
        downloader=None,
        on_progress=lambda *event: progress.append(event),
    )

    assert report['moved'] == 2
    assert progress[0] == ('merging', 0, 2)
    assert progress[-1] == ('merging', 2, 2)
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


def test_base_album_strips_edition_suffixes() -> None:
    assert album_merge.base_album('X (Deluxe)') == 'X'
    assert album_merge.base_album('X (Deluxe Edition)') == 'X'
    assert album_merge.base_album('X - Deluxe Edition') == 'X'
    assert album_merge.base_album('X: Deluxe') == 'X'
    assert album_merge.base_album('X Deluxe') == 'X'
    assert album_merge.base_album('X (Special Edition)') == 'X'
    assert album_merge.base_album('X (2011 Remaster)') == 'X'
    assert album_merge.base_album('X (Deluxe Edition) [2011 Remaster]') == 'X'
    assert album_merge.base_album('X (Bonus Tracks)') == 'X'
    assert album_merge.base_album('X (25th Anniversary Edition)') == 'X'
    assert album_merge.base_album('Thriller (1982)') == 'Thriller'
    assert album_merge.base_album('Bad (1987)') == 'Bad'
    assert album_merge.base_album('Thriller [1982]') == 'Thriller'
    assert album_merge.base_album('Thriller - 1982') == 'Thriller'
    # An album genuinely called Deluxe, or a live/single release, is not
    # an edition of a shorter album and must stay distinct.
    assert album_merge.base_album('Deluxe') == 'Deluxe'
    assert album_merge.base_album('Something Special') == 'Something Special'
    assert album_merge.base_album('X (Live)') == 'X (Live)'
    assert album_merge.base_album('X - Single') == 'X - Single'
    assert album_merge.base_album('Blade Runner 2049') == 'Blade Runner 2049'
    assert album_merge.base_album('1984') == '1984'


def test_release_year_suffix_is_the_same_album(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    plain = ctx.download_dir / 'Michael Jackson' / 'Thriller'
    year = ctx.download_dir / 'Michael Jackson' / 'Thriller (1982)'
    _write_track(
        plain / 'A.mp3', title='A', album='Thriller', number='1', frames=80
    )
    _write_track(
        plain / 'B.mp3', title='B', album='Thriller', number='2', frames=60
    )
    _write_track(
        year / 'A.mp3',
        title='A',
        album='Thriller (1982)',
        number='1',
        frames=40,
    )
    _write_track(
        year / 'B.mp3',
        title='B',
        album='Thriller (1982)',
        number='2',
        frames=50,
    )

    plans = album_merge.detect_split_albums(
        ctx,
        resolver=lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError('no lookup needed')
        ),
    )

    assert len(plans) == 1
    plan = plans[0]
    assert plan.album == 'Thriller'
    assert plan.track_count == 2
    assert plan.dropped_count == 2


def test_folder_year_variant_merges_even_with_identical_tags(
    tmp_path: Path,
) -> None:
    ctx = _context(tmp_path)
    plain = ctx.download_dir / 'Michael Jackson' / 'Thriller'
    year = ctx.download_dir / 'Michael Jackson' / 'Thriller (1982)'
    # Both folders carry the same album tag and hold different songs,
    # so only the normalized folder names identify them as one album.
    _write_track(plain / 'A.mp3', title='A', album='Thriller', number='1')
    _write_track(plain / 'B.mp3', title='B', album='Thriller', number='2')
    _write_track(year / 'C.mp3', title='C', album='Thriller', number='3')
    _write_track(year / 'D.mp3', title='D', album='Thriller', number='4')

    plans = album_merge.detect_split_albums(
        ctx,
        resolver=lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError('no lookup needed')
        ),
    )

    assert len(plans) == 1
    plan = plans[0]
    assert plan.album == 'Thriller'
    assert plan.track_count == 4
    assert plan.dropped_count == 0
    assert plan.editions == ['Thriller', 'Thriller (1982)']


def test_anniversary_edition_with_more_songs_wins(
    tmp_path: Path,
) -> None:
    ctx = _context(tmp_path)
    year = ctx.download_dir / 'Michael Jackson' / 'Bad (1987)'
    anniversary = ctx.download_dir / 'Michael Jackson' / 'Bad 25th Anniversary'
    _write_track(year / 'A.mp3', title='A', album='Bad (1987)', number='1')
    _write_track(year / 'B.mp3', title='B', album='Bad (1987)', number='2')
    _write_track(
        anniversary / 'A.mp3',
        title='A',
        album='Bad 25th Anniversary',
        number='1',
    )
    _write_track(
        anniversary / 'B.mp3',
        title='B',
        album='Bad 25th Anniversary',
        number='2',
    )
    _write_track(
        anniversary / 'C.mp3',
        title='C',
        album='Bad 25th Anniversary',
        number='3',
    )

    plans = album_merge.detect_split_albums(
        ctx,
        resolver=lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError('no lookup needed')
        ),
    )

    assert len(plans) == 1
    plan = plans[0]
    assert plan.album == 'Bad 25th Anniversary'
    assert plan.track_count == 3
    assert plan.dropped_count == 2


def test_deluxe_edition_wins_and_best_quality_copy_is_kept(  # noqa: PLR0915
    tmp_path: Path, monkeypatch: Any
) -> None:
    ctx = _context(tmp_path)
    normal = ctx.download_dir / 'Little Mix' / 'X'
    deluxe = ctx.download_dir / 'Little Mix' / 'X (Deluxe)'
    # Normal edition: A and B. Deluxe: A, B and the bonus track C.
    # The normal copies of A and B are larger (better quality).
    _write_track(normal / 'A.mp3', title='A', album='X', number='1', frames=80)
    _write_track(normal / 'B.mp3', title='B', album='X', number='2', frames=60)
    _write_track(
        deluxe / 'A.mp3',
        title='A',
        album='X (Deluxe)',
        number='1',
        frames=40,
    )
    _write_track(
        deluxe / 'B.mp3',
        title='B',
        album='X (Deluxe)',
        number='2',
        frames=50,
    )
    _write_track(
        deluxe / 'C.mp3',
        title='C',
        album='X (Deluxe)',
        number='3',
        frames=40,
    )
    # Leftovers in the source album folder must not keep it alive after
    # the merge: the .lrc travels with its track, the cover is orphaned.
    (normal / 'A.lrc').write_text('[00:01] hi\n', encoding='utf-8')
    (normal / 'cover.png').write_bytes(_png(600))

    def _must_not_look_up(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError('edition merges must not call a tag source')

    stats: dict[str, Any] = {}
    plans = album_merge.detect_split_albums(
        ctx, resolver=_must_not_look_up, stats=stats
    )

    assert stats['tracks'] == 5
    assert stats['lookups'] == 0
    assert stats['elapsed_ms'] >= 0
    assert len(plans) == 1
    plan = plans[0]
    assert plan.album == 'X (Deluxe)'
    assert plan.album_artist == 'Little Mix'
    assert plan.track_count == 3
    assert plan.dropped_count == 2
    assert plan.editions == ['X (Deluxe)', 'X']

    kept = [track for track in plan.tracks if track.keep]
    assert [track.title for track in kept] == ['A', 'B', 'C']
    assert [track.track_number for track in kept] == [1, 2, 3]
    # A and B are the larger files from the standard edition.
    assert kept[0].file.replace('\\', '/').endswith('Little Mix/X/A.mp3')
    assert kept[1].file.replace('\\', '/').endswith('Little Mix/X/B.mp3')
    assert (
        kept[2].file.replace('\\', '/').endswith('Little Mix/X (Deluxe)/C.mp3')
    )
    dropped = [track for track in plan.tracks if not track.keep]
    assert {track.title for track in dropped} == {'A', 'B'}
    assert all(track.duplicate_of for track in dropped)

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
        [plan.to_dict()], ctx=ctx, downloader=None
    )

    assert report['moved'] == 3
    assert report['deleted'] == 2
    assert report['failed'] == []
    assert report['reclaimed_bytes'] > 0
    target = ctx.download_dir / 'Little Mix' / 'X (Deluxe)'
    first = target / '01 - A.mp3'
    second = target / '02 - B.mp3'
    third = target / '03 - C.mp3'
    assert first.is_file()
    assert second.is_file()
    assert third.is_file()
    assert album_merge.lyrics_mod.lrc_path_for(first).is_file()
    assert not normal.exists()
    assert not (deluxe / 'A.mp3').exists()
    assert not (deluxe / 'B.mp3').exists()

    tags = ID3(str(first))
    assert str(tags['TALB']) == 'X (Deluxe)'
    assert str(tags['TRCK']) == '1'


def test_duplicate_copies_in_two_folders_are_removed(
    tmp_path: Path, monkeypatch: Any
) -> None:
    ctx = _context(tmp_path)
    first = ctx.download_dir / 'Little Mix' / 'X'
    second = ctx.download_dir / 'Little Mix' / 'X (copy)'
    _write_track(first / 'A.mp3', title='A', album='X', number='1', frames=80)
    _write_track(first / 'B.mp3', title='B', album='X', number='2')
    _write_track(second / 'A.mp3', title='A', album='X', number='1')
    _write_track(second / 'C.mp3', title='C', album='X', number='3')

    plans = album_merge.detect_split_albums(
        ctx,
        resolver=lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError('no lookup needed')
        ),
    )
    assert len(plans) == 1
    plan = plans[0]
    assert plan.track_count == 3
    assert plan.dropped_count == 1
    # The larger copy in the first folder wins.
    kept_a = next(
        track for track in plan.tracks if track.keep and track.title == 'A'
    )
    assert kept_a.file.replace('\\', '/').endswith('Little Mix/X/A.mp3')

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
        [plan.to_dict()], ctx=ctx, downloader=None
    )
    assert report['moved'] == 3
    assert report['deleted'] == 1
    target = ctx.download_dir / 'Little Mix' / 'X'
    assert (target / '01 - A.mp3').is_file()
    assert (target / '02 - B.mp3').is_file()
    assert (target / '03 - C.mp3').is_file()
    assert not second.exists()


def test_apply_never_deletes_the_only_copy(
    tmp_path: Path, monkeypatch: Any
) -> None:
    ctx = _context(tmp_path)
    _write_track(
        ctx.download_dir / 'Little Mix' / 'A.mp3',
        title='A',
        album='X',
        number='1',
    )
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
        [
            {
                'album': 'X',
                'album_artist': 'Little Mix',
                'tracks': [
                    {
                        'file': 'Little Mix/A.mp3',
                        'title': 'A',
                        'current_album': 'X',
                        'track_number': 1,
                        # A client cannot turn the only copy into a
                        # duplicate and have it deleted.
                        'keep': False,
                    }
                ],
            }
        ],
        ctx=ctx,
        downloader=None,
    )

    assert report['deleted'] == 0
    assert report['moved'] == 1
    assert (ctx.download_dir / 'Little Mix' / 'X' / '01 - A.mp3').is_file()


def test_only_single_like_tracks_are_looked_up(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    album = ctx.download_dir / 'Little Mix' / 'Glory Days'
    _write_track(
        album / 'A.mp3',
        title='A',
        album='Glory Days',
        number='1',
    )
    _write_track(
        album / 'B.mp3',
        title='B',
        album='Glory Days',
        number='2',
    )
    _write_track(
        ctx.download_dir / 'Little Mix' / 'C.mp3',
        title='C',
        album='C',
    )
    _write_track(
        ctx.download_dir / 'Little Mix' / 'D.mp3',
        title='D',
        album='D',
    )

    calls: set[str] = set()
    calls_lock = threading.Lock()

    def _resolver(song: dict[str, Any], _spotify_id: str) -> dict[str, Any]:
        with calls_lock:
            calls.add(str(song.get('name') or ''))
        numbers = {'C': 3, 'D': 4}
        return {
            'album_name': 'Glory Days',
            'album_artist': 'Little Mix',
            'year': '2016',
            'track_number': numbers[str(song.get('name'))],
        }

    plans = album_merge.detect_split_albums(ctx, resolver=_resolver)

    assert calls == {'C', 'D'}
    assert len(plans) == 1
    plan = plans[0]
    assert plan.album == 'Glory Days'
    assert [track.title for track in plan.tracks if track.keep] == [
        'A',
        'B',
        'C',
        'D',
    ]
    assert [track.track_number for track in plan.tracks if track.keep] == [
        1,
        2,
        3,
        4,
    ]


def test_default_resolver_prefers_deezer_and_caches(
    monkeypatch: Any,
) -> None:
    album_merge.clear_resolution_cache()
    calls: list[str] = []

    def _deezer(_song: dict[str, Any]) -> dict[str, Any]:
        calls.append('deezer')
        return {'album_name': 'Deezer Album'}

    def _itunes(_song: dict[str, Any]) -> dict[str, Any]:
        calls.append('itunes')
        return {'album_name': 'iTunes Album'}

    monkeypatch.setattr(album_merge.deezer, 'lookup_track_metadata', _deezer)
    monkeypatch.setattr(album_merge.itunes, 'lookup_track_metadata', _itunes)

    song = {
        'name': 'A Very Unique Song',
        'artist': 'A Very Unique Artist',
        'album_name': 'A Very Unique Single',
    }
    first = album_merge.default_resolver(song, '')
    second = album_merge.default_resolver(song, '')

    assert first == {'album_name': 'Deezer Album'}
    assert second == first
    assert calls == ['deezer']
    album_merge.clear_resolution_cache()


def test_default_resolver_asks_itunes_when_deezer_only_knows_the_single(
    monkeypatch: Any,
) -> None:
    album_merge.clear_resolution_cache()
    calls: list[str] = []

    def _deezer(_song: dict[str, Any]) -> dict[str, Any]:
        calls.append('deezer')
        return {'album_name': 'Unique Title'}

    def _itunes(_song: dict[str, Any]) -> dict[str, Any]:
        calls.append('itunes')
        return {'album_name': 'The Real Album'}

    monkeypatch.setattr(album_merge.deezer, 'lookup_track_metadata', _deezer)
    monkeypatch.setattr(album_merge.itunes, 'lookup_track_metadata', _itunes)

    song = {
        'name': 'Unique Title',
        'artist': 'Unique Artist',
        'album_name': 'Unique Title',
    }
    assert (
        album_merge.default_resolver(song, '')['album_name']
        == 'The Real Album'
    )
    assert calls == ['deezer', 'itunes']
    # The chosen answer is cached, so the next scan is free.
    album_merge.default_resolver(song, '')
    assert calls == ['deezer', 'itunes']
    album_merge.clear_resolution_cache()


def test_duration_clusters_keep_extended_versions_separate() -> None:
    class _Item:
        def __init__(self, duration: float) -> None:
            self.duration = duration

    items = [_Item(180.0), _Item(180.4), _Item(245.0)]
    clusters = album_merge._duration_clusters(
        items, lambda item: item.duration
    )
    assert [len(cluster) for cluster in clusters] == [2, 1]


# ?? Content-aware editions, unknown folders, empty folders ???????????


def _no_lookup(_song, _spotify_id):
    return None


def test_spilled_edition_is_merged_by_track_overlap(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    standard = ctx.download_dir / 'Little Mix' / 'GUTS'
    for number, title in enumerate(('A', 'B'), start=1):
        _write_track(
            standard / f'{title}.mp3',
            title=title,
            album='GUTS',
            number=str(number),
        )
    spilled = ctx.download_dir / 'Little Mix' / 'GUTS (Spilled)'
    for number, title in enumerate(('A', 'B', 'C'), start=1):
        _write_track(
            spilled / f'{title}.mp3',
            title=title,
            album='GUTS (Spilled)',
            number=str(number),
        )

    plans = album_merge.detect_split_albums(ctx, resolver=_no_lookup)

    assert len(plans) == 1
    plan = plans[0]
    assert plan.album == 'GUTS (Spilled)'
    assert {track.title for track in plan.tracks if track.keep} == {
        'A',
        'B',
        'C',
    }
    assert 'GUTS' in plan.editions
    assert 'GUTS (Spilled)' in plan.editions


def test_similar_name_with_different_songs_is_not_merged(
    tmp_path: Path,
) -> None:
    ctx = _context(tmp_path)
    first = ctx.download_dir / 'Little Mix' / 'Global Warming'
    for number, title in enumerate(('A', 'B'), start=1):
        _write_track(
            first / f'{title}.mp3',
            title=title,
            album='Global Warming',
            number=str(number),
        )
    second = ctx.download_dir / 'Little Mix' / 'Global Warming Meltdown'
    for number, title in enumerate(('C', 'D'), start=1):
        _write_track(
            second / f'{title}.mp3',
            title=title,
            album='Global Warming Meltdown',
            number=str(number),
        )

    assert album_merge.detect_split_albums(ctx, resolver=_no_lookup) == []


def _unknown_resolver(song, _spotify_id):
    if song.get('name') != 'Shout Out to My Ex':
        return None
    return {
        'album_name': 'Glory Days',
        'album_artist': 'Little Mix',
        'year': '2016',
        'track_number': 1,
        'genre': 'Pop',
    }


def test_unknown_folder_tracks_are_rehomed_and_folder_disappears(
    tmp_path: Path,
) -> None:
    ctx = _context(tmp_path)
    unknown = ctx.download_dir / 'unknown' / 'song.mp3'
    _write_track(unknown, title='Shout Out to My Ex', album='unknown')

    report = album_merge.tidy_library(
        ctx,
        downloader=None,
        resolver=_unknown_resolver,
    )

    target = (
        ctx.download_dir
        / 'Little Mix'
        / 'Glory Days'
        / '01 - Shout Out to My Ex.mp3'
    )
    assert report['rehomed'] == 1
    assert target.is_file()
    assert not (ctx.download_dir / 'unknown').exists()
    tags = ID3(str(target))
    assert str(tags['TALB']) == 'Glory Days'
    assert str(tags['TPE2']) == 'Little Mix'
    assert tags['TRCK'].text[0] == '1'


def test_unknown_duplicate_is_removed_when_the_album_already_has_it(
    tmp_path: Path,
) -> None:
    ctx = _context(tmp_path)
    existing = (
        ctx.download_dir
        / 'Little Mix'
        / 'Glory Days'
        / '01 - Shout Out to My Ex.mp3'
    )
    _write_track(
        existing, title='Shout Out to My Ex', album='Glory Days', number='1'
    )
    duplicate = ctx.download_dir / 'unknown' / 'song.mp3'
    _write_track(duplicate, title='Shout Out to My Ex', album='unknown')

    report = album_merge.tidy_library(
        ctx,
        downloader=None,
        resolver=_unknown_resolver,
    )

    assert report['duplicates_removed'] == 1
    assert report['rehomed'] == 0
    assert existing.is_file()
    assert not (ctx.download_dir / 'unknown').exists()


def test_empty_album_folders_are_deleted_but_used_ones_stay(
    tmp_path: Path,
) -> None:
    ctx = _context(tmp_path)
    empty = ctx.download_dir / 'Little Mix' / 'Empty Album'
    empty.mkdir(parents=True)
    used = ctx.download_dir / 'Little Mix' / 'Glory Days'
    _write_track(used / 'a.mp3', title='A', album='Glory Days', number='1')

    removed = album_merge.prune_empty_folders(ctx)

    assert removed == 1
    assert not empty.exists()
    assert used.is_dir()


def test_live_album_with_the_same_songs_is_not_merged(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    studio = ctx.download_dir / 'Little Mix' / 'Glory Days'
    for number, title in enumerate(('A', 'B'), start=1):
        _write_track(
            studio / f'{title}.mp3',
            title=title,
            album='Glory Days',
            number=str(number),
            frames=40,
        )
    live = ctx.download_dir / 'Little Mix' / 'Glory Days Live'
    for number, title in enumerate(('A', 'B'), start=1):
        _write_track(
            live / f'{title}.mp3',
            title=title,
            album='Glory Days Live',
            number=str(number),
            frames=80,
        )

    assert album_merge.detect_split_albums(ctx, resolver=_no_lookup) == []


def test_unknown_track_uses_a_correctly_tagged_copy_without_a_lookup(
    tmp_path: Path,
) -> None:
    ctx = _context(tmp_path)
    existing = (
        ctx.download_dir
        / 'Little Mix'
        / 'Glory Days'
        / '01 - Shout Out to My Ex.mp3'
    )
    _write_track(
        existing, title='Shout Out to My Ex', album='Glory Days', number='1'
    )
    unknown = ctx.download_dir / 'unknown' / 'song.mp3'
    _write_track(unknown, title='Shout Out to My Ex', album='')

    def _must_not_lookup(_song, _spotify_id):
        raise AssertionError('local tags already know the album')

    report = album_merge.tidy_library(
        ctx,
        downloader=None,
        resolver=_must_not_lookup,
    )

    # The destination already had the track: the unknown copy is removed.
    assert report['duplicates_removed'] == 1
    assert existing.is_file()
    assert not unknown.exists()
    assert not (ctx.download_dir / 'unknown').exists()


def test_a_folder_with_only_a_cover_is_removed(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    _write_track(
        ctx.download_dir / 'Little Mix' / 'Glory Days' / 'a.mp3',
        title='A',
        album='Glory Days',
        number='1',
    )
    folder = ctx.download_dir / 'Little Mix' / 'Abandoned'
    folder.mkdir(parents=True)
    (folder / 'cover.jpg').write_bytes(b'jpeg')
    (folder / 'info.nfo').write_text('release info', encoding='utf-8')

    assert album_merge.prune_empty_folders(ctx) == 1
    assert not folder.exists()


def test_spilled_folder_is_merged_even_when_the_tags_say_guts(
    tmp_path: Path,
) -> None:
    ctx = _context(tmp_path)
    standard = ctx.download_dir / 'Olivia Rodrigo' / 'GUTS (2023)'
    for number, title in enumerate(('A', 'B'), start=1):
        _write_track(
            standard / f'{title}.mp3',
            title=title,
            album='GUTS',
            number=str(number),
        )
    spilled = ctx.download_dir / 'Olivia Rodrigo' / 'GUTS (spilled)'
    for number, title in enumerate(('C', 'D'), start=1):
        _write_track(
            spilled / f'{title}.mp3',
            title=title,
            album='GUTS',
            number=str(number),
        )

    plans = album_merge.detect_split_albums(ctx, resolver=_no_lookup)

    assert len(plans) == 1
    plan = plans[0]
    assert plan.album == 'GUTS'
    assert {track.title for track in plan.tracks if track.keep} == {
        'A',
        'B',
        'C',
        'D',
    }
    assert any('spilled' in edition for edition in plan.editions)


def test_artist_prefixed_title_is_cleaned_before_matching(
    tmp_path: Path,
) -> None:
    ctx = _context(tmp_path)
    existing = (
        ctx.download_dir
        / 'Little Mix'
        / 'Glory Days'
        / '01 - Shout Out to My Ex.mp3'
    )
    _write_track(
        existing, title='Shout Out to My Ex', album='Glory Days', number='1'
    )
    unknown = (
        ctx.download_dir / 'unknown' / 'Little Mix - Shout Out to My Ex.mp3'
    )
    _write_track(
        unknown,
        title='Little Mix - Shout Out to My Ex',
        album='',
    )

    report = album_merge.tidy_library(
        ctx,
        downloader=None,
        resolver=lambda *args: None,
    )

    assert report['duplicates_removed'] == 1
    assert existing.is_file()
    assert not unknown.exists()
