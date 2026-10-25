"""Security hardening: headers, hidden-file access and feed SSRF."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from downtify import api, podcasts
from downtify.security_headers import SecurityHeadersMiddleware
from downtify.url_safety import UnsafeUrl


def _client() -> TestClient:
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get('/')
    def index():
        return PlainTextResponse('ok')

    return TestClient(app)


def test_every_response_has_hardening_headers():
    response = _client().get('/')
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert response.headers['referrer-policy'] == 'no-referrer'
    assert response.headers['x-frame-options'] == 'DENY'
    assert 'camera=()' in response.headers['permissions-policy']


def test_https_gets_hsts_but_plain_http_does_not():
    assert 'strict-transport-security' not in _client().get('/').headers
    secure = TestClient(_client().app, base_url='https://testserver')
    assert 'strict-transport-security' in secure.get('/').headers


def test_a_not_found_response_still_carries_the_headers():
    response = _client().get('/missing')
    assert response.status_code == 404
    assert response.headers['x-content-type-options'] == 'nosniff'


class _Rules:
    def hides(self, entry):
        return entry.get('content_key') == 'hidden-key'


def test_hidden_library_files_are_refused_for_users(monkeypatch):
    monkeypatch.setattr(api, 'request_is_admin', lambda request: False)
    rules = _Rules()
    monkeypatch.setattr(api, 'hidden_rules', lambda: rules)
    monkeypatch.setattr(
        api,
        'listing_for_request',
        lambda ctx: [
            {'file': 'visible.mp3', 'content_key': 'visible-key'},
            {'file': 'hidden.mp3', 'content_key': 'hidden-key'},
        ],
    )

    request = object()
    assert api.file_visible_to('visible.mp3', request) is True
    assert api.file_visible_to('hidden.mp3', request) is False
    # Not a library row at all: podcasts and sidecars are not governed.
    assert api.file_visible_to('Podcasts/Show/episode.mp3', request) is True


def test_an_admin_sees_hidden_files(monkeypatch):
    monkeypatch.setattr(api, 'request_is_admin', lambda request: True)
    assert api.file_visible_to('hidden.mp3', object()) is True


def test_without_hidden_rules_every_file_is_readable(monkeypatch):
    monkeypatch.setattr(api, 'request_is_admin', lambda request: False)
    monkeypatch.setattr(api, 'hidden_rules', lambda: None)
    assert api.file_visible_to('anything.mp3', object()) is True


def test_a_private_podcast_feed_is_not_fetched(monkeypatch):
    monkeypatch.delenv('DOWNTIFY_ALLOW_PRIVATE_FEEDS', raising=False)
    with pytest.raises(UnsafeUrl):
        podcasts.fetch_feed('http://127.0.0.1:8000/feed.xml')


def test_a_private_episode_enclosure_is_not_downloaded(tmp_path, monkeypatch):
    monkeypatch.delenv('DOWNTIFY_ALLOW_PRIVATE_FEEDS', raising=False)
    episode = podcasts.EpisodeInfo(
        guid='g1',
        title='Episode',
        description='',
        published_at='',
        duration_seconds=0,
        season_number=None,
        episode_number=None,
        enclosure_url='http://169.254.169.254/latest/meta-data/',
        enclosure_type='audio/mpeg',
    )
    with pytest.raises(UnsafeUrl):
        podcasts.download_episode(
            episode,
            show_name='Show',
            author='Author',
            folder_name='Show',
            download_dir=tmp_path,
            cover_bytes=None,
        )
    assert not list(tmp_path.rglob('*.mp3'))
