"""只追加的事件存储与乐观并发控制。

存储不解释业务状态，只保证三件事：事件信封合法、aggregate 版本严格递增、
event_id 全局唯一。业务事实一旦写入即不可改写，更正只能以新事件表达。
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from .envelope import validate_event


class EventStore:
    def __init__(self, contract_path: str | Path | None = None) -> None:
        if contract_path is None:
            contract_path = Path(__file__).resolve().parents[1] / "contracts" / "domain.json"
        contract = json.loads(Path(contract_path).read_text(encoding="utf-8"))
        self._allowed_events = set(contract["events"])
        self._events: list[dict[str, Any]] = []
        self._by_aggregate: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._versions: dict[str, int] = {}
        self._event_ids: set[str] = set()

    def append(self, event: dict[str, Any], expected_version: int | None) -> dict[str, Any]:
        """追加一个事件；expected_version 为该 aggregate 追加前的版本，新建传 0。"""
        errors = validate_event(event, self._allowed_events)
        if errors:
            raise ValueError("; ".join(errors))
        aggregate_id = event["aggregate_id"]
        current = self._versions.get(aggregate_id, 0)
        if expected_version != current:
            raise ValueError(
                f"aggregate {aggregate_id} 版本冲突: 期望 {expected_version}, 实际 {current}"
            )
        if event["event_id"] in self._event_ids:
            raise ValueError(f"event_id 重复: {event['event_id']}")
        if event["version"] != current + 1:
            raise ValueError(
                f"事件版本必须严格递增: {event['version']} != {current + 1}"
            )
        self._event_ids.add(event["event_id"])
        self._events.append(event)
        self._by_aggregate[aggregate_id].append(event)
        self._versions[aggregate_id] = event["version"]
        return event

    def events(self, aggregate_id: str) -> list[dict[str, Any]]:
        return list(self._by_aggregate.get(aggregate_id, ()))

    def all_events(self) -> list[dict[str, Any]]:
        return list(self._events)

    def version_of(self, aggregate_id: str) -> int:
        return self._versions.get(aggregate_id, 0)

    def seen_event_ids(self) -> set[str]:
        return set(self._event_ids)
