"""Find and write the cover image of an album folder.

Media servers such as Navidrome or Jellyfin show a cover for an album a
folder holds only when that folder contains an image — or, for clients
that read them, embedded ones. A library built by an older Downtify (or
downloaded with *organize by album* off) often has artwork embedded in
every track but no image file next to them, which is exactly the "album
art is empty although the tracks have covers" case.

Nothing here writes to an audio file; it only reads an existing cover
out of one and drops the bytes into the folder as a sidecar.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

from loguru import logger

from .library_catalog import IMAGE_EXTENSIONS

#: Stems media servers look for, in the order they prefer them. Matching
#: is case-insensitive and extension-agnostic (``Cover.JPG`` counts).
COVER_STEMS = ('cover', 'folder', 'front', 'album', 'albumart', 'thumb')

#: What a written cover is called. ``cover.jpg`` mirrors the name the
#: downloader already uses (see ``downloader.Downloader._save_album_cover``
#: and ``library_cleanup``); PNG bytes keep their extension so the file
#: isn't a lie, and both are recognized by the servers above.
COVER_FILENAME = 'cover.jpg'
COVER_FILENAME_PNG = 'cover.png'


def find_cover(folder: Path) -> Optional[Path]:
    """An existing cover image directly inside *folder*, or ``None``.

    Only the folder's own files are considered — a recursive search would
    let a parent's artwork count for every sub-album, which is not what
    "this album has a cover" means.
    """

    try:
        entries = list(folder.iterdir())
    except OSError:
        return None
    by_stem: dict[str, Path] = {}
    for path in entries:
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        if path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        by_stem.setdefault(path.stem.casefold(), path)
    for stem in COVER_STEMS:
        hit = by_stem.get(stem)
        if hit is not None:
            return hit
    return None


def has_cover(folder: Path) -> bool:
    return find_cover(folder) is not None


def iter_audio_files(folder: Path) -> Iterable[Path]:
    """Audio files directly inside *folder*, for cover extraction."""

    try:
        entries = sorted(folder.iterdir())
    except OSError:
        return ()
    from .library_catalog import AUDIO_EXTENSIONS  # noqa: PLC0415  (cycle)

    found: list[Path] = []
    for path in entries:
        try:
            if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS:
                found.append(path)
        except OSError:
            continue
    return found


def _is_png(data: bytes) -> bool:
    return data[:8] == b'\x89PNG\r\n\x1a\n'


def write_cover(folder: Path, data: bytes) -> Optional[Path]:
    """Write cover bytes into *folder*; return the path, or ``None``.

    An existing cover under the chosen name is overwritten — the caller
    only reaches this with a better or the first image.
    """

    if not data:
        return None
    name = COVER_FILENAME_PNG if _is_png(data) else COVER_FILENAME
    # Never leave the other name behind as a smaller stale cover.
    other = COVER_FILENAME if name != COVER_FILENAME else COVER_FILENAME_PNG
    target = folder / name
    try:
        folder.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        (folder / other).unlink(missing_ok=True)
    except OSError:
        logger.opt(exception=True).warning(
            'Could not write album cover {}', target
        )
        return None
    return target
