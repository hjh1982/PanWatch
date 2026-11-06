from __future__ import annotations

import json
import time
from copy import deepcopy
from typing import Any

from sqlalchemy.dialects.sqlite import insert

from src.platform.persistence.database import SessionLocal
from src.platform.persistence.models import StatisticSnapshot


class StatisticsStore:
    def __init__(self, namespace: str, default_ttl_sec: float):
        self.namespace = namespace
        self.default_ttl_sec = default_ttl_sec

    def get(self, key: str, *, allow_stale: bool = False) -> Any | None:
        with SessionLocal() as db:
            row = db.get(StatisticSnapshot, (self.namespace, key))
            if row is None or (not allow_stale and row.expires_at <= time.time()):
                return None
            return row.payload

    def set(self, key: str, value: Any, ttl_sec: float | None = None) -> None:
        json.dumps(value, allow_nan=False)
        now = time.time()
        ttl = self.default_ttl_sec if ttl_sec is None else ttl_sec
        statement = insert(StatisticSnapshot).values(
            namespace=self.namespace,
            key=key,
            payload=value,
            fetched_at=now,
            expires_at=now + ttl,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[StatisticSnapshot.namespace, StatisticSnapshot.key],
            set_={
                "payload": statement.excluded.payload,
                "fetched_at": statement.excluded.fetched_at,
                "expires_at": statement.excluded.expires_at,
            },
        )
        with SessionLocal() as db:
            db.execute(statement)
            db.commit()


def merge_mappings(previous: dict, incoming: dict) -> dict:
    merged = deepcopy(previous)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_mappings(merged[key], value)
        elif value is not None:
            merged[key] = deepcopy(value)
    return merged
