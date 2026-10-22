"""Document store for LLM generations.

TinyDB (a JSON file) instead of MongoDB: one process, no server to run for a
reviewer, and generations are naturally documents (nested test cases, raw
attempts, source snapshots). Everything goes through ``GenerationStore`` so a
Mongo-backed class with the same four methods can replace it.
"""
from __future__ import annotations

import threading
from typing import Protocol

from tinydb import Query, TinyDB
from tinydb.storages import MemoryStorage


class GenerationStore(Protocol):
    def insert(self, doc: dict) -> None: ...
    def get(self, generation_id: str) -> dict | None: ...
    def find(self, *, selection_id: str | None = None, logical_id: int | None = None,
             cache_key: str | None = None, status: str | None = None) -> list[dict]: ...


class TinyDBStore:
    def __init__(self, path: str | None):
        self._db = TinyDB(storage=MemoryStorage) if path in (None, ":memory:") else \
            TinyDB(path, indent=1, ensure_ascii=False)
        self._t = self._db.table("generations")
        self._lock = threading.Lock()  # TinyDB is not thread-safe; FastAPI runs sync routes in a pool

    def insert(self, doc: dict) -> None:
        with self._lock:
            self._t.insert(doc)

    def get(self, generation_id: str) -> dict | None:
        with self._lock:
            return self._t.get(Query().id == generation_id)

    def find(self, *, selection_id=None, logical_id=None, cache_key=None, status=None) -> list[dict]:
        q = Query()
        cond = q.id.exists()
        if selection_id is not None:
            cond &= q.selection_id == selection_id
        if cache_key is not None:
            cond &= q.cache_key == cache_key
        if status is not None:
            cond &= q.status == status
        if logical_id is not None:
            cond &= q.sources.any(Query().logical_id == logical_id)
        with self._lock:
            rows = self._t.search(cond)
        return sorted(rows, key=lambda d: d["created_at"])
