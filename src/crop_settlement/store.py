"""线程安全的内存仓储。

所有表共享一把可重入锁：服务层在 `locked()` 临界区内完成
「检查余量 -> 登记」这样的复合操作，保证多个收货点同时登记时
也不会突破批次实收数量。写入按记录标识幂等：同一标识重复提交
返回既有记录，不重复计数，站点重试是安全的。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any


class Store:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tables: dict[str, dict[str, Any]] = {}

    @contextmanager
    def locked(self) -> Iterator[None]:
        with self._lock:
            yield

    def put(self, table: str, key: str, record: Any) -> tuple[Any, bool]:
        """写入记录；键已存在时返回既有记录且 inserted=False。"""
        with self._lock:
            bucket = self._tables.setdefault(table, {})
            if key in bucket:
                return bucket[key], False
            bucket[key] = record
            return record, True

    def get(self, table: str, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._tables.get(table, {}).get(key, default)

    def all(self, table: str) -> list[Any]:
        with self._lock:
            return list(self._tables.get(table, {}).values())

    def filter(self, table: str, predicate: Callable[[Any], bool]) -> list[Any]:
        with self._lock:
            return [item for item in self._tables.get(table, {}).values() if predicate(item)]
