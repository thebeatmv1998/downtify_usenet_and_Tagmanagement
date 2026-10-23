import asyncio

import pytest

from downtify import album_merge, api
from downtify.album_merge_progress import MergeProgress


def test_progress_is_separate_and_returned_as_snapshot():
    progress = MergeProgress()
    progress.start('scan')
    progress.start('apply')
    progress.update('scan', 'matching', 2, 10)
    snapshot = progress.get('scan')
    assert snapshot == {
        'stage': 'matching',
        'completed': 2,
        'total': 10,
        'running': True,
    }
    snapshot['completed'] = 999
    assert progress.get('scan')['completed'] == 2
    assert progress.get('apply')['completed'] == 0
    assert progress.get('missing') == {}
    progress.finish('scan')
    assert progress.get('scan')['stage'] == 'done'
    assert not progress.get('scan')['running']


def test_active_operation_cannot_be_replaced():
    progress = MergeProgress()
    progress.start('scan')
    with pytest.raises(RuntimeError):
        progress.start('scan')
    progress.finish('scan', failed=True)
    assert progress.get('scan')['stage'] == 'failed'
    progress.start('scan')
    assert progress.get('scan')['running']


def test_progress_history_is_bounded():
    progress = MergeProgress()
    for index in range(100):
        progress.start(str(index))
        progress.finish(str(index))
    assert not progress.get('0')
    assert progress.get('99')['stage'] == 'done'


def test_parallel_lookups_report_completed_work():
    events = []
    tasks = {str(index): ({'name': str(index)}, '') for index in range(10)}
    results = album_merge._resolve_tasks(
        lambda _song, _id: {'album_name': 'Album'},
        tasks,
        on_progress=lambda *event: events.append(event),
    )
    assert len(results) == 10
    assert events == [('matching', index, 10) for index in range(11)]


def test_preview_endpoint_publishes_progress(monkeypatch):
    monkeypatch.setattr(api, '_merge_progress', MergeProgress())
    monkeypatch.setattr(api, 'library_context', lambda: None)

    def detect(_ctx, **kwargs):
        kwargs['on_progress']('grouping', 3, 5)
        assert api._merge_progress.get('preview')['completed'] == 3
        return []

    monkeypatch.setattr(album_merge, 'detect_split_albums', detect)
    result = asyncio.run(
        api.library_albums_merge_preview(operation_id='preview')
    )
    assert result['candidates'] == []
    snapshot = asyncio.run(api.library_albums_merge_progress('preview'))
    assert snapshot['stage'] == 'done'
    assert not snapshot['running']


def test_failed_merge_endpoint_finishes_progress(monkeypatch):
    monkeypatch.setattr(api, '_merge_progress', MergeProgress())
    monkeypatch.setattr(api.state, 'downloader', object())
    monkeypatch.setattr(api, 'library_context', lambda: None)

    def apply(*_args, **kwargs):
        kwargs['on_progress']('merging', 1, 10)
        raise OSError('Disk full')

    monkeypatch.setattr(album_merge, 'apply_merge', apply)
    with pytest.raises(OSError, match='Disk full'):
        asyncio.run(
            api.library_albums_merge(
                payload={'plans': []}, operation_id='apply'
            )
        )
    snapshot = api._merge_progress.get('apply')
    assert snapshot['stage'] == 'failed'
    assert not snapshot['running']
