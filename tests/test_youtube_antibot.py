"""Tests for the YouTube anti-bot hardening: PO Tokens, shared yt-dlp
options for the fallback search, cookies.txt sanity checks, and the
translated "Sign in to confirm you're not a bot" error."""

from __future__ import annotations

from downtify import downloader as downloader_mod
from downtify import providers as providers_mod
from downtify.cookies import check_cookies_file, default_cookiefile
from downtify.downloader import Downloader, youtube_extractor_args


class _CapturedOpts(Exception):
    """Raised by the fakes below once yt-dlp's opts have been recorded."""


class _FakeYoutubeDL:
    """Stands in for yt_dlp.YoutubeDL, capturing the opts dict."""

    captured: dict = {}

    def __init__(self, opts):
        _FakeYoutubeDL.captured = opts
        self.params = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def add_post_processor(self, pp, when='post_process'):
        pass

    def download(self, urls):  # noqa: PLR6301
        raise _CapturedOpts

    def extract_info(self, url, download=False):  # noqa: PLR6301
        return {'entries': [{'id': 'abc123def45', 'title': 'A - Song'}]}


_SONG = {
    'name': 'Song',
    'artists': ['Artist'],
    'youtube_id': 'abc123def45',
    'album_name': 'Album',
    'cover_url': 'https://example.com/cover.jpg',
}


# ── youtube_extractor_args ────────────────────────────────────────────────


def test_extractor_args_omit_po_token_when_env_unset(monkeypatch):
    monkeypatch.delenv('DOWNTIFY_YT_PO_TOKEN', raising=False)
    args = youtube_extractor_args()
    assert 'player_client' in args['youtube']
    assert 'po_token' not in args['youtube']


def test_extractor_args_include_po_tokens_from_env(monkeypatch):
    monkeypatch.setenv(
        'DOWNTIFY_YT_PO_TOKEN', ' mweb.gvs+ABC123 , web.gvs+XYZ987 '
    )
    args = youtube_extractor_args()
    assert args['youtube']['po_token'] == [
        'mweb.gvs+ABC123',
        'web.gvs+XYZ987',
    ]


def test_extractor_args_respect_player_client_env(monkeypatch):
    monkeypatch.setenv('DOWNTIFY_YT_PLAYER_CLIENTS', 'tv,mweb')
    monkeypatch.delenv('DOWNTIFY_YT_PO_TOKEN', raising=False)
    args = youtube_extractor_args()
    assert args['youtube']['player_client'] == ['tv', 'mweb']


def test_download_ydl_opts_include_po_token(tmp_path, monkeypatch):
    """The download path must carry the PO Token through to yt-dlp —
    without it the GVS-gated ios/android formats are skipped and the
    bot check is the only thing left."""
    monkeypatch.setenv('DOWNTIFY_YT_PO_TOKEN', 'mweb.gvs+ABC123')
    monkeypatch.setattr(downloader_mod.yt_dlp, 'YoutubeDL', _FakeYoutubeDL)
    d = Downloader(tmp_path)
    try:
        d.download(dict(_SONG))
    except _CapturedOpts:
        pass
    youtube_args = _FakeYoutubeDL.captured['extractor_args']['youtube']
    assert youtube_args['po_token'] == ['mweb.gvs+ABC123']


# ── bot-check error translation ────────────────────────────────────────────


_BOT_CHECK_ERROR = RuntimeError(
    'ERROR: [youtube] lENt7-t8I-I: Sign in to confirm you\u2019re not a '
    'bot. Use --cookies-from-browser or --cookies for the '
    'authentication.'
)


def test_bot_check_error_without_cookies_points_to_settings():
    translated = downloader_mod._translate_download_error(
        _BOT_CHECK_ERROR, {'name': 'Some Track'}, has_cookies=False
    )
    message = str(translated)
    assert isinstance(translated, RuntimeError)
    assert 'Some Track' in message
    assert 'Settings > YouTube cookies' in message
    assert 'DOWNTIFY_COOKIES_FILE' in message
    # Raw yt-dlp CLI advice means nothing in the web UI.
    assert '--cookies-from-browser' not in message


def test_bot_check_error_with_cookies_suggests_fresh_export():
    translated = downloader_mod._translate_download_error(
        _BOT_CHECK_ERROR, {'name': 'Some Track'}, has_cookies=True
    )
    message = str(translated)
    assert 'not accepted' in message
    assert 'DOWNTIFY_YT_PO_TOKEN' in message


def test_age_gate_wording_is_not_misread_as_bot_check():
    # "Sign in to confirm your age" shares the "sign in to confirm"
    # fragment — it must still get the age-gate message.
    error = RuntimeError(
        'Sign in to confirm your age. This video may be inappropriate '
        'for some users.'
    )
    translated = downloader_mod._translate_download_error(
        error, {'name': 'Some Track'}, has_cookies=False
    )
    assert 'explicit content' in str(translated)


# ── cookies.txt sanity checks ──────────────────────────────────────────────


_FAR_FUTURE = '4102444800'  # 2100-01-01
_FAR_PAST = '946684800'  # 2000-01-01


def _jar(*lines: str) -> bytes:
    return ('# Netscape HTTP Cookie File\n' + '\n'.join(lines)).encode()


def test_check_cookies_file_accepts_fresh_youtube_jar(tmp_path):
    path = tmp_path / 'cookies.txt'
    path.write_bytes(
        _jar(
            f'.youtube.com\tTRUE\t/\tTRUE\t{_FAR_FUTURE}\tSID\tsession',
            '.google.com\tTRUE\t/\tTRUE\t0\tHSID\tsession-only',
        )
    )
    ok, warning = check_cookies_file(path)
    assert ok
    assert warning is None


def test_check_cookies_file_warns_when_all_youtube_cookies_expired(
    tmp_path,
):
    path = tmp_path / 'cookies.txt'
    path.write_bytes(
        _jar(f'.youtube.com\tTRUE\t/\tTRUE\t{_FAR_PAST}\tSID\tstale')
    )
    ok, warning = check_cookies_file(path)
    assert ok
    assert warning is not None
    assert 'expired' in warning


def test_check_cookies_file_session_cookies_are_not_expired(tmp_path):
    # Expiry 0 = browser session cookie; export extensions usually don't
    # persist those, but when present they must not trigger the warning.
    path = tmp_path / 'cookies.txt'
    path.write_bytes(_jar('.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tlive'))
    ok, warning = check_cookies_file(path)
    assert ok
    assert warning is None


def test_check_cookies_file_warns_without_youtube_cookies(tmp_path):
    path = tmp_path / 'cookies.txt'
    path.write_bytes(
        _jar(f'.example.com\tTRUE\t/\tTRUE\t{_FAR_FUTURE}\tSID\tvalue')
    )
    ok, warning = check_cookies_file(path)
    assert ok
    assert warning is not None
    assert 'youtube.com' in warning


def test_check_cookies_file_rejects_non_netscape_content(tmp_path):
    path = tmp_path / 'cookies.txt'
    path.write_bytes(b'SID=abc; HSID=def\n')
    ok, warning = check_cookies_file(path)
    assert not ok
    assert warning is not None


def test_check_cookies_file_missing_file_is_not_ok(tmp_path):
    ok, warning = check_cookies_file(tmp_path / 'nope.txt')
    assert not ok
    assert warning is not None


# ── default_cookiefile ─────────────────────────────────────────────────────


def test_default_cookiefile_prefers_env_var(tmp_path, monkeypatch):
    env_file = tmp_path / 'env.txt'
    env_file.write_text('x')
    monkeypatch.setenv('DOWNTIFY_COOKIES_FILE', str(env_file))
    store_dir = tmp_path / 'data'
    store_dir.mkdir()
    (store_dir / 'cookies.txt').write_text('y')
    monkeypatch.setenv('DATABASE_DIR', str(store_dir))
    assert default_cookiefile() == str(env_file)


def test_default_cookiefile_falls_back_to_database_dir(tmp_path, monkeypatch):
    monkeypatch.delenv('DOWNTIFY_COOKIES_FILE', raising=False)
    store_dir = tmp_path / 'data'
    store_dir.mkdir()
    uploaded = store_dir / 'cookies.txt'
    uploaded.write_text('y')
    monkeypatch.setenv('DATABASE_DIR', str(store_dir))
    assert default_cookiefile() == str(uploaded)


def test_default_cookiefile_empty_when_nothing_exists(tmp_path, monkeypatch):
    monkeypatch.delenv('DOWNTIFY_COOKIES_FILE', raising=False)
    monkeypatch.setenv('DATABASE_DIR', str(tmp_path))
    assert not default_cookiefile()


# ── _youtube_search shares the download path's yt-dlp hardening ────────────


def test_youtube_search_uses_cookies_and_extractor_args(monkeypatch):
    monkeypatch.setenv('DOWNTIFY_YT_PO_TOKEN', 'mweb.gvs+ABC123')
    monkeypatch.setattr(
        providers_mod, 'default_cookiefile', lambda: '/data/cookies.txt'
    )
    monkeypatch.setattr(providers_mod.yt_dlp, 'YoutubeDL', _FakeYoutubeDL)
    entries = providers_mod._youtube_search('Artist Song', 3)
    opts = _FakeYoutubeDL.captured
    assert opts['cookiefile'] == '/data/cookies.txt'
    youtube_args = opts['extractor_args']['youtube']
    assert youtube_args['po_token'] == ['mweb.gvs+ABC123']
    assert 'player_client' in youtube_args
    assert opts['remote_components'] == ['ejs:github']
    assert entries[0]['id'] == 'abc123def45'


def test_youtube_search_omits_cookiefile_when_none_configured(monkeypatch):
    monkeypatch.delenv('DOWNTIFY_YT_PO_TOKEN', raising=False)
    monkeypatch.setattr(providers_mod, 'default_cookiefile', lambda: '')
    monkeypatch.setattr(providers_mod.yt_dlp, 'YoutubeDL', _FakeYoutubeDL)
    providers_mod._youtube_search('Artist Song', 3)
    assert 'cookiefile' not in _FakeYoutubeDL.captured
    youtube_args = _FakeYoutubeDL.captured['extractor_args']['youtube']
    assert 'po_token' not in youtube_args


def test_extractor_args_omit_pot_provider_when_env_unset(monkeypatch):
    monkeypatch.delenv('DOWNTIFY_YT_POT_PROVIDER', raising=False)
    args = youtube_extractor_args()
    assert 'youtubepot-bgutilhttp' not in args['youtube']


def test_extractor_args_point_pot_provider_at_sidecar(monkeypatch):
    monkeypatch.setenv('DOWNTIFY_YT_POT_PROVIDER', 'http://potside:4416/')
    args = youtube_extractor_args()
    # The provider is its own top-level extractor_args group; nesting it
    # under 'youtube' is silently ignored by yt-dlp.
    assert args['youtubepot-bgutilhttp'] == {
        'base_url': ['http://potside:4416']
    }
    assert 'youtubepot-bgutilhttp' not in args['youtube']


def test_plugin_warnings_are_quiet_without_a_provider(monkeypatch):
    # The plugin probes its default localhost URL when no sidecar is
    # configured; those notices are noise, not errors.
    from downtify.downloader import _YtdlpLogger

    monkeypatch.delenv('DOWNTIFY_YT_POT_PROVIDER', raising=False)
    emitted: list[str] = []
    monkeypatch.setattr(
        'downtify.downloader.logger.warning', lambda m, *a, **k: emitted.append(str(m))
    )
    _YtdlpLogger.warning(
        'No base_url provided, defaulting to http://127.0.0.1:4416'
    )
    assert emitted == []
    # With a provider configured, the same message must stay visible.
    monkeypatch.setenv('DOWNTIFY_YT_POT_PROVIDER', 'http://potside:4416')
    _YtdlpLogger.warning(
        'No base_url provided, defaulting to http://127.0.0.1:4416'
    )
    assert len(emitted) == 1


def test_bot_check_message_reports_the_pot_provider_state(monkeypatch):
    monkeypatch.setenv('DOWNTIFY_YT_POT_PROVIDER', 'http://potside:4416')
    message = str(
        downloader_mod._translate_download_error(
            _BOT_CHECK_ERROR, {'name': 'Some Track'}, has_cookies=False
        )
    )
    assert 'PO Token provider is configured at http://potside:4416' in message


def test_bot_check_message_suggests_the_pot_provider_when_unset(monkeypatch):
    monkeypatch.delenv('DOWNTIFY_YT_POT_PROVIDER', raising=False)
    message = str(
        downloader_mod._translate_download_error(
            _BOT_CHECK_ERROR, {'name': 'Some Track'}, has_cookies=False
        )
    )
    assert 'DOWNTIFY_YT_POT_PROVIDER' in message
    assert 'bgutil-ytdlp-pot-provider' in message
