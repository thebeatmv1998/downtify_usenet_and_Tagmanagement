"""Thread-safe, bounded progress snapshots for album merge requests."""

from __future__ import annotations

import threading
from typing import Any


class MergeProgress:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._operations: dict[str, dict[str, Any]] = {}

    def start(self, operation_id: str) -> None:
        with self._lock:
            previous = self._operations.get(operation_id)
            if previous and previous['running']:
                raise RuntimeError('Operation already running')
            if len(self._operations) >= 64:
                finished = next(
                    (
                        key
                        for key, item in self._operations.items()
                        if not item['running']
                    ),
                    None,
                )
                if finished is None:
                    raise RuntimeError('Too many merge operations')
                del self._operations[finished]
            self._operations[operation_id] = {
                'stage': 'reading',
                'completed': 0,
                'total': 0,
                'running': True,
            }

    def update(
        self, operation_id: str, stage: str, completed: int, total: int
    ) -> None:
        with self._lock:
            item = self._operations.get(operation_id)
            if item is not None:
                item.update(stage=stage, completed=completed, total=total)

    def finish(self, operation_id: str, *, failed: bool = False) -> None:
        with self._lock:
            item = self._operations.get(operation_id)
            if item is not None:
                item.update(
                    running=False, stage='failed' if failed else 'done'
                )

    def get(self, operation_id: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._operations.get(operation_id) or {})
