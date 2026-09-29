"""只追加事件存储。

事实一旦写入不可修改或删除：更正只能以新事件表达。
每个聚合内版本号严格递增；event_id 全局唯一，保证重复提交幂等。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Optional

from .envelope import validate_event
from .errors import StoreError


_ROOT = Path(__file__).resolve().parents[1]
_ALLOWED_EVENTS = frozenset(
    json.loads((_ROOT / "contracts" / "domain.json").read_text(encoding="utf-8"))["events"]
)


class EventStore:
    def __init__(self) -> None:
        self._events: list[dict] = []
        self._event_ids: dict[str, int] = {}
        self._stream_versions: dict[str, int] = {}

    def append(
        self,
        event: dict,
        *,
        expected_version: Optional[int] = None,
        idempotency_key: Optional[str] = None,
    ) -> Optional[dict]:
        """追加事件。

        - expected_version 为聚合当前版本号（首事件为 0）；
        - 若 idempotency_key 此前已成功追加，则直接返回原事件，不再写入。
        """
        if idempotency_key is not None:
            existing = self.find_by_idempotency_key(idempotency_key)
            if existing is not None:
                return existing
        errors = validate_event(event, set(_ALLOWED_EVENTS))
        if errors:
            raise StoreError("; ".join(errors))
        event_id = event["event_id"]
        if event_id in self._event_ids:
            raise StoreError(f"event_id 重复: {event_id}")
        aggregate_id = event["aggregate_id"]
        current = self._stream_versions.get(aggregate_id, 0)
        if expected_version is not None and current != expected_version:
            raise StoreError(
                f"聚合 {aggregate_id} 版本冲突: 期望 {expected_version}, 实际 {current}"
            )
        if event["version"] != current + 1:
            raise StoreError(
                f"聚合 {aggregate_id} 事件版本必须为 {current + 1}, 收到 {event['version']}"
            )
        stored = dict(event)
        if idempotency_key is not None:
            stored["_idempotency_key"] = idempotency_key
        self._events.append(stored)
        self._event_ids[event_id] = len(self._events) - 1
        self._stream_versions[aggregate_id] = event["version"]
        return stored

    def stream(self, aggregate_id: str) -> list[dict]:
        return [e for e in self._events if e["aggregate_id"] == aggregate_id]

    def all_events(self) -> list[dict]:
        return list(self._events)

    def stream_version(self, aggregate_id: str) -> int:
        return self._stream_versions.get(aggregate_id, 0)

    def exists(self, aggregate_id: str) -> bool:
        return aggregate_id in self._stream_versions

    def find(self, event_id: str) -> Optional[dict]:
        idx = self._event_ids.get(event_id)
        return None if idx is None else self._events[idx]

    def find_by_idempotency_key(self, key: str) -> Optional[dict]:
        for event in self._events:
            if event.get("_idempotency_key") == key:
                return event
        return None

    def query(self, event_type: str) -> Iterable[dict]:
        return (e for e in self._events if e["event_type"] == event_type)
