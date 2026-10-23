"""The generation switch behind Stop all downloads."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from downtify import api, download_control
from downtify.downloader import Downloader


def test_a_stop_cancels_old_generations_but_not_new_ones():
    old = download_control.current_generation()
    new = download_control.stop_all()

    assert new > old
    assert download_control.is_cancelled(old) is True
    assert download_control.is_cancelled(new) is False


def test_downloader_refuses_a_cancelled_generation(tmp_path: Path):
    old = download_control.current_generation()
    download_control.stop_all()
    downloader = Downloader(tmp_path)

    with pytest.raises(download_control.DownloadCancelled):
        downloader.download(
            {'name': 'Song', 'artists': ['Artist']},
            cancel_generation=old,
        )


def test_sleep_interruptible_returns_as_soon_as_a_stop_arrives(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(download_control.time, 'sleep', slept.append)
    checks = {'n': 0}

    def cancelled() -> bool:
        checks['n'] += 1
        return checks['n'] > 2

    assert download_control.sleep_interruptible(30, cancelled) is True
    # Two full 250 ms slices, then the third check stops the wait.
    assert slept == [0.25, 0.25]


def test_sleep_interruptible_runs_a_whole_sleep_without_a_stop(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(download_control.time, 'sleep', slept.append)

    assert download_control.sleep_interruptible(1, lambda: False) is False
    assert slept == [0.25, 0.25, 0.25, 0.25]


class _Task:
    def __init__(self):
        self.cancelled = False

    def done(self):
        return False

    def cancel(self):
        self.cancelled = True


class _Batches:
    def __init__(self):
        self.finished: list[tuple] = []

    def list_open_batches(self):
        return [
            {'id': 1, 'status': 'in_progress', 'succeeded_count': 3},
            {'id': 2, 'status': 'incomplete', 'succeeded_count': 1},
        ]

    def finish_batch(
        self,
        batch_id: int,
        succeeded: int,
        failed: int,
        *,
        status: str,
    ):
        self.finished.append((batch_id, succeeded, failed, status))


def test_stop_endpoint_cancels_tasks_clears_jobs_and_closes_batches(
    monkeypatch,
):
    task = _Task()
    batches = _Batches()

    async def _broadcast(_message: Any):
        return None

    monkeypatch.setattr(
        api.state,
        'download_jobs',
        {
            'a': {'status': 'downloading'},
            'b': {'status': 'queued'},
            'c': {'status': 'done'},
        },
    )
    monkeypatch.setattr(api.state, 'download_tasks', {task})
    monkeypatch.setattr(api.state, 'playlist_batch_store', batches)
    monkeypatch.setattr(api.state.connections, 'broadcast', _broadcast)

    result = asyncio.run(api.stop_all_downloads_endpoint())

    assert result['stopped'] == 2
    assert result['tasks'] == 1
    assert result['batches'] == 1
    assert task.cancelled is True
    assert api.state.download_jobs == {}
    assert batches.finished == [(1, 3, 0, 'incomplete')]
