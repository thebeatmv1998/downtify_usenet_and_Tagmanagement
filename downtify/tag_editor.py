"""Read and write the full set of embedded tags a library track carries.

The player and library only need a handful of fields, and
:mod:`downtify.downloader` writes a fixed set at download time. A tag
editor needs both directions for everything a user may want to fix — the
title, artist, album, album artist, track/disc position, date, genre,
comment, composer — plus the embedded cover, across MP3 (ID3),
M4A/MP4, FLAC and Ogg Vorbis/Opus.

Nothing here touches the audio stream: it rewrites the tag blocks in
place, the same way a desktop tagger does. An empty field is removed
rather than written as an empty frame.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Callable, Optional

from loguru import logger
from mutagen.flac import FLAC, Picture
from mutagen.id3 import (
    APIC,
    COMM,
    ID3,
    TALB,
    TCOM,
    TCON,
    TDRC,
    TIT2,
    TPE1,
    TPE2,
    TPOS,
    TRCK,
)
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

from .cover_art import extract_embedded_cover

#: Text fields the editor exposes, in the order the UI shows them.
TEXT_FIELDS = (
    'title',
    'artist',
    'album',
    'album_artist',
    'track_number',
    'track_total',
    'disc_number',
    'disc_total',
    'date',
    'genre',
    'comment',
    'composer',
)

#: Fields that carry a number rather than text.
INT_FIELDS = (
    'track_number',
    'track_total',
    'disc_number',
    'disc_total',
)

_PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'


def _to_int(value: Any) -> int:
    try:
        return max(0, int(str(value).strip()))
    except (TypeError, ValueError):
        return 0


def _split_pair(value: str) -> tuple[int, int]:
    """``'3/12'`` → ``(3, 12)``; ``'3'`` → ``(3, 0)``; junk → ``(0, 0)``."""

    head, _, tail = str(value or '').partition('/')
    return _to_int(head), _to_int(tail)


def _first(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        for item in value:
            text = str(item).strip()
            if text:
                return text
        return ''
    return str(value).strip() if value is not None else ''


def _cover_mime(data: bytes) -> str:
    return 'image/png' if data.startswith(_PNG_SIGNATURE) else 'image/jpeg'


# ── Reading ───────────────────────────────────────────────────────────
def _read_id3(path: Path) -> dict[str, Any]:
    try:
        audio = MP3(str(path), ID3=ID3)
    except Exception:
        return {}
    tags = audio.tags
    if tags is None:
        return {}
    text = lambda frame_id: _first(  # noqa: E731
        getattr(tags.get(frame_id), 'text', None) or ''
    )
    track, track_total = _split_pair(text('TRCK'))
    disc, disc_total = _split_pair(text('TPOS'))
    comment = ''
    for frame in tags.getall('COMM'):
        comment = _first(getattr(frame, 'text', None) or '')
        if comment:
            break
    return {
        'title': text('TIT2'),
        'artist': text('TPE1'),
        'album': text('TALB'),
        'album_artist': text('TPE2'),
        'track_number': track,
        'track_total': track_total,
        'disc_number': disc,
        'disc_total': disc_total,
        'date': text('TDRC') or text('TYER'),
        'genre': text('TCON'),
        'comment': comment,
        'composer': text('TCOM'),
    }


def _read_mp4(path: Path) -> dict[str, Any]:
    try:
        audio = MP4(str(path))
    except Exception:
        return {}
    tags = audio.tags
    if tags is None:
        return {}
    text = lambda key: _first(tags.get(key) or '')  # noqa: E731

    def pair(key: str) -> tuple[int, int]:
        value = tags.get(key) or []
        if value and isinstance(value[0], (list, tuple)):
            row = value[0]
            return _to_int(row[0]), _to_int(row[1] if len(row) > 1 else 0)
        return 0, 0

    track, track_total = pair('trkn')
    disc, disc_total = pair('disk')
    return {
        'title': text('\xa9nam'),
        'artist': text('\xa9ART'),
        'album': text('\xa9alb'),
        'album_artist': text('aART'),
        'track_number': track,
        'track_total': track_total,
        'disc_number': disc,
        'disc_total': disc_total,
        'date': text('\xa9day'),
        'genre': text('\xa9gen'),
        'comment': text('\xa9cmt'),
        'composer': text('\xa9wrt'),
    }


def _read_vorbis(audio: Any) -> dict[str, Any]:
    audio = audio or {}
    text = lambda key: _first(audio.get(key) or '')  # noqa: E731
    return {
        'title': text('title'),
        'artist': text('artist'),
        'album': text('album'),
        'album_artist': text('albumartist') or text('album artist'),
        'track_number': _to_int(text('tracknumber')),
        'track_total': _to_int(text('tracktotal')),
        'disc_number': _to_int(text('discnumber')),
        'disc_total': _to_int(text('disctotal') or text('totaldiscs')),
        'date': text('date'),
        'genre': text('genre'),
        'comment': text('comment'),
        'composer': text('composer'),
    }


def read_tags(path: Path) -> dict[str, Any]:
    """Every editable tag of *path*, plus cover/format facts for the UI."""

    suffix = path.suffix.lower()
    fields: dict[str, Any] = {}
    audio: Any = None
    try:
        if suffix == '.mp3':
            fields = _read_id3(path)
            audio = MP3(str(path))
        elif suffix in {'.m4a', '.mp4', '.aac'}:
            fields = _read_mp4(path)
            audio = MP4(str(path))
        elif suffix == '.flac':
            audio = FLAC(str(path))
            fields = _read_vorbis(audio)
        elif suffix in {'.ogg', '.oga'}:
            audio = OggVorbis(str(path))
            fields = _read_vorbis(audio)
        elif suffix == '.opus':
            audio = OggOpus(str(path))
            fields = _read_vorbis(audio)
    except Exception:
        logger.opt(exception=True).debug('Tag editor: could not read {}', path)

    result = {name: '' for name in TEXT_FIELDS}
    for name in INT_FIELDS:
        result[name] = 0
    result.update({key: value for key, value in fields.items()})
    # ``artist`` may hold several names; the UI edits them as one string.
    result['year'] = str(result.get('date') or '')[:4]
    cover, mime = extract_embedded_cover(path)
    result['has_cover'] = bool(cover)
    result['cover_mime'] = mime or ''
    result['duration'] = 0.0
    result['codec'] = ''
    info = getattr(audio, 'info', None)
    try:
        result['duration'] = round(float(info.length), 2)
    except (AttributeError, TypeError, ValueError):
        pass
    result['codec'] = type(audio).__name__.lower() if audio is not None else ''
    return result


# ── Writing ───────────────────────────────────────────────────────────
def _text(fields: dict[str, Any], key: str) -> str:
    value = fields.get(key)
    return str(value).strip() if value is not None else ''


def _artists(fields: dict[str, Any]) -> list[str]:
    raw = _text(fields, 'artist')
    if not raw:
        return []
    parts = [part.strip() for part in raw.replace('/', ';').split(';')]
    return [part for part in parts if part]


def _number(fields: dict[str, Any], key: str) -> str:
    value = _to_int(fields.get(key))
    return str(value) if value > 0 else ''


def _pair(fields: dict[str, Any], number_key: str, total_key: str) -> str:
    number = _number(fields, number_key)
    total = _number(fields, total_key)
    if not number:
        return ''
    return f'{number}/{total}' if total else number


def _has(fields: dict[str, Any], *keys: str) -> bool:
    """True when any of *keys* was sent: absent means "leave unchanged"."""

    return any(key in fields for key in keys)


def _write_mp3(
    path: Path,
    fields: dict[str, Any],
    cover: Optional[bytes],
    remove_cover: bool,
) -> None:
    audio = MP3(str(path), ID3=ID3)
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    assert tags is not None

    def put(frame_id: str, frame_cls: Any, key: str) -> None:
        if key not in fields:
            return
        tags.delall(frame_id)
        text = _text(fields, key)
        if text:
            tags.add(frame_cls(encoding=3, text=text))

    put('TIT2', TIT2, 'title')
    put('TPE1', TPE1, 'artist')
    put('TALB', TALB, 'album')
    put('TPE2', TPE2, 'album_artist')
    put('TDRC', TDRC, 'date')
    put('TCON', TCON, 'genre')
    put('TCOM', TCOM, 'composer')
    if _has(fields, 'track_number', 'track_total'):
        tags.delall('TRCK')
        text = _pair(fields, 'track_number', 'track_total')
        if text:
            tags.add(TRCK(encoding=3, text=text))
    if _has(fields, 'disc_number', 'disc_total'):
        tags.delall('TPOS')
        text = _pair(fields, 'disc_number', 'disc_total')
        if text:
            tags.add(TPOS(encoding=3, text=text))
    if 'comment' in fields:
        tags.delall('COMM')
        comment = _text(fields, 'comment')
        if comment:
            tags.add(COMM(encoding=3, lang='eng', desc='', text=comment))
    if remove_cover:
        tags.delall('APIC')
    if cover:
        tags.delall('APIC')
        tags.add(
            APIC(
                encoding=3,
                mime=_cover_mime(cover),
                type=3,
                desc='Cover',
                data=cover,
            )
        )
    audio.save(v2_version=4)


def _write_mp4(
    path: Path,
    fields: dict[str, Any],
    cover: Optional[bytes],
    remove_cover: bool,
) -> None:
    audio = MP4(str(path))

    def put(key: str, field: str) -> None:
        if field not in fields:
            return
        text = _text(fields, field)
        if text:
            audio[key] = [text]
        elif audio.tags is not None and key in audio:
            del audio[key]

    put('\xa9nam', 'title')
    put('\xa9ART', 'artist')
    put('\xa9alb', 'album')
    put('aART', 'album_artist')
    put('\xa9day', 'date')
    put('\xa9gen', 'genre')
    put('\xa9cmt', 'comment')
    put('\xa9wrt', 'composer')

    if _has(fields, 'track_number', 'track_total'):
        track = _to_int(fields.get('track_number'))
        total = _to_int(fields.get('track_total'))
        if track or total:
            audio['trkn'] = [(track, total)]
        elif audio.tags is not None and 'trkn' in audio:
            del audio['trkn']
    if _has(fields, 'disc_number', 'disc_total'):
        disc = _to_int(fields.get('disc_number'))
        disc_total = _to_int(fields.get('disc_total'))
        if disc or disc_total:
            audio['disk'] = [(disc, disc_total)]
        elif audio.tags is not None and 'disk' in audio:
            del audio['disk']

    if remove_cover and audio.tags is not None and 'covr' in audio:
        del audio['covr']
    if cover:
        fmt = (
            MP4Cover.FORMAT_PNG
            if cover.startswith(_PNG_SIGNATURE)
            else MP4Cover.FORMAT_JPEG
        )
        audio['covr'] = [MP4Cover(cover, imageformat=fmt)]
    audio.save()


def _vorbis_put(audio: Any, key: str, value: str) -> None:
    if value:
        audio[key] = value
    elif key in audio:
        del audio[key]


def _write_vorbis(
    opener: Callable[[str], Any],
    path: Path,
    fields: dict[str, Any],
    cover: Optional[bytes],
    remove_cover: bool,
) -> None:
    audio = opener(str(path))

    def put(key: str, field: str) -> None:
        if field in fields:
            _vorbis_put(audio, key, _text(fields, field))

    put('title', 'title')
    put('artist', 'artist')
    put('album', 'album')
    put('albumartist', 'album_artist')
    put('date', 'date')
    put('genre', 'genre')
    put('comment', 'comment')
    put('composer', 'composer')
    if 'track_number' in fields:
        _vorbis_put(audio, 'tracknumber', _number(fields, 'track_number'))
    if 'track_total' in fields:
        _vorbis_put(audio, 'tracktotal', _number(fields, 'track_total'))
    if 'disc_number' in fields:
        _vorbis_put(audio, 'discnumber', _number(fields, 'disc_number'))
    if 'disc_total' in fields:
        _vorbis_put(audio, 'disctotal', _number(fields, 'disc_total'))

    if remove_cover and 'metadata_block_picture' in audio:
        del audio['metadata_block_picture']
    if cover:
        picture = Picture()
        picture.data = cover
        picture.type = 3
        picture.mime = _cover_mime(cover)
        encoded = base64.b64encode(picture.write()).decode('ascii')
        audio['metadata_block_picture'] = [encoded]
    audio.save()


def write_tags(
    path: Path,
    fields: dict[str, Any],
    *,
    cover: Optional[bytes] = None,
    remove_cover: bool = False,
) -> None:
    """Write *fields* (and optionally the cover) into *path* in place.

    Raises :class:`ValueError` for a format the editor can't write, and
    the caller's ``OSError``/mutagen errors propagate so the API can
    report them.
    """

    suffix = path.suffix.lower()
    if suffix == '.mp3':
        _write_mp3(path, fields, cover, remove_cover)
    elif suffix in {'.m4a', '.mp4', '.aac'}:
        _write_mp4(path, fields, cover, remove_cover)
    elif suffix == '.flac':
        _write_vorbis(FLAC, path, fields, cover, remove_cover)
    elif suffix in {'.ogg', '.oga'}:
        _write_vorbis(OggVorbis, path, fields, cover, remove_cover)
    elif suffix == '.opus':
        _write_vorbis(OggOpus, path, fields, cover, remove_cover)
    else:
        raise ValueError(f'Unsupported audio format: {suffix or "unknown"}')
    logger.info('Tag editor: wrote tags to {}', path.name)
