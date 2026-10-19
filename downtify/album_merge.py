"""Merge an album a library holds as separate singles back into one album.

A track pulled from a release page that only lists the single it was
published on — or one downloaded one song at a time — often carries the
song's own title as its *album* tag, so a player shows a dozen
one-track albums instead of the album they all belong to. The tag
sources know better; this module asks them which release each track is
really on, groups the tracks that agree, and (in :func:`apply_merge`)
rewrites their tags and moves them into one album folder.

Detection never writes anything: it returns a plan the caller shows for
confirmation, exactly like a library upgrade's scan. Audio is not
replaced — only the tags are rewritten and the file is moved — so this
reuses :func:`downtify.downloader.embed_metadata`.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from loguru import logger

from . import album_covers, cover_sources, deezer, itunes, spotify
from .downloader import _sanitize, embed_metadata
from .library_catalog import (
    LibraryContext,
    album_artist_of,
    is_blank_or_placeholder,
    list_library_entries,
    song_from_entry,
)
from .library_paths import (
    library_stored_path,
    locate_library_file,
    slskd_dir_from_downloader,
)
from .library_paths_cache import invalidate_library_paths_cache

#: How many tracks of one artist are looked up in a single detection
#: pass. A source lookup each, so this bounds a mistake (a huge artist
#: folder) without truncating any realistic album.
LOOKUP_LIMIT = 120


@dataclass
class MergeTrack:
    """One track of a proposed merge, as the preview shows it."""

    file: str
    title: str
    current_album: str
    track_number: int = 0
    release_date: str = ''
    genre: str = ''

    def to_dict(self) -> dict[str, Any]:
        return {
            'file': self.file,
            'title': self.title,
            'current_album': self.current_album,
            'track_number': self.track_number,
            'release_date': self.release_date,
            'genre': self.genre,
        }


@dataclass
class MergePlan:
    """One album to merge, with its tracks in the order to write them."""

    album: str
    album_artist: str
    year: str = ''
    tracks: list[MergeTrack] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            'album': self.album,
            'album_artist': self.album_artist,
            'year': self.year,
            'track_count': len(self.tracks),
            'tracks': [track.to_dict() for track in self.tracks],
        }


#: Resolver signature: ``(song, spotify_id) -> remote song dict or None``.
Resolver = Callable[[dict[str, Any], str], Optional[dict[str, Any]]]


def default_resolver(
    song: dict[str, Any], spotify_id: str
) -> Optional[dict[str, Any]]:
    """Which album a track is on, per Spotify, iTunes or Deezer.

    Spotify first when the track id is known (most precise), then Apple
    Music (iTunes) by artist and title, then Deezer. The iTunes endpoint
    rate-limits hard — a merge scan asks it one question per track — so
    Deezer is a real second source, not a nicety: without it a burst of
    403s would silently drop most candidates.
    """

    if spotify_id:
        try:
            found = spotify.track_from_id(spotify_id, with_album=True)
            if found and found.get('album_name'):
                return found
        except Exception:
            logger.opt(exception=True).debug(
                'Album merge: Spotify lookup failed for {}', spotify_id
            )
    for source, lookup in (
        ('iTunes', itunes.lookup_track_metadata),
        ('Deezer', deezer.lookup_track_metadata),
    ):
        try:
            found = lookup(song)
        except Exception:
            logger.opt(exception=True).debug(
                'Album merge: {} lookup failed for {!r}',
                source,
                song.get('name'),
            )
            found = None
        if found and found.get('album_name'):
            return found
    return None


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


def detect_split_albums(
    ctx: LibraryContext,
    *,
    spotify_id_for: Optional[Callable[[str], str]] = None,
    resolver: Optional[Resolver] = None,
    lookup_limit: int = LOOKUP_LIMIT,
) -> list[MergePlan]:
    """Albums the tag sources say belong together, as merge plans.

    Only artists whose tracks are currently split over several album
    tags — with at least one that looks like a standalone single — are
    looked up, so a healthy library costs no requests at all.
    """

    resolve = resolver or default_resolver
    entries = list_library_entries(ctx)

    by_artist: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        artist = album_artist_of(entry)
        if artist:
            by_artist.setdefault(artist.casefold(), []).append(entry)

    plans: list[MergePlan] = []
    for artist_tracks in by_artist.values():
        if len(artist_tracks) < 2 or not _needs_merge(artist_tracks):
            continue
        plans.extend(
            _plans_for_artist(
                artist_tracks,
                spotify_id_for=spotify_id_for,
                resolve=resolve,
                lookup_limit=lookup_limit,
            )
        )
    plans.sort(key=lambda plan: (-len(plan.tracks), plan.album.casefold()))
    return plans


def _plans_for_artist(
    entries: list[dict[str, Any]],
    *,
    spotify_id_for: Optional[Callable[[str], str]],
    resolve: Resolver,
    lookup_limit: int,
) -> list[MergePlan]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in entries[:lookup_limit]:
        song = song_from_entry(entry)
        spotify_id = ''
        if spotify_id_for is not None:
            spotify_id = str(
                spotify_id_for(str(entry.get('file') or '')) or ''
            )
        remote = resolve(song, spotify_id) or {}
        album = str(remote.get('album_name') or '').strip()
        if not album:
            continue
        artist = str(
            remote.get('album_artist') or album_artist_of(entry)
        ).strip() or album_artist_of(entry)
        key = (artist.casefold(), album.casefold())
        bucket = grouped.setdefault(
            key,
            {'album': album, 'album_artist': artist, 'year': '', 'tracks': []},
        )
        if not bucket['year'] and remote.get('year'):
            bucket['year'] = str(remote.get('year'))
        bucket['tracks'].append((entry, remote))

    plans: list[MergePlan] = []
    for bucket in grouped.values():
        members = bucket['tracks']
        if len(members) < 2:
            continue
        target = str(bucket['album']).strip().casefold()
        if all(
            str(entry.get('album') or '').strip().casefold() == target
            for entry, _remote in members
        ):
            # Every track already agrees on this album tag: nothing to
            # merge (a folder move alone is out of scope here).
            continue
        members.sort(
            key=lambda pair: (
                int(pair[1].get('track_number') or 0),
                str(pair[0].get('title') or '').casefold(),
            )
        )
        plan = MergePlan(
            album=str(bucket['album']),
            album_artist=str(bucket['album_artist']),
            year=str(bucket['year'] or ''),
        )
        for entry, remote in members:
            plan.tracks.append(
                MergeTrack(
                    file=str(entry.get('file') or ''),
                    title=str(entry.get('title') or ''),
                    current_album=str(entry.get('album') or ''),
                    track_number=int(remote.get('track_number') or 0),
                    release_date=str(remote.get('release_date') or ''),
                    genre=str(remote.get('genre') or ''),
                )
            )
        plans.append(plan)
    return plans


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


def _move(source: Path, target: Path) -> bool:
    if source == target:
        return True
    try:
        stat = source.stat()
    except OSError:
        return False
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
            return False
    try:
        os.utime(str(target), ns=(stat.st_atime_ns, stat.st_mtime_ns))
    except OSError:
        pass
    return True


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


def apply_merge(
    plans: list[dict[str, Any]],
    *,
    ctx: LibraryContext,
    downloader: Any,
    settings: Optional[dict[str, Any]] = None,
    monitor_db: Any = None,
) -> dict[str, Any]:
    """Retag and move every track of *plans* into its album folder.

    One track that fails never aborts the rest: it is logged and listed
    in the report's ``failed``. Returns ``{"merged", "moved", "failed"}``.
    """

    download_dir = Path(ctx.download_dir)
    slskd_dir = ctx.slskd_dir
    if downloader is not None:
        slskd_dir = slskd_dir or slskd_dir_from_downloader(downloader)
    affected: set[str] = set()
    moved = 0
    failed: list[str] = []

    for raw in plans:
        plan = _coerce_plan(raw)
        if plan is None:
            continue
        for track in plan.tracks:
            stored = str(track.file or '').strip()
            source = _resolve(stored, ctx)
            if source is None:
                failed.append(stored)
                continue
            song = _plan_for_track(plan, track)
            suffix = source.suffix or '.mp3'
            try:
                target_dir, basename = _target_location(
                    song, downloader, download_dir
                )
                target = target_dir / f'{basename}{suffix}'
                cover = _cover_bytes(song, source)
                if not _move(source, target):
                    failed.append(stored)
                    continue
                try:
                    embed_metadata(
                        target,
                        song,
                        download_cover=True,
                        cover_bytes=cover,
                    )
                    if cover:
                        album_covers.write_cover(target.parent, cover)
                except Exception:
                    logger.opt(exception=True).warning(
                        'Album merge: tagging {} failed', target
                    )
                moved += 1
                _relink(
                    stored,
                    target,
                    ctx,
                    download_dir,
                    slskd_dir,
                    affected,
                    monitor_db,
                )
            except Exception:
                logger.opt(exception=True).warning(
                    'Album merge: {} failed', stored
                )
                failed.append(stored)

    invalidate_library_paths_cache()
    return {
        'merged': len([p for p in plans if p]),
        'moved': moved,
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
            track_number=int(item.get('track_number') or 0),
            release_date=str(item.get('release_date') or ''),
            genre=str(item.get('genre') or ''),
        )
        for item in raw.get('tracks') or []
        if isinstance(item, dict) and item.get('file')
    ]
    if not tracks:
        return None
    return MergePlan(
        album=album,
        album_artist=artist,
        year=str(raw.get('year') or ''),
        tracks=tracks,
    )


def _resolve(stored: str, ctx: LibraryContext) -> Optional[Path]:
    return locate_library_file(
        stored, ctx.download_dir, ctx.slskd_dir, ctx.extra_dirs
    )


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
            affected.update(monitor_db.update_filename(old_stored, new_stored))
    except Exception:
        logger.opt(exception=True).debug(
            'Album merge: relinking {} failed', old_stored
        )
