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

import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
from loguru import logger

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


# ── Newznab ──────────────────────────────────────────────────────────
class NewznabClient:
    """Minimal Newznab API client for one indexer."""

    def __init__(self, indexer: dict[str, Any], timeout: int = 20):
        self.name = str(indexer.get('name') or '').strip() or 'indexer'
        self.base_url = (
            str(indexer.get('base_url') or '').strip().rstrip('/')
        )
        self.api_key = str(indexer.get('api_key') or '').strip()
        self.timeout = timeout

    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    def _get(self, params: dict[str, Any], **kwargs: Any) -> httpx.Response:
        resp = httpx.get(
            f'{self.base_url}/api',
            params=params,
            timeout=self.timeout,
            follow_redirects=True,
            **kwargs,
        )
        resp.raise_for_status()
        return resp

    def search(self, query: str) -> list[dict[str, Any]]:
        """Run an audio-category search; returns parsed release rows."""
        resp = self._get({
            't': 'search',
            'q': query,
            'cat': '3000,3010',
            'apikey': self.api_key,
            'extended': '1',
            'limit': '100',
        })
        return parse_newznab_xml(resp.content, self.name)

    def fetch_nzb(self, url: str) -> bytes:
        """Download the raw NZB bytes (used when SABnzbd can't fetch them)."""
        resp = httpx.get(
            url,
            timeout=self.timeout,
            follow_redirects=True,
        )
        resp.raise_for_status()
        return resp.content


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
    """Score bonus when the release title advertises a preferred format."""
    folded = title.casefold()
    for ext in extensions:
        if ext == 'flac' and re.search(r'(?:^|[^a-z])flac(?:[^a-z]|$)', folded):
            return 3, 'ext_flac'
        if ext == 'mp3' and (
            re.search(r'(?:^|[^a-z0-9])(?:320|mp3)(?:[^a-z0-9]|$)', folded)
        ):
            return 2, 'ext_mp3'
        if ext not in {'flac', 'mp3'} and ext in folded:
            return 2, f'ext_{ext}'
    return 0, ''


def _implied_bitrate_ok(size: int, duration: int) -> bool:
    """Plausibility of the NZB size against the track's runtime."""
    if size <= 0 or duration <= 0:
        return True
    kbps = (size * 8) / (duration * 1000)
    return _MIN_IMPLIED_KBPS <= kbps <= _MAX_IMPLIED_KBPS


def _score_usenet_candidate(  # noqa: PLR0914  # mirrors slskd scorer
    song: dict[str, Any],
    release: dict[str, Any],
    *,
    target_duration: int,
    settings: Optional[dict[str, Any]] = None,
) -> Optional[tuple[int, list[str]]]:
    """Return (score, reasons) or None when hard requirements fail."""
    tol = duration_tolerances_from_settings(settings)
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
    if (title or full_title) and not title_ok:
        return None
    if artist and artist not in release_alnum:
        return None

    duration = _to_int(release.get('duration'))
    if duration and target_duration:
        if not media_duration_matches_song(
            song,
            duration,
            tolerance_seconds=tol['seconds'],
            tolerance_percent=tol['percent'],
        ):
            return None

    size = _to_int(release.get('size'))
    if not _implied_bitrate_ok(size, duration or target_duration):
        return None

    score = 0
    reasons: list[str] = []

    if artist and release_alnum.startswith(artist):
        score += 3
        reasons.append('artist_prefix')
    elif artist:
        score += 1
        reasons.append('artist')

    if title and title in release_alnum:
        score += 3
        reasons.append('title')
    elif full_title and full_title in release_alnum:
        score += 2
        reasons.append('title_full')

    if album and album in release_alnum:
        score += 2
        reasons.append('album')

    if duration and target_duration:
        if abs(target_duration - duration) <= _STRICT_DURATION_SECONDS:
            score += 3
            reasons.append('duration_3s')

    raw_ext = (settings or {}).get('extensions') or ['flac', 'mp3']
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

    return score, reasons


def _rank_usenet_candidates(
    song: dict[str, Any],
    releases: list[dict[str, Any]],
    settings: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """Score and sort all viable releases (highest confidence first)."""
    target_duration = _song_duration_seconds(song)
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
        score, reasons = scored
        ranked.append({
            **release,
            'match_score': score,
            'match_reasons': reasons,
        })
    ranked.sort(
        key=lambda row: (
            -_to_int(row.get('match_score')),
            -_to_int(row.get('grabs')),
        )
    )
    return ranked


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
        futures = {pool.submit(client.search, query): client
                   for client in indexers}
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
) -> Optional[dict[str, Any]]:
    """Poll queue/history until the job completes; returns the history slot."""
    interval = _int_setting(
        settings, 'poll_interval_seconds', 5, minimum=1, maximum=30
    )
    attempts = _int_setting(
        settings, 'poll_max_attempts', 180, minimum=1, maximum=10000
    )
    for attempt in range(max(1, attempts)):
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
        time.sleep(max(1, interval))
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


def download_from_usenet(  # noqa: PLR0914  # search, rank, queue, poll, verify
    song: dict[str, Any],
    settings: dict[str, Any],
    progress_cb: Optional[ProgressCallback] = None,
) -> Optional[Path]:
    """Search Newznab indexers and download the best release via SABnzbd.

    Mirrors :func:`downtify.slskd_provider.download_from_slskd`: returns the
    path of the finished audio file, or ``None`` when nothing usable was
    found (the downloader then tries the next audio provider).
    """
    if not bool(settings.get('enabled')):
        return None

    timeout = _int_setting(settings, 'timeout_seconds', 20, minimum=5,
                           maximum=120)
    raw_indexers = settings.get('indexers')
    indexers = [
        NewznabClient(row, timeout=timeout)
        for row in (raw_indexers if isinstance(raw_indexers, list) else [])
        if isinstance(row, dict) and row.get('enabled')
    ]
    indexers = [client for client in indexers if client.configured()]
    client = SabnzbdClient(settings, timeout=timeout)
    if not client.configured() or not indexers:
        logger.info(
            'usenet: missing configuration title={!r}', song.get('name')
        )
        return None

    spotify_row = snapshot_spotify_metadata(song)
    queries = _usenet_search_queries(spotify_row)
    if not queries:
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
        settings, 'min_score', _DEFAULT_MATCH_MIN_SCORE,
        minimum=0, maximum=100,
    )
    sab = settings.get('sabnzbd')
    sab = sab if isinstance(sab, dict) else {}
    complete_dir = Path(str(sab.get('complete_dir') or '/usenet'))

    with _usenet_semaphore(settings):
        report(5.0, 'searching')
        ranked: list[dict[str, Any]] = []
        for query in queries:
            releases = _search_indexers(indexers, query)
            if not releases:
                continue
            ranked = [
                row
                for row in _rank_usenet_candidates(song, releases, settings)
                if _to_int(row.get('match_score')) >= min_score
            ]
            if ranked:
                break
            logger.info(
                'usenet: no confident match for query={!r} title={!r} '
                'releases={} min_score={}',
                query[:120],
                song.get('name'),
                len(releases),
                min_score,
            )

        if not ranked:
            logger.info(
                'usenet: no matching releases title={!r} queries={}',
                song.get('name'),
                queries[:6],
            )
            return None

        report(36.0, f'queued ({len(ranked)} candidates)')

        for attempt_idx, release in enumerate(ranked, start=1):
            release_name = str(release.get('title') or '')[:120]
            nzb_url = str(release.get('nzb_url') or '')
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
                # Some indexers only serve NZBs to the logged-in client;
                # fetch the bytes ourselves and upload them instead.
                indexer = next(
                    (c for c in indexers if c.name == release.get('indexer')),
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

            slot = _wait_for_sabnzbd(
                client, nzo_id, settings, progress_cb=progress_cb
            )
            if slot is None:
                continue

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

            if not verify_downloaded_file_matches_spotify(found, spotify_row):
                meta = read_audio_metadata(found)
                mismatch_row = {
                    **spotify_row,
                    'name': str(meta.get('title') or ''),
                    'artists': list(meta.get('artists') or []),
                    'library_from_tags': True,
                }
                logger.info(
                    'usenet: tags mismatch after download {}; trying next '
                    'candidate ({}/{}) file={!r}',
                    spotify_file_tag_mismatch_label(mismatch_row),
                    attempt_idx,
                    len(ranked),
                    _file_basename(found.name)[:120],
                )
                _discard_mismatched_download(found)
                continue

            if bool(settings.get('cleanup', True)):
                _schedule_cleanup(client, nzo_id)
            logger.info(
                'usenet: picked score={} reasons={} indexer={!r} release={!r}',
                release.get('match_score'),
                ','.join(release.get('match_reasons') or []),
                release.get('indexer'),
                release_name,
            )
            return found

        logger.info(
            'usenet: all candidates failed title={!r} tried={}',
            song.get('name'),
            len(ranked),
        )
        return None
