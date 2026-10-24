"""Merge an album a library holds as singles or as several editions.

A track pulled from a release page that only lists the single it was
published on — or one downloaded one song at a time — often carries the
song's own title as its *album* tag, so a player shows a dozen
one-track albums instead of the album they all belong to. The same
album also often sits in the library twice: once as the standard
edition and once as the deluxe edition with a few bonus tracks.

Detection groups tracks by their album title with edition suffixes
(``Deluxe``, ``Expanded``, ``Special``, ...) removed. The edition with
the most distinct songs becomes the target, every song is kept once
(the best-quality copy wins) and duplicate copies are marked for
removal. Only tracks that look like standalone singles need a remote
tag source to find their real album; standard/deluxe collisions are
resolved from the local tags alone, so a scan does not pay for one
request per track.

Detection never writes anything: it returns a plan the caller shows for
confirmation. :func:`apply_merge` then retags and moves the kept tracks
into one album folder and deletes the duplicate copies — but only after
a better copy has been moved successfully.
"""

from __future__ import annotations

import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from loguru import logger

from . import (
    album_covers,
    cover_sources,
    deezer,
    itunes,
    library_cleanup,
    spotify,
)
from . import lyrics as lyrics_mod
from .downloader import _AUDIO_EXTENSIONS, _sanitize, embed_metadata
from .library_catalog import (
    LibraryContext,
    album_artist_of,
    fold_text,
    is_blank_or_placeholder,
    list_library_entries,
    song_from_entry,
    song_key,
)
from .library_metadata import read_audio_metadata
from .library_paths import (
    library_file_root,
    library_stored_path,
    locate_library_file,
    slskd_dir_from_downloader,
)
from .library_paths_cache import invalidate_library_paths_cache

#: How many tracks of one artist are looked up in a single detection
#: pass. A source lookup each, so this bounds a mistake (a huge artist
#: folder) without truncating any realistic album.
LOOKUP_LIMIT = 120

#: Concurrent tag-source lookups. Spotify and Deezer answer in
#: parallel; iTunes keeps its own one-request-per-second gate, so its
#: calls still queue. The win is for libraries with Spotify ids, where
#: the old sequential loop spent most of its time waiting.
MAX_PARALLEL_LOOKUPS = 8

#: Duplicate copies whose lengths differ by more than this are treated
#: as different recordings (an extended mix, a live version) and both
#: are kept. Encoder padding between formats stays well below it.
DURATION_TOLERANCE_SECONDS = 5.0

_LOSSLESS_CODECS = frozenset({
    'flac',
    'alac',
    'wav',
    'aiff',
    'aif',
    'ape',
    'wv',
    'dsd',
    'dsf',
})

#: Trailing edition markers that name the same album rather than a
#: different release. Only stripped from the end, and only when a
#: non-empty base title remains, so an album actually called "Deluxe"
#: is left alone.
_EDITION_WORD = (
    r'(?:the\s+)?(?:super\s+deluxe|deluxe|special|expanded|'
    r'bonus(?:\s+tracks?)?|(?:\d+(?:st|nd|rd|th)\s+)?anniversary|'
    r'collector\'?s|limited|platinum|legacy|'
    r'(?:(?:19|20)\d{2}\s+)?remaster(?:ed)?)'
)
_EDITION_TAIL = re.compile(
    r'(?:'
    # Bracketed: any edition marker counts, e.g. "(Special)".
    r'\s*[\(\[]\s*'
    + _EDITION_WORD
    + r'(?:\s+(?:edition|version))?(?:\s+(?:19|20)\d{2})?\s*[\)\]]'
    # After a dash/colon a bare marker counts too.
    r'|\s*[-–—:]\s*'
    + _EDITION_WORD
    + r'(?:\s+(?:edition|version))?(?:\s+(?:19|20)\d{2})?'
    # Bare "Deluxe"/"Remastered" at the end.
    r'|\s+(?:super\s+deluxe|deluxe|'
    r'(?:(?:19|20)\d{2}\s+)?remaster(?:ed)?)'
    r'(?:\s+(?:edition|version))?(?:\s+(?:19|20)\d{2})?'
    # Bare "Special Edition" etc., but not a lone adjective.
    r'|\s+(?:the\s+)?(?:special|expanded|anniversary|collector\'?s|'
    r'limited|platinum|legacy|bonus(?:\s+tracks?)?)'
    r'\s+(?:edition|version)(?:\s+(?:19|20)\d{2})?'
    # "Bad 25th Anniversary" without a following "Edition".
    r'|\s+(?:the\s+)?(?:\d+(?:st|nd|rd|th)\s+)?anniversary'
    r'(?:\s+(?:edition|version))?(?:\s+(?:19|20)\d{2})?'
    # A release year is not a different album: "Thriller (1982)".
    r'|\s*[\(\[]\s*(?:19|20)\d{2}\s*[\)\]]'
    r'|\s*[-–—:]\s*(?:19|20)\d{2}'
    r')\s*$',
    re.IGNORECASE,
)

_EDITION_RANK_WORDS = re.compile(
    r'deluxe|special|expanded|anniversary|remaster|bonus|'
    r'collector|limited|platinum|legacy',
    re.IGNORECASE,
)

_RESOLUTION_CACHE: dict[str, dict[str, Any]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 4096


@dataclass
class MergeTrack:
    """One file of a proposed merge, as the preview shows it."""

    file: str
    title: str
    current_album: str
    track_number: int = 0
    release_date: str = ''
    genre: str = ''
    #: False means this copy is a duplicate and is deleted after the
    #: best-quality copy has been moved into the album folder.
    keep: bool = True
    quality: str = ''
    codec: str = ''
    bitrate: int = 0
    sample_rate: int = 0
    channels: int = 0
    size: int = 0
    duration: float = 0.0
    #: True for a file that already sits in the winning edition, used
    #: only as the tie-breaker between two equally good copies.
    target_edition: bool = False
    #: The kept file this one is a duplicate of, for the preview.
    duplicate_of: str = ''

    def quality_signature(self) -> tuple[int, int, int, int, int]:
        return _quality_signature(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            'file': self.file,
            'title': self.title,
            'current_album': self.current_album,
            'track_number': self.track_number,
            'release_date': self.release_date,
            'genre': self.genre,
            'keep': self.keep,
            'quality': self.quality,
            'codec': self.codec,
            'bitrate': self.bitrate,
            'sample_rate': self.sample_rate,
            'channels': self.channels,
            'size': self.size,
            'duration': self.duration,
            'target_edition': self.target_edition,
            'duplicate_of': self.duplicate_of,
        }


@dataclass
class MergePlan:
    """One album to merge, with its tracks in the order to write them."""

    album: str
    album_artist: str
    year: str = ''
    tracks: list[MergeTrack] = field(default_factory=list)
    editions: list[str] = field(default_factory=list)

    @property
    def track_count(self) -> int:
        return sum(1 for track in self.tracks if track.keep)

    @property
    def dropped_count(self) -> int:
        return sum(1 for track in self.tracks if not track.keep)

    def to_dict(self) -> dict[str, Any]:
        dropped = [track for track in self.tracks if not track.keep]
        return {
            'album': self.album,
            'album_artist': self.album_artist,
            'year': self.year,
            'track_count': self.track_count,
            'file_count': len(self.tracks),
            'dropped_count': len(dropped),
            'dropped_bytes': sum(int(track.size or 0) for track in dropped),
            'editions': list(self.editions),
            'tracks': [track.to_dict() for track in self.tracks],
        }


#: Resolver signature: ``(song, spotify_id) -> remote song dict or None``.
Resolver = Callable[[dict[str, Any], str], Optional[dict[str, Any]]]
Progress = Callable[[str, int, int], None]


def _progress(
    callback: Optional[Progress], stage: str, completed: int, total: int
) -> None:
    if callback is not None:
        callback(stage, completed, total)


def clear_resolution_cache() -> None:
    """Forget successful tag-source answers (used by tests)."""

    with _CACHE_LOCK:
        _RESOLUTION_CACHE.clear()


def _resolution_key(song: dict[str, Any], spotify_id: str) -> str:
    sid = str(spotify_id or '').strip()
    if sid:
        return f'spotify:{sid}'
    return 'meta:' + '|'.join((
        str(song.get('album_artist') or song.get('artist') or '')
        .strip()
        .casefold(),
        str(song.get('name') or '').strip().casefold(),
        str(song.get('album_name') or '').strip().casefold(),
    ))


def default_resolver(
    song: dict[str, Any], spotify_id: str
) -> Optional[dict[str, Any]]:
    """Which album a track is on, per Spotify, Deezer or iTunes.

    Spotify first when the track id is known (most precise). Otherwise
    Deezer is asked first because it answers two quick HTTP calls with
    no hard rate limit. When Deezer only knows the track as its own
    single (the album name equals the song title), Apple Music (iTunes)
    is asked anyway: its catalogue often maps a former single to the
    album it later appeared on. A successful answer is cached for the
    life of the process, so a second scan does not ask again.
    """

    cache_key = _resolution_key(song, spotify_id)
    with _CACHE_LOCK:
        cached = _RESOLUTION_CACHE.get(cache_key)
    if cached is not None:
        return dict(cached)

    found = _resolve_uncached(song, spotify_id)
    if found and found.get('album_name'):
        with _CACHE_LOCK:
            if len(_RESOLUTION_CACHE) >= _CACHE_MAX:
                _RESOLUTION_CACHE.clear()
            _RESOLUTION_CACHE[cache_key] = dict(found)
    return found


def _resolve_uncached(
    song: dict[str, Any], spotify_id: str
) -> Optional[dict[str, Any]]:
    if spotify_id:
        try:
            found = spotify.track_from_id(spotify_id, with_album=True)
            if found and found.get('album_name'):
                return found
        except Exception:
            logger.opt(exception=True).debug(
                'Album merge: Spotify lookup failed for {}', spotify_id
            )

    def lookup(
        source: str, fn: Callable[..., Any]
    ) -> Optional[dict[str, Any]]:
        try:
            found = fn(song)
        except Exception:
            logger.opt(exception=True).debug(
                'Album merge: {} lookup failed for {!r}',
                source,
                song.get('name'),
            )
            return None
        return found if found and found.get('album_name') else None

    title = str(song.get('name') or '').strip().casefold()
    from_deezer = lookup('Deezer', deezer.lookup_track_metadata)
    if from_deezer:
        album = str(from_deezer.get('album_name') or '').strip().casefold()
        # A real album answers the question; the single's own title does
        # not, so iTunes gets a chance to place it on the album.
        if album and album != title:
            return from_deezer
    from_itunes = lookup('iTunes', itunes.lookup_track_metadata)
    if from_itunes:
        return from_itunes
    return from_deezer


# ── Edition / quality helpers ──────────────────────────────────────────
def base_album(title: str) -> str:
    """*title* without trailing deluxe/expanded/remaster markers.

    Iterates so ``Album (Deluxe Edition) [2011 Remaster]`` collapses all
    the way to ``Album``. Falls back to the original title if stripping
    would leave nothing (an album genuinely named ``Deluxe``).
    """

    text = str(title or '').strip()
    if not text:
        return ''
    original = text
    for _ in range(5):
        cleaned = _EDITION_TAIL.sub('', text).strip(' -–—:')
        if not cleaned or cleaned == text:
            break
        text = cleaned
    return text or original


def _edition_rank(title: str) -> int:
    """1 for a real edition marker, 0 for a bare release year."""

    if fold_text(base_album(title)) == fold_text(title):
        return 0
    return 1 if _EDITION_RANK_WORDS.search(title) else 0


def _field(data: Any, name: str, default: Any = 0) -> Any:
    if isinstance(data, dict):
        return data.get(name, default)
    return getattr(data, name, default)


def _quality_signature(data: Any) -> tuple[int, int, int, int, int]:
    """Sort key: lossless first, then bitrate/sample rate, size last."""

    codec = str(_field(data, 'codec', '') or '').strip().casefold()
    try:
        bitrate = int(_field(data, 'bitrate', 0) or 0)
        sample_rate = int(_field(data, 'sample_rate', 0) or 0)
        channels = int(_field(data, 'channels', 0) or 0)
        size = int(_field(data, 'size', 0) or 0)
    except (TypeError, ValueError):
        bitrate = sample_rate = channels = size = 0
    return (
        1 if codec in _LOSSLESS_CODECS else 0,
        bitrate,
        sample_rate,
        channels,
        size,
    )


def _quality_label(data: Any) -> str:
    codec = str(_field(data, 'codec', '') or '').strip().upper()
    try:
        bitrate = int(_field(data, 'bitrate', 0) or 0)
        sample_rate = int(_field(data, 'sample_rate', 0) or 0)
    except (TypeError, ValueError):
        bitrate = sample_rate = 0
    parts = [codec or 'Unknown']
    if bitrate:
        parts.append(f'{round(bitrate / 1000)} kbps')
    if sample_rate:
        parts.append(f'{sample_rate / 1000:g} kHz')
    return ' · '.join(parts)


def _stored_parent_basename(stored: str) -> str:
    """The immediate folder name of a stored library path (``''`` in a root)."""

    text = str(stored or '').replace('\\', '/').rstrip('/')
    if '/' not in text:
        return ''
    parent = text.rsplit('/', 1)[0]
    return parent.rsplit('/', 1)[-1] if parent else ''


def _folder_variants(items: list[_Entry], group_base: str) -> set[str]:
    """Album-folder names that normalize to the same album.

    Tag values sometimes already agree while the folders still carry a
    year or edition suffix (``Thriller`` and ``Thriller (1982)``). Those
    are the same album and should become one folder. A folder is only
    considered when its normalized name matches the album's normalized
    title, so an artist root, a disc subfolder or a per-track folder is
    never treated as an edition of the album.
    """

    names: set[str] = set()
    for item in items:
        name = _stored_parent_basename(str(item.entry.get('file') or ''))
        if not name:
            continue
        if fold_text(base_album(name)) != group_base:
            continue
        # An artist root can have the album's name (a self-titled album);
        # it is not an album folder of its own.
        if fold_text(name) == fold_text(item.group_artist):
            continue
        names.add(name.strip())
    return names if len(names) > 1 else set()


def _duration_clusters(
    items: list[Any], duration_of: Callable[[Any], float]
) -> list[list[Any]]:
    """Split copies of one song by length, so an extended mix survives.

    Copies without a duration stay with the first cluster; they are
    almost always the same recording with missing metadata.
    """

    known = sorted(
        (item for item in items if float(duration_of(item) or 0) > 0),
        key=lambda item: float(duration_of(item) or 0),
    )
    unknown = [item for item in items if float(duration_of(item) or 0) <= 0]
    clusters: list[list[Any]] = []
    for item in known:
        duration = float(duration_of(item) or 0)
        if clusters and abs(
            duration - float(duration_of(clusters[-1][0]) or 0)
        ) <= (DURATION_TOLERANCE_SECONDS):
            clusters[-1].append(item)
        else:
            clusters.append([item])
    if unknown:
        if clusters:
            clusters[0].extend(unknown)
        else:
            clusters.append(unknown)
    return clusters


@dataclass
class _Entry:
    """A library row with its effective (possibly remote) album."""

    entry: dict[str, Any]
    album: str
    album_artist: str
    year: str
    track_number: int
    release_date: str
    genre: str
    duration: float
    song_key: str
    group_artist: str
    resolved: bool = False


def _effective_entry(
    entry: dict[str, Any], remote: Optional[dict[str, Any]]
) -> _Entry:
    remote = remote or {}
    group_artist = album_artist_of(entry)
    album = str(remote.get('album_name') or entry.get('album') or '').strip()
    album_artist = str(
        remote.get('album_artist')
        or entry.get('album_artist')
        or group_artist
        or entry.get('artist')
        or ''
    ).strip()
    return _Entry(
        entry=entry,
        album=album,
        album_artist=album_artist,
        year=str(remote.get('year') or entry.get('year') or '').strip(),
        track_number=_int_or_zero(
            remote.get('track_number') or entry.get('track_number')
        ),
        release_date=str(remote.get('release_date') or '').strip(),
        genre=str(remote.get('genre') or entry.get('genre') or '').strip(),
        duration=_float_or_zero(entry.get('duration')),
        song_key=song_key(group_artist, str(entry.get('title') or '')),
        group_artist=group_artist,
        resolved=bool(remote.get('album_name')),
    )


def _int_or_zero(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _float_or_zero(value: Any) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _artist_key(name: str) -> str:
    """Grouping key for an artist: accent/case folded, feat. removed."""

    return song_key(name, '')


def _looks_like_a_single(entry: dict[str, Any]) -> bool:
    """True when the local tags suggest the track is filed as its own EP."""

    album = str(entry.get('album') or '').strip()
    title = str(entry.get('title') or '').strip()
    if not album or is_blank_or_placeholder(album):
        return True
    if title and album.casefold() == title.casefold():
        return True
    return False


def _needs_merge(entries: list[dict[str, Any]]) -> bool:
    """Whether an artist's tracks are split enough to ask the sources.

    True when at least one track looks like a standalone single *and*
    either the artist carries more than one album label or every track is
    filed as its own single. An artist whose tracks are correctly grouped
    under one album (and nothing looks like a stray single) is left
    alone, so a healthy library costs no requests.
    """

    if len(entries) < 2:
        return False
    singles = [entry for entry in entries if _looks_like_a_single(entry)]
    if not singles:
        return False
    labels = {
        str(entry.get('album') or '').strip().casefold() for entry in entries
    }
    return len(labels) >= 2 or len(singles) >= 2


# ── Detection ─────────────────────────────────────────────────────────
def detect_split_albums(  # noqa: PLR0914
    ctx: LibraryContext,
    *,
    spotify_id_for: Optional[Callable[[str], str]] = None,
    resolver: Optional[Resolver] = None,
    lookup_limit: int = LOOKUP_LIMIT,
    stats: Optional[dict[str, Any]] = None,
    on_progress: Optional[Progress] = None,
) -> list[MergePlan]:
    """Albums that belong together, as merge plans.

    Standard/deluxe editions and duplicate copies are found from the
    local tags without a single request. Only tracks that look like
    standalone singles are looked up, and those lookups are cached and
    run in parallel.
    """

    started = time.monotonic()
    resolve = resolver or default_resolver
    _progress(on_progress, 'reading', 0, 0)
    entries = list_library_entries(ctx)

    by_artist: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        artist = album_artist_of(entry)
        if not artist or not str(entry.get('file') or '').strip():
            continue
        by_artist.setdefault(_artist_key(artist), []).append(entry)

    tasks: dict[str, tuple[dict[str, Any], str]] = {}
    entry_keys: dict[str, str] = {}
    for artist_tracks in by_artist.values():
        if len(artist_tracks) < 2 or not _needs_merge(artist_tracks):
            continue
        singles = [
            entry for entry in artist_tracks if _looks_like_a_single(entry)
        ]
        singles.sort(
            key=lambda entry: (
                str(entry.get('title') or '').casefold(),
                str(entry.get('file') or ''),
            )
        )
        for entry in singles[:lookup_limit]:
            file = str(entry.get('file') or '')
            if not file:
                continue
            spotify_id = ''
            if spotify_id_for is not None:
                try:
                    spotify_id = str(spotify_id_for(file) or '')
                except Exception:
                    logger.opt(exception=True).debug(
                        'Album merge: spotify id lookup failed for {}', file
                    )
            key = _resolution_key(song_from_entry(entry), spotify_id)
            entry_keys[file] = key
            if key not in tasks:
                tasks[key] = (song_from_entry(entry), spotify_id)

    resolved = _resolve_tasks(resolve, tasks, on_progress=on_progress)

    effective: list[_Entry] = []
    for entry in entries:
        file = str(entry.get('file') or '')
        if not str(entry.get('title') or '').strip():
            continue
        if not album_artist_of(entry):
            continue
        remote = None
        key = entry_keys.get(file)
        if key:
            remote = resolved.get(key)
        effective.append(_effective_entry(entry, remote))

    groups: dict[tuple[str, str], list[_Entry]] = {}
    for item in effective:
        if not item.album or is_blank_or_placeholder(item.album):
            continue
        artist_key = _artist_key(item.group_artist)
        album_key = fold_text(base_album(item.album))
        if not artist_key or not album_key:
            continue
        groups.setdefault((artist_key, album_key), []).append(item)

    plans: list[MergePlan] = []
    grouped = _merge_related_groups(groups)
    _progress(on_progress, 'grouping', 0, len(grouped))
    for index, items in enumerate(grouped, start=1):
        _progress(on_progress, 'grouping', index, len(grouped))
        if len(items) < 2:
            continue
        plan = _build_plan(items)
        if plan is not None:
            plans.append(plan)

    plans.sort(key=lambda plan: (-plan.track_count, plan.album.casefold()))
    elapsed = time.monotonic() - started
    if stats is not None:
        stats['tracks'] = len(entries)
        stats['groups'] = len(groups)
        stats['lookups'] = len(tasks)
        stats['elapsed_ms'] = int(elapsed * 1000)
    logger.info(
        'Album merge: {} tracks, {} album groups, {} remote lookup(s), '
        '{} candidate(s) in {:.1f}s',
        len(entries),
        len(groups),
        len(tasks),
        len(plans),
        elapsed,
    )
    return plans


def _resolve_tasks(
    resolve: Resolver,
    tasks: dict[str, tuple[dict[str, Any], str]],
    *,
    on_progress: Optional[Progress] = None,
) -> dict[str, Optional[dict[str, Any]]]:
    _progress(on_progress, 'matching', 0, len(tasks))
    if not tasks:
        return {}
    if len(tasks) == 1:
        key, (song, spotify_id) = next(iter(tasks.items()))
        result = {key: _safe_resolve(resolve, song, spotify_id)}
        _progress(on_progress, 'matching', 1, 1)
        return result

    results: dict[str, Optional[dict[str, Any]]] = {}
    workers = min(MAX_PARALLEL_LOOKUPS, len(tasks))
    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix='album-merge'
    ) as pool:
        futures = {
            pool.submit(_safe_resolve, resolve, song, spotify_id): key
            for key, (song, spotify_id) in tasks.items()
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                results[key] = future.result()
            except Exception:
                logger.opt(exception=True).debug(
                    'Album merge: tag-source lookup failed'
                )
                results[key] = None
            _progress(on_progress, 'matching', len(results), len(tasks))
    return results


def _safe_resolve(
    resolve: Resolver, song: dict[str, Any], spotify_id: str
) -> Optional[dict[str, Any]]:
    try:
        return resolve(song, spotify_id) or None
    except Exception:
        logger.opt(exception=True).debug(
            'Album merge: lookup failed for {!r}', song.get('name')
        )
        return None


def _clusters(items: list[_Entry]) -> list[list[_Entry]]:
    by_song: dict[str, list[_Entry]] = {}
    for item in items:
        by_song.setdefault(item.song_key, []).append(item)
    clusters: list[list[_Entry]] = []
    for group in by_song.values():
        clusters.extend(_duration_clusters(group, lambda item: item.duration))
    return clusters


def _album_name_tokens(name: str) -> list[str]:
    text = fold_text(base_album(name))
    cleaned = ''.join(char if char.isalnum() else ' ' for char in text)
    return [token for token in cleaned.split() if token]


#: Words that extend an album title without naming a different release.
#: Deliberately excludes ``live``, ``acoustic``, ``remix``, ``instrumental``
#: and ``karaoke``: those are separate albums, not editions.
_EDITION_EXTRA_TOKENS = frozenset({
    'deluxe',
    'expanded',
    'special',
    'bonus',
    'edition',
    'version',
    'reissue',
    'remaster',
    'remastered',
    'anniversary',
    'spilled',
    'complete',
    'collector',
    'collectors',
    'limited',
    'tour',
    'standard',
    'explicit',
    'clean',
    'cd',
    'disc',
    'disk',
})


def _related_album_names(left: str, right: str) -> bool:
    """Whether two album titles are editions of the same release family.

    Token-prefix relation only: ``GUTS`` / ``GUTS (Spilled)`` and
    ``Global Warming`` / ``Global Warming Meltdown`` are related by name,
    while ``SOUR`` / ``GUTS`` are not. The extra words have to name an
    edition (deluxe, spilled, expanded, ...), never a different release
    such as a live or remix album. Whether a related pair is really the
    same album is decided by their track overlap below.
    """

    a = _album_name_tokens(left)
    b = _album_name_tokens(right)
    if not a or not b:
        return False
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if long[: len(short)] != short:
        return False
    extra = long[len(short) :]
    if not extra:
        return True
    return all(token in _EDITION_EXTRA_TOKENS for token in extra)


def _song_keys_of(items: list[_Entry]) -> set[str]:
    return {item.song_key for item in items if item.song_key}


def _solid_song_overlap(left_keys: set[str], right_keys: set[str]) -> bool:
    """Most of the smaller edition's songs also appear in the other one."""

    if not left_keys or not right_keys:
        return False
    shared = len(left_keys & right_keys)
    return shared / min(len(left_keys), len(right_keys)) >= 0.6


def _merge_related_groups(
    groups: dict[tuple[str, str], list[_Entry]],
) -> list[list[_Entry]]:
    """Join name-related groups whose track lists largely overlap.

    ``GUTS`` and ``GUTS (Spilled)`` share almost every song, so they are
    editions of one album. ``Global Warming`` and ``Global Warming
    Meltdown`` are related by name but contain different songs, so they
    stay separate albums.
    """

    keys = list(groups)
    if len(keys) < 2:
        return list(groups.values())
    parent = {key: key for key in keys}

    def find(key: tuple[str, str]) -> tuple[str, str]:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    by_artist: dict[str, list[tuple[str, str]]] = {}
    for key in keys:
        by_artist.setdefault(key[0], []).append(key)

    keys_of = {key: _song_keys_of(groups[key]) for key in keys}
    for artist_keys in by_artist.values():
        if len(artist_keys) < 2:
            continue
        for index, left in enumerate(artist_keys):
            for right in artist_keys[index + 1 :]:
                if not _related_album_names(
                    groups[left][0].album, groups[right][0].album
                ):
                    continue
                if not _solid_song_overlap(keys_of[left], keys_of[right]):
                    continue
                parent[find(right)] = find(left)

    combined: dict[tuple[str, str], list[_Entry]] = {}
    for key in keys:
        combined.setdefault(find(key), []).extend(groups[key])
    return list(combined.values())


def _build_plan(  # noqa: PLR0914
    items: list[_Entry],
) -> Optional[MergePlan]:
    items = [
        item
        for item in items
        if item.group_artist
        and str(item.entry.get('title') or '').strip()
        and item.album
        and not is_blank_or_placeholder(item.album)
    ]
    if len(items) < 2:
        return None

    editions: dict[str, dict[str, Any]] = {}
    for item in items:
        key = fold_text(item.album)
        bucket = editions.get(key)
        if bucket is None:
            bucket = {'title': item.album.strip(), 'items': []}
            editions[key] = bucket
        bucket['items'].append(item)

    def distinct_songs(bucket: dict[str, Any]) -> int:
        return len({item.song_key for item in bucket['items']})

    target = max(
        editions.values(),
        key=lambda bucket: (
            distinct_songs(bucket),
            _edition_rank(str(bucket['title'])),
            -len(str(bucket['title'])),
            len(bucket['items']),
        ),
    )
    target_title = str(target['title']).strip()
    target_items: list[_Entry] = target['items']
    target_fold = fold_text(target_title)
    group_base = fold_text(base_album(target_title))
    folder_variants = _folder_variants(items, group_base)

    artist_counts: dict[str, int] = {}
    for item in target_items:
        artist = album_artist_of(item.entry) or item.album_artist
        if artist:
            artist_counts[artist] = artist_counts.get(artist, 0) + 1
    target_artist = (
        max(artist_counts, key=artist_counts.get)
        if artist_counts
        else (target_items[0].album_artist or target_items[0].group_artist)
    )
    if not target_artist:
        return None

    target_year = next((item.year for item in target_items if item.year), '')

    clusters = _clusters(items)
    if not clusters:
        return None

    # The winning edition's own track numbers order the merged album;
    # bonus songs that exist only in the other edition follow them.
    target_numbers: dict[int, int] = {}
    for index, cluster in enumerate(clusters):
        numbers = [
            item.track_number
            for item in cluster
            if item.track_number > 0 and fold_text(item.album) == target_fold
        ]
        if numbers:
            target_numbers[index] = min(numbers)

    kept: list[tuple[int, MergeTrack]] = []
    dropped: list[MergeTrack] = []
    for index, cluster in enumerate(clusters):
        best = max(
            cluster,
            key=lambda item: (
                _quality_signature(item.entry),
                1 if fold_text(item.album) == target_fold else 0,
                -len(str(item.entry.get('file') or '')),
            ),
        )
        best_is_target = fold_text(best.album) == target_fold
        best_track = _track_from_item(
            best,
            keep=True,
            target_edition=best_is_target,
        )
        kept.append((index, best_track))
        for item in cluster:
            if item is best:
                continue
            dropped.append(
                _track_from_item(
                    item,
                    keep=False,
                    target_edition=fold_text(item.album) == target_fold,
                    duplicate_of=str(best.entry.get('file') or ''),
                )
            )

    if not kept:
        return None

    local_tags_differ = any(
        fold_text(str(item.entry.get('album') or '').strip()) != target_fold
        for item in items
    )
    duplicate_count = len(items) - len(clusters)
    if len(clusters) < 2 and duplicate_count == 0:
        # One song with no duplicate is not an album merge.
        return None
    if not (local_tags_differ or duplicate_count > 0 or folder_variants):
        # Already one album with one copy per song. A multi-disc album
        # split over two folders is left alone rather than flattened
        # into a single folder behind the user's back.
        return None

    def order_key(pair: tuple[int, MergeTrack]) -> tuple[int, int, str]:
        index, track = pair
        source_numbers = [
            item.track_number
            for item in clusters[index]
            if item.track_number > 0
        ]
        return (
            target_numbers.get(index, 10**6),
            min(source_numbers) if source_numbers else 10**6,
            track.title.casefold(),
        )

    kept.sort(key=order_key)
    for number, (_index, track) in enumerate(kept, start=1):
        track.track_number = number
    dropped.sort(key=lambda track: (track.title.casefold(), track.file))

    edition_titles = [target_title]
    for title in sorted(
        [
            str(bucket['title']).strip()
            for key, bucket in editions.items()
            if key != target_fold
        ]
        + sorted(folder_variants),
        key=str.casefold,
    ):
        if fold_text(title) not in {
            fold_text(existing) for existing in edition_titles
        }:
            edition_titles.append(title)
    return MergePlan(
        album=target_title,
        album_artist=target_artist,
        year=target_year,
        tracks=[track for _index, track in kept] + dropped,
        editions=edition_titles,
    )


def _track_from_item(
    item: _Entry,
    *,
    keep: bool,
    target_edition: bool,
    duplicate_of: str = '',
) -> MergeTrack:
    entry = item.entry
    return MergeTrack(
        file=str(entry.get('file') or ''),
        title=str(entry.get('title') or ''),
        current_album=str(entry.get('album') or ''),
        track_number=item.track_number,
        release_date=item.release_date or item.year,
        genre=item.genre,
        keep=keep,
        quality=_quality_label(entry),
        codec=str(entry.get('codec') or ''),
        bitrate=_int_or_zero(entry.get('bitrate')),
        sample_rate=_int_or_zero(entry.get('sample_rate')),
        channels=_int_or_zero(entry.get('channels')),
        size=_int_or_zero(entry.get('size')),
        duration=item.duration,
        target_edition=target_edition,
        duplicate_of=duplicate_of,
    )


# ── Applying a merge ──────────────────────────────────────────────────
def _plan_for_track(plan: MergePlan, track: MergeTrack) -> dict[str, Any]:
    return {
        'name': track.title,
        'artists': [plan.album_artist] if plan.album_artist else [],
        'artist': plan.album_artist,
        'album_name': plan.album,
        'album_artist': plan.album_artist,
        'year': plan.year,
        'release_date': track.release_date or plan.year,
        'track_number': track.track_number,
        'genre': track.genre,
    }


def _unique_path(target: Path) -> Path:
    """*target*, or a `` (2)``/`` (3)`` variant when it already exists."""

    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for n in range(2, 1000):
        candidate = target.with_name(f'{stem} ({n}){suffix}')
        if not candidate.exists():
            return candidate
    return target


def _move(source: Path, target: Path) -> Optional[Path]:
    """Move *source* to *target*; return the path it ended up at."""

    if source == target:
        return source
    try:
        stat = source.stat()
    except OSError:
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    target = _unique_path(target)
    try:
        os.replace(str(source), str(target))
    except OSError:
        try:
            shutil.move(str(source), str(target))
        except OSError:
            logger.opt(exception=True).warning(
                'Album merge: could not move {}', source
            )
            return None
    try:
        os.utime(str(target), ns=(stat.st_atime_ns, stat.st_mtime_ns))
    except OSError:
        pass
    return target


def _cover_bytes(song: dict[str, Any], path: Path) -> Optional[bytes]:
    existing = cover_sources.current_cover(path)
    better = cover_sources.best_cover(
        song,
        current=existing,
        preference=cover_sources.PREFERENCE_HIGHEST,
    )
    if better is not None:
        return better.data
    if existing is not None:
        return existing.data
    fallback = cover_sources.default_cover()
    return fallback.data if fallback is not None else None


def _track_priority(track: MergeTrack) -> tuple[Any, ...]:
    return (
        track.quality_signature(),
        1 if track.target_edition else 0,
        -len(track.file or ''),
    )


def _cluster_tracks(
    tracks: list[MergeTrack], album_artist: str
) -> list[list[MergeTrack]]:
    by_song: dict[str, list[MergeTrack]] = {}
    for track in tracks:
        by_song.setdefault(song_key(album_artist, track.title), []).append(
            track
        )
    clusters: list[list[MergeTrack]] = []
    for group in by_song.values():
        clusters.extend(
            _duration_clusters(group, lambda track: track.duration)
        )
    return clusters


def _refresh_track_facts(track: MergeTrack, source: Path) -> None:
    """Re-read the actual file so the best copy is chosen from disk.

    The plan arrives from the browser, so its quality fields cannot be
    the only basis for deleting a file. Reading the real title, length
    and format keeps the choice correct even for a stale or edited
    preview.
    """

    try:
        meta = read_audio_metadata(source)
    except Exception:
        logger.opt(exception=True).debug(
            'Album merge: could not re-read {}', source
        )
        return
    if not isinstance(meta, dict):
        return
    title = str(meta.get('title') or '').strip()
    if title:
        track.title = title
    album = str(meta.get('album') or '').strip()
    if album:
        track.current_album = album
    # Overwrite the client's quality fields with what the file really
    # is, zeros included: only the disk decides which copy wins.
    track.codec = str(meta.get('codec') or '').strip()
    track.bitrate = _int_or_zero(meta.get('bitrate'))
    track.sample_rate = _int_or_zero(meta.get('sample_rate'))
    track.channels = _int_or_zero(meta.get('channels'))
    track.duration = _float_or_zero(meta.get('duration'))
    try:
        track.size = source.stat().st_size
    except OSError:
        pass
    track.quality = _quality_label(track)


def _move_sidecar(source: Path, target: Path) -> None:
    """Move an existing ``.lrc`` with its audio, so the old folder empties.

    The destination honours the configured lyrics location; duplicate
    sidecars left in old locations are removed afterwards.
    """

    try:
        for sidecar in lyrics_mod.lrc_candidates(source):
            if not sidecar.is_file():
                continue
            destination = lyrics_mod.lrc_path_for(target)
            if sidecar.resolve() != destination.resolve():
                destination.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.replace(str(sidecar), str(destination))
                except OSError:
                    shutil.move(str(sidecar), str(destination))
            break
    except Exception:
        logger.opt(exception=True).debug(
            'Album merge: could not move lyrics for {}', source
        )
    library_cleanup.delete_lrc_sidecar(source)


def _prune_after_move(
    stored: str,
    source: Path,
    ctx: LibraryContext,
    download_dir: Path,
    slskd_dir: Optional[Path],
) -> None:
    try:
        root, _relative = library_file_root(
            stored, download_dir, slskd_dir, ctx.extra_dirs
        )
        library_cleanup.delete_album_cover_if_orphaned(source)
        library_cleanup.prune_empty_parent_dirs(source.parent, root)
    except Exception:
        logger.opt(exception=True).debug(
            'Album merge: pruning {} failed', source.parent
        )


def apply_merge(  # noqa: PLR0914
    plans: list[dict[str, Any]],
    *,
    ctx: LibraryContext,
    downloader: Any,
    settings: Optional[dict[str, Any]] = None,
    monitor_db: Any = None,
    on_progress: Optional[Progress] = None,
) -> dict[str, Any]:
    """Retag and move every kept track; delete the duplicate copies.

    The best-quality copy of each song is always kept. A duplicate is
    only deleted after its kept copy has been moved into the album
    folder successfully, so a failed move never loses the last copy.
    Returns ``{"merged", "moved", "deleted", "reclaimed_bytes",
    "failed", "playlists"}``.
    """

    download_dir = Path(ctx.download_dir)
    slskd_dir = ctx.slskd_dir
    if downloader is not None:
        slskd_dir = slskd_dir or slskd_dir_from_downloader(downloader)
    affected: set[str] = set()
    moved = 0
    deleted = 0
    reclaimed_bytes = 0
    merged = 0
    failed: list[str] = []
    coerced = [plan for raw in plans if (plan := _coerce_plan(raw))]
    total = sum(len(plan.tracks) for plan in coerced)
    completed = 0
    _progress(on_progress, 'merging', completed, total)

    for plan in coerced:
        sources: dict[str, Optional[Path]] = {}
        found: list[tuple[MergeTrack, str, Optional[Path]]] = []
        for track in plan.tracks:
            stored = str(track.file or '').strip()
            if not stored:
                continue
            source = _resolve(stored, ctx)
            if source is not None:
                _refresh_track_facts(track, source)
            sources[track.file] = source
            found.append((track, stored, source))
        if not found:
            continue

        # Recompute the best copy server-side. This also protects a
        # client that marked a unique track as a duplicate: a song with
        # no other copy can never be deleted.
        drop_to_keep: dict[str, str] = {}
        for cluster in _cluster_tracks(plan.tracks, plan.album_artist):
            available = [
                track
                for track in cluster
                if sources.get(track.file) is not None
            ]
            if not available:
                continue
            best = max(available, key=_track_priority)
            for track in cluster:
                track.keep = track.file == best.file
                if track.file != best.file:
                    drop_to_keep[track.file] = best.file

        moved_targets: dict[str, Path] = {}
        plan_moved = 0
        for track, stored, source in found:
            if not track.keep:
                continue
            _progress(on_progress, 'merging', completed, total)
            completed += 1
            if source is None:
                failed.append(stored)
                continue
            try:
                song = _plan_for_track(plan, track)
                target_dir, basename = _target_location(
                    song, downloader, download_dir
                )
                target = target_dir / f'{basename}{source.suffix or ".mp3"}'
                cover = _cover_bytes(song, source)
                final = _move(source, target)
                if final is None:
                    failed.append(stored)
                    continue
                _move_sidecar(source, final)
                try:
                    embed_metadata(
                        final,
                        song,
                        download_cover=True,
                        cover_bytes=cover,
                    )
                    if cover:
                        album_covers.write_cover(final.parent, cover)
                except Exception:
                    logger.opt(exception=True).warning(
                        'Album merge: tagging {} failed', final
                    )
                moved += 1
                plan_moved += 1
                moved_targets[track.file] = final
                _relink(
                    stored,
                    final,
                    ctx,
                    download_dir,
                    slskd_dir,
                    affected,
                    monitor_db,
                )
                _prune_after_move(stored, source, ctx, download_dir, slskd_dir)
            except Exception:
                logger.opt(exception=True).warning(
                    'Album merge: {} failed', stored
                )
                failed.append(stored)

        plan_deleted = 0
        for track, stored, source in found:
            if track.keep:
                continue
            _progress(on_progress, 'merging', completed, total)
            completed += 1
            if source is None:
                continue
            kept_file = drop_to_keep.get(track.file)
            kept_target = moved_targets.get(kept_file) if kept_file else None
            if kept_target is None or not kept_target.exists():
                logger.warning(
                    'Album merge: keeping {} because no kept copy exists',
                    stored,
                )
                continue
            try:
                if source.resolve() == kept_target.resolve():
                    continue
                size = source.stat().st_size
                # Point playlists and watches at the kept copy before
                # the duplicate disappears.
                _relink(
                    stored,
                    kept_target,
                    ctx,
                    download_dir,
                    slskd_dir,
                    affected,
                    monitor_db,
                )
                source.unlink()
                deleted += 1
                plan_deleted += 1
                reclaimed_bytes += size
                root, _relative = library_file_root(
                    stored, download_dir, slskd_dir, ctx.extra_dirs
                )
                library_cleanup.remove_track_leftovers(source, root)
            except OSError:
                logger.opt(exception=True).warning(
                    'Album merge: could not delete duplicate {}', stored
                )
                failed.append(stored)

        if plan_moved or plan_deleted:
            merged += 1

    _progress(on_progress, 'merging', total, total)
    folders_removed = prune_empty_folders(ctx)
    invalidate_library_paths_cache(drop_entries=True)
    return {
        'merged': merged,
        'moved': moved,
        'deleted': deleted,
        'reclaimed_bytes': reclaimed_bytes,
        'folders_removed': folders_removed,
        'failed': failed,
        'playlists': sorted(affected),
    }


def _coerce_plan(raw: Any) -> Optional[MergePlan]:
    if isinstance(raw, MergePlan):
        return raw
    if not isinstance(raw, dict):
        return None
    album = str(raw.get('album') or '').strip()
    artist = str(raw.get('album_artist') or '').strip()
    if not album or not artist:
        return None
    tracks = [
        MergeTrack(
            file=str(item.get('file') or ''),
            title=str(item.get('title') or ''),
            current_album=str(item.get('current_album') or ''),
            track_number=_int_or_zero(item.get('track_number')),
            release_date=str(item.get('release_date') or ''),
            genre=str(item.get('genre') or ''),
            keep=bool(item.get('keep', True)),
            quality=str(item.get('quality') or ''),
            codec=str(item.get('codec') or ''),
            bitrate=_int_or_zero(item.get('bitrate')),
            sample_rate=_int_or_zero(item.get('sample_rate')),
            channels=_int_or_zero(item.get('channels')),
            size=_int_or_zero(item.get('size')),
            duration=_float_or_zero(item.get('duration')),
            target_edition=bool(item.get('target_edition')),
            duplicate_of=str(item.get('duplicate_of') or ''),
        )
        for item in raw.get('tracks') or []
        if isinstance(item, dict) and item.get('file')
    ]
    if not tracks:
        return None
    editions = [
        str(title).strip()
        for title in (raw.get('editions') or [])
        if str(title).strip()
    ]
    return MergePlan(
        album=album,
        album_artist=artist,
        year=str(raw.get('year') or ''),
        tracks=tracks,
        editions=editions,
    )


def _resolve(stored: str, ctx: LibraryContext) -> Optional[Path]:
    return locate_library_file(
        stored, ctx.download_dir, ctx.slskd_dir, ctx.extra_dirs
    )


# ── Unknown folders, leftovers ───────────────────────────────────────
_UNKNOWN_FOLDER_NAMES = frozenset({
    'unknown',
    'unknown album',
    'unknown artist',
    'unknowns',
    'unsorted',
})


def _is_unknown_album(value: Any) -> bool:
    text = str(value or '').strip()
    return is_blank_or_placeholder(text) or fold_text(text) in (
        _UNKNOWN_FOLDER_NAMES
    )


def _under_unknown_folder(stored: str) -> bool:
    parts = re.split(r'[\\/]+', str(stored or ''))
    return any(
        fold_text(part) in _UNKNOWN_FOLDER_NAMES for part in parts[:-1] if part
    )


def _song_with_remote(
    song: dict[str, Any], remote: dict[str, Any]
) -> dict[str, Any]:
    merged = dict(song)
    for key in (
        'album_name',
        'album_artist',
        'year',
        'release_date',
        'genre',
        'track_number',
    ):
        value = remote.get(key)
        if value not in {None, '', 0}:
            merged[key] = value
    if not str(merged.get('album_artist') or '').strip():
        artists = merged.get('artists') or []
        merged['album_artist'] = str(artists[0] if artists else '')
    return merged


def _local_album_index(
    entries: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """``artist|title`` → a library row whose album is already known.

    Resolving an unknown track against this index costs no network
    request and handles the common case where the song also exists in a
    correctly tagged copy (another album, playlist or download).
    """

    index: dict[str, dict[str, Any]] = {}
    for entry in entries:
        album = str(entry.get('album') or '').strip()
        if _is_unknown_album(album):
            continue
        artist = album_artist_of(entry)
        title = str(entry.get('title') or '').strip()
        if not artist or not title:
            continue
        key = song_key(artist, title)
        if key != '|':
            index.setdefault(key, entry)
    return index


def rehome_unknown_tracks(  # noqa: PLR0914
    ctx: LibraryContext,
    *,
    downloader: Any,
    spotify_id_for: Optional[Callable[[str], str]] = None,
    resolver: Optional[Resolver] = None,
    monitor_db: Any = None,
    on_progress: Optional[Progress] = None,
    limit: int = 500,
) -> dict[str, Any]:
    """Move tracks out of ``unknown`` folders into their real album.

    Files that were downloaded without album metadata end up under an
    ``unknown`` folder (or carry an ``unknown`` album tag). The album is
    resolved from the track's Spotify id, Deezer or iTunes, the file is
    moved into the normal output-template folder and retagged. A copy
    that already exists at the destination wins; the unknown copy is
    then removed instead of creating a ``(2)`` file.
    """

    resolve = resolver or default_resolver
    download_dir = Path(ctx.download_dir)
    slskd_dir = ctx.slskd_dir
    if downloader is not None:
        slskd_dir = slskd_dir or slskd_dir_from_downloader(downloader)

    entries = list_library_entries(ctx)
    local_index = _local_album_index(entries)
    targets = [
        entry
        for entry in entries
        if str(entry.get('file') or '').strip()
        and (
            _is_unknown_album(entry.get('album'))
            or _under_unknown_folder(str(entry.get('file') or ''))
        )
    ]
    total = min(len(targets), max(0, int(limit or 0)))
    rehomed = 0
    duplicates = 0
    unresolved = 0
    affected: set[str] = set()
    failed: list[str] = []
    _progress(on_progress, 'rehoming', 0, total)

    for index, entry in enumerate(targets[:total], start=1):
        _progress(on_progress, 'rehoming', index, total)
        stored = str(entry.get('file') or '')
        source = _resolve(stored, ctx)
        if source is None:
            continue
        song = song_from_entry(entry)
        local = local_index.get(song_key(album_artist_of(entry), song['name']))
        if local is not None:
            found = {
                'album_name': str(local.get('album') or ''),
                'album_artist': str(local.get('album_artist') or ''),
                'year': str(local.get('year') or ''),
                'track_number': int(local.get('track_number') or 0),
                'genre': str(local.get('genre') or ''),
            }
        else:
            spotify_id = ''
            if spotify_id_for is not None:
                try:
                    spotify_id = str(spotify_id_for(stored) or '')
                except Exception:
                    logger.opt(exception=True).debug(
                        'Unknown album: id lookup failed for {}', stored
                    )
            found = _safe_resolve(resolve, song, spotify_id) or {}
        album = str(found.get('album_name') or '').strip()
        if _is_unknown_album(album):
            unresolved += 1
            continue
        merged = _song_with_remote(song, found)
        if not str(merged.get('album_artist') or '').strip():
            continue
        try:
            target_dir, basename = _target_location(
                merged, downloader, download_dir
            )
            target = target_dir / f'{basename}{source.suffix or ".mp3"}'
            if target.resolve() == source.resolve():
                # Already in the right folder; only refresh the tags.
                embed_metadata(
                    target,
                    merged,
                    download_cover=False,
                    cover_bytes=None,
                )
                continue
            if target.exists():
                source_meta = read_audio_metadata(source)
                target_meta = read_audio_metadata(target)
                source_duration = _float_or_zero(source_meta.get('duration'))
                target_duration = _float_or_zero(target_meta.get('duration'))
                if (
                    not source_duration
                    or not target_duration
                    or abs(source_duration - target_duration)
                    <= DURATION_TOLERANCE_SECONDS
                ):
                    _relink(
                        stored,
                        target,
                        ctx,
                        download_dir,
                        slskd_dir,
                        affected,
                        monitor_db,
                    )
                    root, _relative = library_file_root(
                        stored,
                        download_dir,
                        slskd_dir,
                        ctx.extra_dirs,
                    )
                    source.unlink()
                    library_cleanup.remove_track_leftovers(source, root)
                    duplicates += 1
                    continue
            cover = _cover_bytes(merged, source)
            final = _move(source, target)
            if final is None:
                failed.append(stored)
                continue
            _move_sidecar(source, final)
            try:
                embed_metadata(
                    final,
                    merged,
                    download_cover=True,
                    cover_bytes=cover,
                )
                if cover:
                    album_covers.write_cover(final.parent, cover)
            except Exception:
                logger.opt(exception=True).warning(
                    'Unknown album: tagging {} failed', final
                )
            _relink(
                stored,
                final,
                ctx,
                download_dir,
                slskd_dir,
                affected,
                monitor_db,
            )
            _prune_after_move(stored, source, ctx, download_dir, slskd_dir)
            rehomed += 1
        except Exception:
            logger.opt(exception=True).warning(
                'Unknown album: could not rehome {}', stored
            )
            failed.append(stored)

    _progress(on_progress, 'rehoming', total, total)
    return {
        'rehomed': rehomed,
        'duplicates_removed': duplicates,
        'unresolved': unresolved,
        'failed': failed,
        'playlists': sorted(affected),
    }


def prune_empty_folders(ctx: LibraryContext) -> int:
    """Delete album folders that no longer hold audio.

    A folder whose only remaining files are cover art, lyrics or SABnzbd
    leftovers is empty for this purpose: those files are removed and the
    folder goes too. Folders with audio, or with a subfolder that still
    has some, are left alone.
    """

    leftovers = frozenset({
        'cover.jpg',
        'folder.jpg',
        'front.jpg',
        'album.jpg',
        'cover.png',
        'folder.png',
        'front.png',
        'thumbs.db',
        '.ds_store',
    })
    roots = [Path(ctx.download_dir), *ctx.extra_dirs]
    removed = 0
    for candidate_root in roots:
        try:
            root = candidate_root.resolve()
        except OSError:
            continue
        if not root.is_dir():
            continue
        for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
            path = Path(dirpath)
            if path == root or path.name.startswith('.'):
                continue
            try:
                entries = list(path.iterdir())
            except OSError:
                continue
            has_audio = any(
                entry.is_file()
                and entry.suffix.lower().lstrip('.') in _AUDIO_EXTENSIONS
                for entry in entries
            )
            has_content = any(
                entry.is_dir()
                and not entry.name.startswith('.')
                and any(entry.iterdir())
                for entry in entries
            )
            if has_audio or has_content:
                continue
            for entry in entries:
                if not entry.is_file():
                    continue
                name = entry.name.casefold()
                if name in leftovers or entry.suffix.lower() in {
                    '.lrc',
                    '.jpg',
                    '.jpeg',
                    '.png',
                    '.nfo',
                }:
                    try:
                        entry.unlink()
                    except OSError:
                        pass
            try:
                if not any(path.iterdir()):
                    path.rmdir()
                    removed += 1
            except OSError:
                continue
    return removed


def tidy_library(
    ctx: LibraryContext,
    *,
    downloader: Any,
    spotify_id_for: Optional[Callable[[str], str]] = None,
    resolver: Optional[Resolver] = None,
    monitor_db: Any = None,
    on_progress: Optional[Progress] = None,
    limit: int = 500,
) -> dict[str, Any]:
    """Rehome ``unknown`` tracks, then remove empty album folders."""

    result = rehome_unknown_tracks(
        ctx,
        downloader=downloader,
        spotify_id_for=spotify_id_for,
        resolver=resolver,
        monitor_db=monitor_db,
        on_progress=on_progress,
        limit=limit,
    )
    result['folders_removed'] = prune_empty_folders(ctx)
    invalidate_library_paths_cache(drop_entries=True)
    return result


def _target_location(
    song: dict[str, Any], downloader: Any, download_dir: Path
) -> tuple[Path, str]:
    album_artist = _sanitize(str(song.get('album_artist') or 'unknown'))
    album = _sanitize(str(song.get('album_name') or 'unknown'))
    fallback = f'{album_artist}/{album}'
    if downloader is not None:
        try:
            target_dir, _prefix, basename = downloader._target_location(
                song, fallback
            )
            return target_dir, basename
        except Exception:
            logger.opt(exception=True).debug(
                'Album merge: output template failed; using album folder'
            )
    title = _sanitize(str(song.get('name') or 'unknown'))
    number = int(song.get('track_number') or 0)
    basename = f'{number:02d} - {title}' if number else title
    return download_dir / fallback, basename


def _relink(
    old_stored: str,
    target: Path,
    ctx: LibraryContext,
    download_dir: Path,
    slskd_dir: Optional[Path],
    affected: set[str],
    monitor_db: Any = None,
) -> None:
    """Point the library stores at a moved track's new path."""

    new_stored = library_stored_path(
        target, download_dir, slskd_dir, ctx.extra_dirs
    )
    if not new_stored or new_stored == old_stored:
        return
    try:
        if ctx.track_index is not None:
            tid = ctx.track_index.spotify_id_for_filename(old_stored)
            if tid:
                ctx.track_index.update_filename(tid, new_stored)
        if ctx.playlist_catalog is not None:
            affected.update(
                ctx.playlist_catalog.update_filename(old_stored, new_stored)
            )
        if monitor_db is not None:
            affected.update(
                monitor_db.update_filename_for_spotify(
                    old_stored, new_stored, by_filename=True
                )
            )
    except Exception:
        logger.opt(exception=True).debug(
            'Album merge: relinking {} failed', old_stored
        )
