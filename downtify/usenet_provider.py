"""Usenet provider: Newznab indexer search plus SABnzbd download handoff.

One or more Newznab indexers (NZBGeek, DrunkenSlug, ...) are searched in
parallel for audio releases; the best-scoring release is handed to SABnzbd
as an NZB URL (falling back to uploading the NZB bytes when the indexer
refuses a direct fetch by SABnzbd). The finished file is located under the
mounted ``complete_dir`` and verified against the Spotify metadata before
being returned to the downloader.

Cleanup design: SABnzbd history entries (and their files) are deleted on a
60-second ``threading.Timer`` *after* verification succeeds. The downloader
copies the file synchronously in ``_finalize_local_source`` right after this
module returns, so a delayed delete gives that copy time to finish without
coupling the provider to the downloader's internals. A timer that never
fires (process exit) just leaves the history entry behind — harmless.
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
from loguru import logger

from .download_control import DownloadCancelled, sleep_interruptible
from .library_metadata import read_audio_metadata
from .slskd_provider import (
    _alnum_only,
    _file_basename,
    _int_setting,
    _normalize_search_text,
    _primary_title,
)
from .track_tag_match import (
    duration_tolerances_from_settings,
    media_duration_matches_song,
    snapshot_spotify_metadata,
    spotify_file_tag_mismatch_label,
    verify_downloaded_file_matches_spotify,
)

ProgressCallback = Callable[[float, str, str], None]

_USENET_SEM: Optional[threading.Semaphore] = None
_USENET_SEM_LIMIT = 0

# Albums already fetched via SABnzbd, ``album_key`` → folder inside the
# mounted complete dir. Lets every song of the same album reuse one
# download instead of re-fetching the album per track.
_ALBUM_FOLDERS: dict[str, str] = {}
_ALBUM_CACHE_PATH: Optional[Path] = None
_ALBUM_CACHE_LOCK = threading.Lock()
# In-flight album downloads: waiters reuse the folder instead of queueing
# the same NZB again while the first fetch is still running.
_ALBUM_EVENTS: dict[str, threading.Event] = {}
# Hard guarantee at the NZB level: one URL is handed to SABnzbd once,
# no matter how many songs/album keys selected it.
_NZB_LOCK = threading.Lock()
_NZB_CLAIMS: dict[str, threading.Event] = {}
_NZB_FOLDERS: dict[str, str] = {}


def configure_album_cache(path: Optional[Path]) -> None:
    """Point the album cache at a JSON file (call once at startup)."""
    global _ALBUM_CACHE_PATH
    with _ALBUM_CACHE_LOCK:
        _ALBUM_CACHE_PATH = path
        _ALBUM_FOLDERS.clear()
        if path is not None and path.is_file():
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                logger.info('usenet: album cache unreadable at {}', path)
                return
            if isinstance(data, dict):
                _ALBUM_FOLDERS.update({
                    str(k): str(v) for k, v in data.items()
                })


def _album_cache_save() -> None:
    if _ALBUM_CACHE_PATH is None:
        return
    try:
        _ALBUM_CACHE_PATH.write_text(
            json.dumps(_ALBUM_FOLDERS), encoding='utf-8'
        )
    except OSError as exc:
        logger.info('usenet: album cache save failed err={}', exc)


def _album_cache_key(song: dict[str, Any]) -> str:
    """Stable key for one release, even when a track credits a feature.

    ``album_artist`` is the album's own artist (set by YouTube Music's
    album resolver and most Spotify rows). Falling back to the track's
    first artist would give a featured track a different key and download
    the same album again.
    """

    artist = str(song.get('album_artist') or '').strip()
    if not artist:
        artist = ' '.join(
            str(a).strip()
            for a in (song.get('artists') or [])[:1]
            if str(a).strip()
        ).strip()
    if not artist:
        artist = str(song.get('artist') or '').strip()
    album = str(song.get('album_name') or '').strip()
    if not artist or not album:
        return ''
    return _alnum_only(f'{artist} {album}').casefold()


def album_cache_key(song: dict[str, Any]) -> str:
    """Public form of :func:`_album_cache_key` for batch cleanup."""

    return _album_cache_key(song)


def _check_cancel(cancelled: Optional[Callable[[], bool]]) -> None:
    if cancelled is not None and cancelled():
        raise DownloadCancelled()


def _prefer_albums(song: dict[str, Any], settings: dict[str, Any]) -> bool:
    """Album releases when the user prefers them or the caller needs an album.

    Album and artist-discography downloads set ``_usenet_prefer_album`` on
    the song so one SABnzbd fetch covers the whole release even when the
    global "prefer album releases" switch is off.
    """

    return bool(
        settings.get('prefer_albums') or song.get('_usenet_prefer_album')
    )


def clear_album_cache(
    *,
    complete_dir: Optional[Path] = None,
    delete_folders: bool = False,
) -> int:
    """Forget cached albums and optionally remove their SABnzbd folders.

    The manual cleanup button uses this after deleting SABnzbd history:
    an album folder kept for reuse may no longer have a history entry,
    so history deletion alone would leave it behind. Only folders below
    *complete_dir* are touched; nothing outside SABnzbd's own tree is
    ever removed.
    """

    with _ALBUM_CACHE_LOCK:
        folders = list(_ALBUM_FOLDERS.values())
        _ALBUM_FOLDERS.clear()
    _album_cache_save()
    with _NZB_LOCK:
        _NZB_FOLDERS.clear()
    if not delete_folders or complete_dir is None:
        return 0
    try:
        root = complete_dir.resolve()
    except OSError:
        return 0
    removed = 0
    for raw in folders:
        folder = Path(raw)
        try:
            resolved = folder.resolve()
            if resolved == root or root not in resolved.parents:
                continue
            if folder.is_dir():
                shutil.rmtree(folder, ignore_errors=True)
                removed += 1
        except OSError:
            logger.info('usenet: could not remove cached album {}', folder)
    return removed


def cleanup_albums(
    keys: list[str] | set[str],
    complete_dir: Path,
    *,
    delay: float = 5.0,
) -> int:
    """Drop cached album entries and delete their SABnzbd folders.

    Called once a batch or an album download has copied every track it
    needed. The cache entry is removed immediately so it is not reused;
    the folder itself is removed after *delay* seconds, which lets the
    last copy/tag operation finish.
    """

    if not keys:
        return 0
    folders: list[Path] = []
    with _ALBUM_CACHE_LOCK:
        for key in keys:
            raw = _ALBUM_FOLDERS.pop(key, None)
            if raw:
                folders.append(Path(raw))
    _album_cache_save()
    if not folders:
        return 0
    try:
        root = complete_dir.resolve()
    except OSError:
        return 0
    scheduled = 0
    for folder in folders:
        try:
            resolved = folder.resolve()
            if resolved == root or root not in resolved.parents:
                continue
            _forget_nzb_folders(folder)
            timer = threading.Timer(
                max(0.0, delay), _remove_cached_folder, args=(folder, root)
            )
            timer.daemon = True
            timer.start()
            scheduled += 1
        except OSError:
            logger.info('usenet: could not schedule album cleanup {}', folder)
    return scheduled


def _remove_cached_folder(folder: Path, root: Path) -> None:
    try:
        resolved = folder.resolve()
        if resolved == root or root not in resolved.parents:
            return
        if folder.is_dir():
            shutil.rmtree(folder, ignore_errors=True)
            logger.info('usenet: removed cached album folder {}', folder)
    except OSError:
        logger.info('usenet: could not remove cached album {}', folder)


def _claim_nzb(
    url: str,
) -> tuple[str, Optional[Path], Optional[threading.Event]]:
    """``(state, folder, event)``: cached, wait, or owner for *url*."""

    if not url:
        return 'owner', None, None
    with _NZB_LOCK:
        raw = _NZB_FOLDERS.get(url)
        if raw:
            folder = Path(raw)
            if folder.is_dir():
                return 'cached', folder, None
            _NZB_FOLDERS.pop(url, None)
        event = _NZB_CLAIMS.get(url)
        if event is not None:
            return 'wait', None, event
        event = threading.Event()
        _NZB_CLAIMS[url] = event
        return 'owner', None, event


def _finish_nzb_claim(
    url: str,
    event: Optional[threading.Event],
    folder: Optional[Path] = None,
) -> None:
    if not url:
        if event is not None:
            event.set()
        return
    with _NZB_LOCK:
        if folder is not None:
            _NZB_FOLDERS[url] = str(folder)
        if event is not None and _NZB_CLAIMS.get(url) is event:
            _NZB_CLAIMS.pop(url, None)
    if event is not None:
        event.set()


def _nzb_folder(url: str) -> Optional[Path]:
    with _NZB_LOCK:
        raw = _NZB_FOLDERS.get(url)
    return Path(raw) if raw else None


def _forget_nzb_folders(folder: Path) -> None:
    raw = str(folder)
    with _NZB_LOCK:
        for url, stored in list(_NZB_FOLDERS.items()):
            if stored == raw:
                _NZB_FOLDERS.pop(url, None)


def _folder_has_audio(folder: Path) -> bool:
    try:
        entries = folder.rglob('*')
    except OSError:
        return False
    for entry in entries:
        try:
            if (
                entry.is_file()
                and entry.suffix.lower().lstrip('.') in _AUDIO_EXTENSIONS
            ):
                return True
        except OSError:
            continue
    return False


def _album_cache_get(key: str) -> Optional[Path]:
    """The cached album folder, or None (stale entries are dropped)."""
    if not key:
        return None
    with _ALBUM_CACHE_LOCK:
        folder = _ALBUM_FOLDERS.get(key)
    if not folder:
        return None
    path = Path(folder)
    if not path.is_dir():
        with _ALBUM_CACHE_LOCK:
            _ALBUM_FOLDERS.pop(key, None)
        _album_cache_save()
        return None
    return path


def _album_cache_put(key: str, folder: Path) -> None:
    if not key:
        return
    with _ALBUM_CACHE_LOCK:
        _ALBUM_FOLDERS[key] = str(folder)
    _album_cache_save()


def _usenet_parallel_limit(settings: dict[str, Any]) -> int:
    return _int_setting(
        settings,
        'max_parallel_downloads',
        3,
        minimum=1,
        maximum=8,
    )


def reset_usenet_parallelism(settings: dict[str, Any]) -> None:
    """Apply a new parallel limit after settings change (call on save)."""
    global _USENET_SEM, _USENET_SEM_LIMIT
    limit = _usenet_parallel_limit(settings)
    _USENET_SEM = threading.Semaphore(limit)
    _USENET_SEM_LIMIT = limit


def _usenet_semaphore(settings: dict[str, Any]) -> threading.Semaphore:
    """Match Downtify parallel download setting; do not serialize all usenet work."""
    limit = _usenet_parallel_limit(settings)
    if _USENET_SEM is None or _USENET_SEM_LIMIT != limit:
        reset_usenet_parallelism(settings)
    return _USENET_SEM


_NEWZNAB_ATTR_NS = '{http://www.newznab.com/DTD/2010/feeds/attributes/}attr'
_AUDIO_EXTENSIONS = frozenset({'mp3', 'flac', 'm4a', 'ogg', 'opus'})
_DEFAULT_MATCH_MIN_SCORE = 5
# Passworded/junk releases, matched against the casefolded release title.
_EXCLUDE_KEYWORD_RE = re.compile(
    r'\b(?:password|passworded|pw)\b|sample\s*only',
    re.IGNORECASE,
)
# Implied-bitrate sanity bounds (kbps) for size vs. duration plausibility.
_MIN_IMPLIED_KBPS = 96
_MAX_IMPLIED_KBPS = 2500
_STRICT_DURATION_SECONDS = 3
_CLEANUP_DELAY_SECONDS = 60.0
_DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 600


def _usenet_deadline(settings: dict[str, Any]) -> float:
    """Wall-clock budget for one track (search + queue + fetch), as slskd."""
    seconds = _int_setting(
        settings,
        'download_timeout_seconds',
        _DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
        minimum=30,
        maximum=3600,
    )
    return time.monotonic() + seconds


def _past_deadline(deadline: float) -> bool:
    return time.monotonic() >= deadline


# ── Newznab / Prowlarr ───────────────────────────────────────────────
class NewznabClient:
    """Minimal Newznab API client for one indexer.

    ``type`` selects the dialect: ``'newznab'`` (classic indexers, XML
    responses at ``<base>/api``) or ``'prowlarr'`` (Prowlarr's native REST
    API at ``<base>/api/v1/search``, JSON responses, ``X-Api-Key`` header).
    """

    def __init__(self, indexer: dict[str, Any], timeout: int = 20):
        self.name = str(indexer.get('name') or '').strip() or 'indexer'
        self.base_url = str(indexer.get('base_url') or '').strip().rstrip('/')
        self.api_key = str(indexer.get('api_key') or '').strip()
        self.type = (
            'prowlarr'
            if str(indexer.get('type') or '').strip().lower() == 'prowlarr'
            else 'newznab'
        )
        self.timeout = timeout

    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    def _headers(self) -> dict[str, str]:
        return {'X-Api-Key': self.api_key} if self.type == 'prowlarr' else {}

    def endpoints(self) -> list[str]:
        """Candidate search endpoints, first match wins.

        Classic indexers serve the spec at ``<base>/api``; Prowlarr (and
        some Hydra setups) expose it at ``<base>/api/v1/search`` instead.
        A base that already ends in ``/api`` or ``/search`` is used
        verbatim. Prowlarr-type clients always use their native endpoint.
        """
        if self.type == 'prowlarr':
            return [f'{self.base_url}/api/v1/search']
        if self.base_url.endswith(('/api', '/search')):
            return [self.base_url]
        return [f'{self.base_url}/api', f'{self.base_url}/api/v1/search']

    def _get(self, params: dict[str, Any], **kwargs: Any) -> httpx.Response:
        endpoints = self.endpoints()
        resp = None
        for i, url in enumerate(endpoints):
            resp = httpx.get(
                url,
                params=params,
                headers=self._headers(),
                timeout=self.timeout,
                follow_redirects=True,
                **kwargs,
            )
            if resp.status_code == 404 and i < len(endpoints) - 1:
                # Prowlarr-style base without the classic /api path; speak
                # its REST dialect on the retry.
                params = _newznab_to_v1_params(params)
                continue
            break
        resp.raise_for_status()
        return resp

    def search(self, query: str) -> list[dict[str, Any]]:
        """Run an audio-category search; returns parsed release rows."""
        resp = self._get(
            {
                't': 'search',
                'q': query,
                'cat': '3000,3010',
                'apikey': self.api_key,
                'extended': '1',
                'limit': '100',
            }
            if self.type == 'newznab'
            else [
                ('query', query),
                # Prowlarr rejects a comma-joined categories value with 400;
                # the API takes repeated parameters.
                ('categories', '3000'),
                ('categories', '3010'),
                ('apikey', self.api_key),
                ('limit', '100'),
            ]
        )
        if self.type == 'prowlarr':
            try:
                rows = resp.json()
            except ValueError:
                logger.info(
                    'usenet: non-JSON from prowlarr indexer={!r}',
                    self.name,
                )
                return []
            return parse_prowlarr_results(rows, self.name)
        return parse_newznab_xml(resp.content, self.name)

    def fetch_nzb(self, url: str) -> bytes:
        """Download the raw NZB bytes (used when SABnzbd can't fetch them)."""
        resp = httpx.get(
            self._absolute_url(url),
            params=(
                {'apikey': self.api_key}
                if self.type == 'prowlarr' and 'apikey=' not in url
                else None
            ),
            headers=self._headers(),
            timeout=self.timeout,
            follow_redirects=True,
        )
        resp.raise_for_status()
        return resp.content

    def _absolute_url(self, url: str) -> str:
        if url.startswith('http'):
            return url
        return f'{self.base_url}/{url.lstrip("/")}'


def _newznab_to_v1_params(params: Any) -> Any:
    """Translate Newznab search params to Prowlarr's REST /api/v1 dialect.

    Used when a Newznab-typed client falls back from ``/api`` (404) to a
    Prowlarr base: ``t=search``/``q``/``cat`` become ``query``/repeated
    ``categories``, the Newznab-only fields are dropped.
    """
    if not isinstance(params, dict) or params.get('t') != 'search':
        return params
    out: list[tuple[str, str]] = [('query', str(params.get('q') or ''))]
    for cat in str(params.get('cat') or '').split(','):
        if cat.strip():
            out.append(('categories', cat.strip()))
    if str(params.get('apikey') or ''):
        out.append(('apikey', str(params['apikey'])))
    if str(params.get('limit') or ''):
        out.append(('limit', str(params['limit'])))
    return out


def parse_newznab_xml(
    content: bytes, indexer_name: str = ''
) -> list[dict[str, Any]]:
    """Parse a Newznab RSS response into release dicts.

    Each row carries ``title``, ``nzb_url`` (link or guid), ``size`` (item
    element or newznab attr), ``grabs``/``passworded``/``duration`` from the
    ``newznab:attr`` namespace, and the originating ``indexer`` name.
    Unparseable payloads return an empty list.
    """
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        logger.info(
            'usenet: bad XML from indexer={!r} err={}', indexer_name, exc
        )
        return []
    rows: list[dict[str, Any]] = []
    for item in root.iter('item'):
        title = str(item.findtext('title') or '').strip()
        if not title:
            continue
        link = str(item.findtext('link') or '').strip()
        guid = str(item.findtext('guid') or '').strip()
        nzb_url = link if link.startswith('http') else guid
        if not nzb_url.startswith('http'):
            continue
        size = _to_int(item.findtext('size'))
        attrs: dict[str, str] = {}
        for attr in item.iter(_NEWZNAB_ATTR_NS):
            name = str(attr.get('name') or '').strip().lower()
            value = str(attr.get('value') or '').strip()
            if name:
                attrs[name] = value
        if not size:
            size = _to_int(attrs.get('size'))
        rows.append({
            'indexer': indexer_name,
            'title': title,
            'nzb_url': nzb_url,
            'size': size,
            'grabs': _to_int(attrs.get('grabs')),
            'passworded': _to_int(attrs.get('password')),
            'duration': _to_int(attrs.get('duration')),
        })
    return rows


def parse_prowlarr_results(
    rows: Any, indexer_name: str = ''
) -> list[dict[str, Any]]:
    """Parse a Prowlarr native-API search response into release dicts.

    Only usenet-protocol results are kept (SABnzbd cannot fetch torrents).
    Prowlarr reports no duration and no password flag, so those checks are
    left to the size/duration plausibility gate and the post-download tag
    verification. Unparseable payloads return an empty list.
    """
    if not isinstance(rows, list):
        return []
    parsed: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        protocol = str(row.get('protocol') or '').strip().lower()
        if protocol and protocol != 'usenet':
            continue
        title = str(row.get('title') or '').strip()
        download_url = str(row.get('downloadUrl') or '').strip()
        if not title or not download_url:
            continue
        parsed.append({
            'indexer': str(row.get('indexer') or '') or indexer_name,
            'title': title,
            'nzb_url': download_url,
            'size': _to_int(row.get('size')),
            'grabs': _to_int(row.get('grabs') or row.get('seeders')),
            'passworded': 0,
            'duration': 0,
        })
    return parsed


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _usenet_search_queries(song: dict[str, Any]) -> list[str]:
    """Query variants for indexers, mirroring the slskd query set."""
    artists = [
        str(a).strip()
        for a in (song.get('artists') or [])[:2]
        if str(a).strip()
    ]
    artist = artists[0] if artists else ''
    title = str(song.get('name') or '').strip()
    short_title = _primary_title(title)
    album = str(song.get('album_name') or '').strip()
    queries: list[str] = []
    seen: set[str] = set()

    def add(q: str) -> None:
        normalized = _normalize_search_text(q)
        if normalized and normalized not in seen:
            seen.add(normalized)
            queries.append(normalized)

    if artist and short_title:
        add(f'{artist} {short_title}')
        add(f'{short_title} {artist}')
    if album and artist and album.casefold() != short_title.casefold():
        add(f'{artist} {album}')
    if title and artist and title != short_title:
        add(f'{artist} {title}')
    if short_title:
        add(short_title)
    return queries


def _release_extension_bonus(
    title: str, extensions: list[str]
) -> tuple[int, str]:
    """Score bonus when the release title advertises a preferred format.

    The ``extensions`` list is the user's preference order: the first
    format scores highest, later ones less, formats not listed none.
    """
    folded = title.casefold()
    for idx, ext in enumerate(extensions):
        bonus = (3, 2, 1)[min(idx, 2)]
        if ext == 'flac' and re.search(
            r'(?:^|[^a-z])flac(?:[^a-z]|$)', folded
        ):
            return bonus, 'ext_flac'
        if ext == 'mp3' and (
            re.search(r'(?:^|[^a-z0-9])(?:320|mp3)(?:[^a-z0-9]|$)', folded)
        ):
            return bonus, 'ext_mp3'
        if ext not in {'flac', 'mp3'} and ext in folded:
            return bonus, f'ext_{ext}'
    return 0, ''


def _implied_bitrate_ok(
    size: int,
    duration: int,
    min_kbps: int = 0,
    max_kbps: int = 0,
) -> bool:
    """Plausibility of the NZB size against the track's runtime.

    ``min_kbps``/``max_kbps`` come from the settings (``0`` = use the
    defaults), letting the user demand e.g. at least 256 kbps.
    """
    if size <= 0 or duration <= 0:
        return True
    kbps = (size * 8) / (duration * 1000)
    low = min_kbps or _MIN_IMPLIED_KBPS
    high = max_kbps or _MAX_IMPLIED_KBPS
    return low <= kbps <= high


def _score_usenet_candidate(  # noqa: PLR0914  # mirrors slskd scorer
    song: dict[str, Any],
    release: dict[str, Any],
    *,
    target_duration: int,
    settings: Optional[dict[str, Any]] = None,
) -> Optional[tuple[int, list[str], str]]:
    """Return (score, reasons, kind) or None when hard requirements fail.

    ``kind`` is ``'track'`` for a release named after the track itself and
    ``'album'`` for an album release matched via artist + album — the
    latter only exists when ``prefer_albums`` is on.
    """
    settings = settings or {}
    tol = duration_tolerances_from_settings(settings)
    prefer_albums = _prefer_albums(song, settings)
    artist = _alnum_only(
        ' '.join(str(a) for a in (song.get('artists') or [])[:1])
    )
    album = _alnum_only(str(song.get('album_name') or ''))
    raw_name = str(song.get('name') or '')
    title = _alnum_only(_primary_title(raw_name))
    full_title = _alnum_only(raw_name)
    release_title = str(release.get('title') or '')
    release_alnum = _alnum_only(release_title)

    if not release_alnum or (not artist and not title and not full_title):
        return None

    if _to_int(release.get('passworded')) != 0:
        return None
    if _EXCLUDE_KEYWORD_RE.search(release_title):
        return None

    title_ok = bool(
        (title and title in release_alnum)
        or (full_title and full_title != title and full_title in release_alnum)
    )
    kind = 'track'
    album_match = False
    if not title_ok and prefer_albums and album and artist:
        # Album releases are named after the album, not the track; with
        # prefer_albums they are viable when artist and album both match.
        album_match = album in release_alnum and artist in release_alnum
        kind = 'album'
    if (title or full_title) and not title_ok and not album_match:
        return None
    if artist and artist not in release_alnum and not album_match:
        return None

    duration = _to_int(release.get('duration'))
    if not album_match and duration and target_duration:
        # An album release's duration attribute is the whole record, so
        # the per-track tolerance check only applies to track releases.
        if not media_duration_matches_song(
            song,
            duration,
            tolerance_seconds=tol['seconds'],
            tolerance_percent=tol['percent'],
        ):
            return None

    size = _to_int(release.get('size'))
    if not album_match and not _implied_bitrate_ok(
        size,
        duration or target_duration,
        _int_setting(settings, 'min_bitrate', 0, minimum=0, maximum=2000),
        _int_setting(settings, 'max_bitrate', 0, minimum=0, maximum=4000),
    ):
        return None

    score = 0
    reasons: list[str] = []

    if artist and release_alnum.startswith(artist):
        score += 3
        reasons.append('artist_prefix')
    elif artist:
        score += 1
        reasons.append('artist')

    if not album_match:
        if title and title in release_alnum:
            score += 3
            reasons.append('title')
        elif full_title and full_title in release_alnum:
            score += 2
            reasons.append('title_full')

    if album and album in release_alnum:
        score += 2
        reasons.append('album')

    if not album_match and duration and target_duration:
        if abs(target_duration - duration) <= _STRICT_DURATION_SECONDS:
            score += 3
            reasons.append('duration_3s')

    raw_ext = settings.get('extensions') or ['flac', 'mp3']
    extensions = [
        str(e).strip().lower().lstrip('.') for e in raw_ext if str(e).strip()
    ] or ['flac', 'mp3']
    bonus, reason = _release_extension_bonus(release_title, extensions)
    if bonus:
        score += bonus
        reasons.append(reason)

    if _to_int(release.get('grabs')) >= 5:
        score += 1
        reasons.append('grabs')

    return score, reasons, kind


def _rank_usenet_candidates(
    song: dict[str, Any],
    releases: list[dict[str, Any]],
    settings: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """Score and sort all viable releases (highest confidence first)."""
    target_duration = _song_duration_seconds(song)
    prefer_albums = _prefer_albums(song, settings or {})
    ranked: list[dict[str, Any]] = []
    for release in releases:
        scored = _score_usenet_candidate(
            song,
            release,
            target_duration=target_duration,
            settings=settings,
        )
        if scored is None:
            continue
        score, reasons, kind = scored
        ranked.append({
            **release,
            'match_score': score,
            'match_reasons': reasons,
            'match_kind': kind,
        })
    _sort_ranked(ranked, prefer_albums)
    return ranked


def _sort_ranked(ranked: list[dict[str, Any]], prefer_albums: bool) -> None:
    """Sort in place: album releases first when preferred, then score."""
    ranked.sort(
        key=lambda row: (
            (0 if prefer_albums and row.get('match_kind') == 'album' else 1),
            -_to_int(row.get('match_score')),
            -_to_int(row.get('grabs')),
        )
    )


def _song_duration_seconds(song: dict[str, Any]) -> int:
    target_duration = int(song.get('duration') or 0)
    if target_duration > 1000:
        return target_duration // 1000
    return target_duration


def _search_indexers(
    indexers: list[NewznabClient], query: str
) -> list[dict[str, Any]]:
    """Query all enabled indexers in parallel; a failed one is skipped."""
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(
        max_workers=max(1, len(indexers)),
        thread_name_prefix='downtify-newznab',
    ) as pool:
        futures = {
            pool.submit(client.search, query): client for client in indexers
        }
        for future, client in futures.items():
            try:
                results.extend(future.result())
            except Exception as exc:
                logger.info(
                    'usenet: indexer search failed indexer={!r} q={!r} err={}',
                    client.name,
                    query[:120],
                    exc,
                )
    return results


# ── SABnzbd ──────────────────────────────────────────────────────────
class SabnzbdClient:
    """Minimal SABnzbd API client (addurl/addfile, queue/history polling)."""

    def __init__(self, settings: dict[str, Any], timeout: int = 20):
        sab = settings.get('sabnzbd')
        sab = sab if isinstance(sab, dict) else {}
        self.base_url = str(sab.get('base_url') or '').strip().rstrip('/')
        self.api_key = str(sab.get('api_key') or '').strip()
        self.category = str(sab.get('category') or 'music').strip() or 'music'
        self.timeout = timeout

    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    def _api(self, params: dict[str, Any]) -> dict[str, Any]:
        query = {
            'output': 'json',
            'apikey': self.api_key,
            **params,
        }
        resp = httpx.get(
            f'{self.base_url}/sabnzbd/api',
            params=query,
            timeout=self.timeout,
            follow_redirects=True,
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {}

    def add_url(self, nzb_url: str, release_name: str) -> Optional[str]:
        """Queue an NZB by URL; returns the nzo_id on success."""
        data = self._api({
            'mode': 'addurl',
            'name': nzb_url,
            'cat': self.category,
            'nzbname': release_name,
        })
        return self._nzo_id(data)

    def add_file(self, nzb_bytes: bytes, release_name: str) -> Optional[str]:
        """Queue an NZB by uploading its bytes (multipart addfile)."""
        resp = httpx.post(
            f'{self.base_url}/sabnzbd/api',
            params={
                'output': 'json',
                'apikey': self.api_key,
                'mode': 'addfile',
                'cat': self.category,
                'nzbname': release_name,
            },
            files={
                'nzbfile': (
                    f'{release_name}.nzb',
                    nzb_bytes,
                    'application/x-nzb',
                ),
            },
            timeout=self.timeout,
            follow_redirects=True,
        )
        resp.raise_for_status()
        data = resp.json()
        return self._nzo_id(data if isinstance(data, dict) else {})

    @staticmethod
    def _nzo_id(data: dict[str, Any]) -> Optional[str]:
        if data.get('status') is False:
            return None
        ids = data.get('nzo_ids')
        if isinstance(ids, list) and ids:
            return str(ids[0])
        raw = data.get('nzo_id')
        return str(raw) if raw else None

    def queue_slot(self, nzo_id: str) -> Optional[dict[str, Any]]:
        data = self._api({'mode': 'queue'})
        queue = data.get('queue')
        slots = queue.get('slots') if isinstance(queue, dict) else None
        return _find_slot(slots, nzo_id)

    def history_slot(self, nzo_id: str) -> Optional[dict[str, Any]]:
        data = self._api({'mode': 'history'})
        history = data.get('history')
        slots = history.get('slots') if isinstance(history, dict) else None
        return _find_slot(slots, nzo_id)

    def delete_history(self, nzo_id: str) -> None:
        """Remove the history entry and its files (post-copy cleanup)."""
        try:
            self._api({
                'mode': 'history',
                'name': 'delete',
                'value': nzo_id,
                'del_files': '1',
            })
        except Exception as exc:
            # The audio file was already copied; leftover history is cosmetic.
            logger.info(
                'usenet: history cleanup failed nzo={} err={}', nzo_id, exc
            )

    def cancel_job(self, nzo_id: str) -> None:
        """Remove a job we queued from SABnzbd, queue or history.

        Used by Stop all downloads. ``abort`` stops the transfer itself;
        if it already finished, the history entry and its files go.
        Only the job IDs we submitted are ever touched.
        """

        if not nzo_id:
            return
        try:
            self._api({
                'mode': 'queue',
                'name': 'delete',
                'value': nzo_id,
                'del_files': '1',
            })
        except Exception as exc:
            logger.info(
                'usenet: queue cancel failed nzo={} err={}', nzo_id, exc
            )
        try:
            self._api({
                'mode': 'history',
                'name': 'delete',
                'value': nzo_id,
                'del_files': '1',
            })
        except Exception as exc:
            logger.info(
                'usenet: history cancel failed nzo={} err={}', nzo_id, exc
            )

    def delete_all_history(self) -> int:
        """Delete every history entry and its files (manual cleanup)."""
        data = self._api({'mode': 'history'})
        history = data.get('history')
        slots = history.get('slots') if isinstance(history, dict) else None
        deleted = 0
        for slot in slots if isinstance(slots, list) else []:
            nzo_id = str(slot.get('nzo_id') or '')
            if not nzo_id:
                continue
            try:
                self._api({
                    'mode': 'history',
                    'name': 'delete',
                    'value': nzo_id,
                    'del_files': '1',
                })
                deleted += 1
            except Exception as exc:
                logger.info(
                    'usenet: history delete failed nzo={} err={}',
                    nzo_id,
                    exc,
                )
        return deleted


def _find_slot(slots: Any, nzo_id: str) -> Optional[dict[str, Any]]:
    if not isinstance(slots, list):
        return None
    for slot in slots:
        if isinstance(slot, dict) and str(slot.get('nzo_id') or '') == nzo_id:
            return slot
    return None


def _usenet_progress_pct(
    queue_slot: Optional[dict[str, Any]], poll_attempt: int
) -> tuple[float, str]:
    """Map an active SABnzbd queue slot to overall job progress (40–92%)."""
    lo, hi = 40.0, 92.0
    span = hi - lo
    if queue_slot:
        try:
            pct = float(queue_slot.get('percentage') or 0)
        except (TypeError, ValueError):
            pct = 0.0
        if 0 < pct <= 100:
            return lo + span * (pct / 100.0), 'Downloading'
    fallback = lo + min(span * 0.12, max(0, poll_attempt) * 0.4)
    return min(hi - 4.0, fallback), 'Waiting for SABnzbd'


def _find_completed_file(
    complete_dir: Path,
    storage: str,
    release_name: str,
    song: dict[str, Any],
) -> Optional[Path]:
    """Locate SABnzbd's output under our mount of its complete folder.

    The path SABnzbd reports (``storage``) is inside its own container; the
    mount here may differ, so match on the lowest path components and fall
    back to a recursive audio-file search by release/track name.
    """
    candidates: list[Path] = []
    if storage:
        normalized = str(storage).replace('\\', '/').rstrip('/')
        base = Path(normalized).name
        if base:
            candidates.append(complete_dir / base)
            direct = Path(normalized)
            if direct.is_file():
                candidates.append(direct)

    for candidate in candidates:
        if candidate.is_file():
            return candidate
        if candidate.is_dir():
            audio = _audio_files_in(candidate, release_name, song)
            if audio is not None:
                return audio

    if not complete_dir.is_dir():
        return None
    tokens = [
        token
        for token in (
            _alnum_only(release_name),
            _alnum_only(str(song.get('name') or '')),
        )
        if len(token) >= 4
    ]
    if not tokens:
        return None
    best: Optional[Path] = None
    best_mtime = 0.0
    try:
        entries = list(complete_dir.rglob('*'))
    except OSError:
        return None
    for entry in entries:
        if not entry.is_file():
            continue
        if entry.suffix.lower().lstrip('.') not in _AUDIO_EXTENSIONS:
            continue
        name_alnum = _alnum_only(entry.name)
        if not any(token in name_alnum for token in tokens):
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if mtime >= best_mtime:
            best = entry
            best_mtime = mtime
    return best


def _album_storage_folder(complete_dir: Path, storage: str) -> Optional[Path]:
    """SABnzbd's finished folder for a release, under our mount."""

    if storage:
        normalized = str(storage).replace('\\', '/').rstrip('/')
        direct = Path(normalized)
        if direct.is_dir():
            return direct
        base = Path(normalized).name
        if base:
            candidate = complete_dir / base
            if candidate.is_dir():
                return candidate
    return None


def _find_album_track(
    complete_dir: Path,
    storage: str,
    song: dict[str, Any],
    settings: dict[str, Any],
) -> Optional[Path]:
    """Pick the wanted track out of a downloaded album folder.

    The album NZB contains every track; the right one is chosen by tag
    title and duration (falling back to the file name), so a wrong pick
    never reaches the library. A single-file storage path is returned
    as-is.
    """
    direct = Path(str(storage).replace('\\', '/')) if storage else None
    if direct is not None and direct.is_file():
        return direct

    folder = _album_storage_folder(complete_dir, storage)
    if folder is None and complete_dir.is_dir():
        folder = complete_dir
    if folder is None:
        return None
    return _pick_track_in_album(folder, song, settings)


def _pick_track_in_album(
    folder: Path,
    song: dict[str, Any],
    settings: dict[str, Any],
) -> Optional[Path]:
    """Score every audio file in ``folder`` against the wanted track."""
    title_key = _alnum_only(_primary_title(str(song.get('name') or '')))
    full_key = _alnum_only(str(song.get('name') or ''))
    target = _song_duration_seconds(song)
    tol = duration_tolerances_from_settings(settings)
    best: Optional[tuple[int, Path]] = None
    try:
        entries = sorted(folder.rglob('*'))
    except OSError:
        return None
    for entry in entries:
        if not entry.is_file():
            continue
        if entry.suffix.lower().lstrip('.') not in _AUDIO_EXTENSIONS:
            continue
        meta = read_audio_metadata(entry)
        name_key = _alnum_only(entry.name)
        meta_title = _alnum_only(str(meta.get('title') or ''))
        score = 0
        if title_key and title_key in meta_title:
            score += 4
        if title_key and title_key in name_key:
            score += 3
        if full_key and full_key != title_key and full_key in name_key:
            score += 1
        duration = int(float(meta.get('duration') or 0))
        if (
            target
            and duration
            and media_duration_matches_song(
                song,
                duration,
                tolerance_seconds=tol['seconds'],
                tolerance_percent=tol['percent'],
            )
        ):
            score += 3
        if score > 0 and (best is None or score > best[0]):
            best = (score, entry)
    return best[1] if best else None


def _audio_files_in(
    folder: Path, release_name: str, song: dict[str, Any]
) -> Optional[Path]:
    tokens = [
        token
        for token in (
            _alnum_only(str(song.get('name') or '')),
            _alnum_only(release_name),
        )
        if len(token) >= 4
    ]
    try:
        entries = sorted(folder.rglob('*'))
    except OSError:
        return None
    for entry in entries:
        if not entry.is_file():
            continue
        if entry.suffix.lower().lstrip('.') not in _AUDIO_EXTENSIONS:
            continue
        if tokens and not any(
            token in _alnum_only(entry.name) for token in tokens
        ):
            continue
        return entry
    return None


def _wait_for_sabnzbd(
    client: SabnzbdClient,
    nzo_id: str,
    settings: dict[str, Any],
    *,
    progress_cb: Optional[ProgressCallback] = None,
    deadline: float = 0.0,
    cancelled: Optional[Callable[[], bool]] = None,
) -> Optional[dict[str, Any]]:
    """Poll queue/history until the job completes; returns the history slot."""
    interval = _int_setting(
        settings, 'poll_interval_seconds', 5, minimum=1, maximum=30
    )
    attempts = _int_setting(
        settings, 'poll_max_attempts', 180, minimum=1, maximum=1000
    )
    for attempt in range(max(1, attempts)):
        _check_cancel(cancelled)
        if _past_deadline(deadline):
            logger.info('usenet: download timed out nzo={}', nzo_id)
            return None
        try:
            slot = client.history_slot(nzo_id)
        except Exception as exc:
            logger.info(
                'usenet: history poll failed nzo={} err={}', nzo_id, exc
            )
            slot = None
        if slot is not None:
            status = str(slot.get('status') or '')
            if status == 'Completed':
                return slot
            if status == 'Failed':
                logger.info(
                    'usenet: SABnzbd job failed nzo={} name={!r}',
                    nzo_id,
                    str(slot.get('name') or '')[:120],
                )
                return None
        try:
            queue_slot = client.queue_slot(nzo_id)
        except Exception:
            queue_slot = None
        if progress_cb is not None:
            pct, message = _usenet_progress_pct(queue_slot, attempt)
            try:
                progress_cb(pct, f'usenet · {message}', 'usenet')
            except Exception:
                logger.opt(exception=True).debug(
                    'usenet progress callback error'
                )
        if sleep_interruptible(max(1, interval), cancelled):
            raise DownloadCancelled()
    logger.info('usenet: download timed out nzo={}', nzo_id)
    return None


def _discard_mismatched_download(path: Path) -> None:
    try:
        if path.is_file():
            path.unlink()
            logger.info('usenet: removed mismatched download file={!r}', path)
    except OSError as exc:
        logger.warning(
            'usenet: could not remove mismatched file={!r} err={}',
            path,
            exc,
        )


def _schedule_cleanup(client: SabnzbdClient, nzo_id: str) -> None:
    """Delete the SABnzbd history entry (and files) after a short delay.

    The downloader copies the returned file synchronously right after this
    module returns; the 60-second timer keeps the SABnzbd-side files alive
    until that copy is done (see the module docstring).
    """
    timer = threading.Timer(
        _CLEANUP_DELAY_SECONDS, client.delete_history, args=(nzo_id,)
    )
    timer.daemon = True
    timer.start()


def download_from_usenet(  # noqa: PLR0914, PLR1702  # search, rank, queue, poll, verify
    song: dict[str, Any],
    settings: dict[str, Any],
    progress_cb: Optional[ProgressCallback] = None,
    *,
    album_only: bool = False,
    album_keys: Optional[list[str]] = None,
    cancelled: Optional[Callable[[], bool]] = None,
) -> Optional[Path]:
    """Search Newznab indexers and download the best release via SABnzbd.

    Mirrors :func:`downtify.slskd_provider.download_from_slskd`: returns the
    path of the finished audio file, or ``None`` when nothing usable was
    found (the downloader then tries the next audio provider).

    ``album_only`` is the batch prefetch: only album releases are
    considered, the per-track parallel semaphore is bypassed, and the
    finished release folder is cached under every supplied ``album_keys``
    entry. It returns the folder (or the first track path) for the caller.
    """
    if not bool(settings.get('enabled')):
        logger.info(
            'usenet: disabled in settings title={!r}', song.get('name')
        )
        return None
    if album_only:
        song = {**song, '_usenet_prefer_album': True}
    _check_cancel(cancelled)

    timeout = _int_setting(
        settings, 'timeout_seconds', 20, minimum=5, maximum=120
    )
    raw_indexers = settings.get('indexers')
    indexers = [
        NewznabClient(row, timeout=timeout)
        for row in (raw_indexers if isinstance(raw_indexers, list) else [])
        if isinstance(row, dict) and row.get('enabled')
    ]
    indexers = [client for client in indexers if client.configured()]
    client = SabnzbdClient(settings, timeout=timeout)
    if not client.configured() or not indexers:
        logger.warning(
            'usenet: missing configuration title={!r} sab_configured={} '
            'enabled_indexers={}',
            song.get('name'),
            client.configured(),
            len(indexers),
        )
        return None

    spotify_row = snapshot_spotify_metadata(song)
    prefer_albums = album_only or _prefer_albums(song, settings)
    if prefer_albums:
        # snapshot_spotify_metadata keeps only the public metadata; carry
        # the album-download intent through to ranking.
        spotify_row['_usenet_prefer_album'] = True
    queries = _usenet_search_queries(spotify_row)
    if not queries:
        logger.warning(
            'usenet: no search queries for title={!r} artists={}',
            song.get('name'),
            song.get('artists'),
        )
        return None

    def report(pct: float, message: str) -> None:
        if progress_cb is not None:
            try:
                text = (
                    message
                    if str(message).lower().startswith('usenet')
                    else f'usenet · {message}'
                )
                progress_cb(pct, text, 'usenet')
            except Exception:
                logger.opt(exception=True).debug(
                    'usenet progress callback error'
                )

    min_score = _int_setting(
        settings,
        'min_score',
        _DEFAULT_MATCH_MIN_SCORE,
        minimum=0,
        maximum=100,
    )
    sab = settings.get('sabnzbd')
    sab = sab if isinstance(sab, dict) else {}
    complete_dir = Path(str(sab.get('complete_dir') or '/usenet'))
    album_key = _album_cache_key(song)
    cache_keys = {key for key in [album_key, *(album_keys or [])] if key}

    # A previously downloaded album with this artist+album is still on
    # disk: extract the track from it instead of re-downloading anything.
    cached_album = _album_cache_get(album_key)
    if cached_album is not None:
        if album_only:
            return cached_album
        report(30.0, 'using downloaded album')
        found = _pick_track_in_album(cached_album, spotify_row, settings)
        if found is not None and verify_downloaded_file_matches_spotify(
            found, spotify_row
        ):
            logger.info(
                'usenet: reused cached album title={!r} album={!r}',
                song.get('name'),
                cached_album.name,
            )
            return found
        if found is not None:
            _discard_mismatched_download(found)

    semaphore = (
        contextlib.nullcontext() if album_only else _usenet_semaphore(settings)
    )
    with semaphore:  # noqa: PLR1702
        deadline = _usenet_deadline(settings)
        report(5.0, 'searching')
        ranked: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for query in queries:
            _check_cancel(cancelled)
            if _past_deadline(deadline):
                logger.info(
                    'usenet: search timed out title={!r}', song.get('name')
                )
                break
            releases = _search_indexers(indexers, query)
            if not releases:
                continue
            fresh = [
                row
                for row in _rank_usenet_candidates(song, releases, settings)
                if _to_int(row.get('match_score')) >= min_score
                and str(row.get('nzb_url') or '') not in seen_urls
            ]
            for row in fresh:
                seen_urls.add(str(row.get('nzb_url') or ''))
            ranked.extend(fresh)
            if ranked and not prefer_albums:
                # Track-only mode keeps the old early exit; with album
                # preference the album queries must run too.
                break
            if not ranked:
                logger.info(
                    'usenet: no confident match for query={!r} title={!r} '
                    'releases={} min_score={}',
                    query[:120],
                    song.get('name'),
                    len(releases),
                    min_score,
                )
        if album_only:
            ranked = [
                row for row in ranked if row.get('match_kind') == 'album'
            ]
        _sort_ranked(ranked, prefer_albums)

        if not ranked:
            logger.info(
                'usenet: no matching releases title={!r} queries={}',
                song.get('name'),
                queries[:6],
            )
            return None

        report(36.0, f'queued ({len(ranked)} candidates)')

        for attempt_idx, release in enumerate(ranked, start=1):
            _check_cancel(cancelled)
            if _past_deadline(deadline):
                logger.info(
                    'usenet: download timed out title={!r} tried={}/{}',
                    song.get('name'),
                    attempt_idx - 1,
                    len(ranked),
                )
                break
            release_name = str(release.get('title') or '')[:120]
            nzb_url = str(release.get('nzb_url') or '')
            is_album = release.get('match_kind') == 'album'
            if album_only and not is_album:
                continue

            # Hard URL-level guard: one NZB is handed to SABnzbd once,
            # even if songs with different album keys selected it.
            nzb_state, nzb_cached, nzb_event = _claim_nzb(nzb_url)
            if nzb_state == 'cached' and nzb_cached is not None:
                if album_only:
                    return nzb_cached
                found = _pick_track_in_album(nzb_cached, spotify_row, settings)
                if found is not None and (
                    verify_downloaded_file_matches_spotify(found, spotify_row)
                ):
                    return found
                if found is not None:
                    _discard_mismatched_download(found)
                continue
            if nzb_state == 'wait' and nzb_event is not None:
                while not nzb_event.wait(1.0):
                    if _past_deadline(deadline):
                        break
                cached = _nzb_folder(nzb_url)
                if cached is not None:
                    if album_only:
                        return cached
                    found = _pick_track_in_album(cached, spotify_row, settings)
                    if found is not None and (
                        verify_downloaded_file_matches_spotify(
                            found, spotify_row
                        )
                    ):
                        return found
                    if found is not None:
                        _discard_mismatched_download(found)
                continue
            claim_event = nzb_event if nzb_state == 'owner' else None
            claim_done = False

            # Only one download per album: a parallel song of the same
            # album waits for the in-flight fetch and reuses its folder.
            owner_event: Optional[threading.Event] = None
            if is_album and album_key:
                with _ALBUM_CACHE_LOCK:
                    event = _ALBUM_EVENTS.get(album_key)
                    if event is None:
                        owner_event = threading.Event()
                        _ALBUM_EVENTS[album_key] = owner_event
                if owner_event is None:
                    report(40.0, 'waiting for album download')
                    while not event.wait(1.0):
                        if _past_deadline(deadline):
                            break
                    cached = _album_cache_get(album_key)
                    found = (
                        _pick_track_in_album(cached, spotify_row, settings)
                        if cached is not None
                        else None
                    )
                    if found is not None and (
                        verify_downloaded_file_matches_spotify(
                            found, spotify_row
                        )
                    ):
                        logger.info(
                            'usenet: reused album fetched in parallel '
                            'title={!r}',
                            song.get('name'),
                        )
                        return found
                    if found is not None:
                        _discard_mismatched_download(found)
                    continue

            try:
                nzo_id: Optional[str] = None
                try:
                    nzo_id = client.add_url(nzb_url, release_name)
                except Exception as exc:
                    logger.info(
                        'usenet: addurl failed title={!r} release={!r} err={}',
                        song.get('name'),
                        release_name,
                        exc,
                    )
                if nzo_id is None:
                    # Some indexers only serve NZBs to the logged-in
                    # client; fetch the bytes ourselves and upload them
                    # instead.
                    indexer = next(
                        (
                            c
                            for c in indexers
                            if c.name == release.get('indexer')
                        ),
                        indexers[0],
                    )
                    try:
                        nzb_bytes = indexer.fetch_nzb(nzb_url)
                        nzo_id = client.add_file(nzb_bytes, release_name)
                    except Exception as exc:
                        logger.info(
                            'usenet: addfile fallback failed title={!r} '
                            'release={!r} err={}',
                            song.get('name'),
                            release_name,
                            exc,
                        )
                if nzo_id is None:
                    continue

                try:
                    slot = _wait_for_sabnzbd(
                        client,
                        nzo_id,
                        settings,
                        progress_cb=progress_cb,
                        deadline=deadline,
                        cancelled=cancelled,
                    )
                except DownloadCancelled:
                    # Stop the SABnzbd job we just submitted instead of
                    # leaving it to download in the background.
                    client.cancel_job(nzo_id)
                    raise
                _check_cancel(cancelled)
                if slot is None:
                    continue

                if album_only:
                    release_folder = _album_storage_folder(
                        complete_dir, str(slot.get('storage') or '')
                    )
                    if release_folder is not None and _folder_has_audio(
                        release_folder
                    ):
                        for key in cache_keys:
                            _album_cache_put(key, release_folder)
                        _finish_nzb_claim(nzb_url, claim_event, release_folder)
                        claim_done = True
                        return release_folder
                    continue

                if is_album:
                    found = _find_album_track(
                        complete_dir,
                        str(slot.get('storage') or ''),
                        spotify_row,
                        settings,
                    )
                else:
                    found = _find_completed_file(
                        complete_dir,
                        str(slot.get('storage') or ''),
                        release_name,
                        spotify_row,
                    )
                if found is None:
                    logger.info(
                        'usenet: completed file not found under {} nzo={} '
                        'release={!r}',
                        complete_dir,
                        nzo_id,
                        release_name,
                    )
                    continue

                if not verify_downloaded_file_matches_spotify(
                    found, spotify_row
                ):
                    meta = read_audio_metadata(found)
                    mismatch_row = {
                        **spotify_row,
                        'name': str(meta.get('title') or ''),
                        'artists': list(meta.get('artists') or []),
                        'library_from_tags': True,
                    }
                    logger.info(
                        'usenet: tags mismatch after download {}; trying '
                        'next candidate ({}/{}) file={!r}',
                        spotify_file_tag_mismatch_label(mismatch_row),
                        attempt_idx,
                        len(ranked),
                        _file_basename(found.name)[:120],
                    )
                    _discard_mismatched_download(found)
                    continue

                if is_album:
                    # Keep the whole release folder (including SABnzbd's
                    # nfo/cover files) for the album's other songs; the
                    # batch/cleanup button removes it afterwards.
                    release_folder = (
                        _album_storage_folder(
                            complete_dir, str(slot.get('storage') or '')
                        )
                        or found.parent
                    )
                    for key in cache_keys:
                        _album_cache_put(key, release_folder)
                    _finish_nzb_claim(nzb_url, claim_event, release_folder)
                    claim_done = True
                elif bool(settings.get('cleanup', True)):
                    _schedule_cleanup(client, nzo_id)
                logger.info(
                    'usenet: picked score={} reasons={} indexer={!r} '
                    'release={!r}',
                    release.get('match_score'),
                    ','.join(release.get('match_reasons') or []),
                    release.get('indexer'),
                    release_name,
                )
                return found
            finally:
                if claim_event is not None and not claim_done:
                    _finish_nzb_claim(nzb_url, claim_event)
                if owner_event is not None:
                    owner_event.set()
                    with _ALBUM_CACHE_LOCK:
                        if _ALBUM_EVENTS.get(album_key) is owner_event:
                            _ALBUM_EVENTS.pop(album_key, None)

        logger.info(
            'usenet: all candidates failed title={!r} tried={}',
            song.get('name'),
            len(ranked),
        )
        return None
