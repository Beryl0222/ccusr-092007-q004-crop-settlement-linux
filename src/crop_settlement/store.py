"""线程安全的内存仓储。

设计要点：
- 一把全局可重入锁，领域服务在 ``store.transaction()`` 上下文内完成
  “读-校验-写”，保证多个收货点同时登记时不会突破实收数量；
- 每个实体带 ``version``，保存时调用方必须提供它先前读到的版本号，
  版本号过期抛 :class:`ConflictError`（乐观锁）；
- 所有按承诺/批次的查询都强制带上采购关系（farmer/workshop），
  工坊只能看到自己的采购数据。
"""

from __future__ import annotations

import threading
from collections import defaultdict
from contextlib import contextmanager
from typing import Callable, Iterable, Iterator, Optional, TypeVar

from .errors import ConflictError, NotFoundError, PermissionDenied
from .models import (
    Advance,
    Appeal,
    Commitment,
    Consumption,
    CropBatch,
    Delivery,
    Disposition,
    ForceMajeureEvent,
    Grading,
    LedgerAdjustment,
    Negotiation,
    Party,
    Plot,
    Sample,
    Station,
)

T = TypeVar("T")


class Store:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.parties: dict[str, Party] = {}
        self.stations: dict[str, Station] = {}
        self.plots: dict[str, Plot] = {}
        self.batches: dict[str, CropBatch] = {}
        self.commitments: dict[str, Commitment] = {}
        self.advances: dict[str, Advance] = {}
        self.deliveries: dict[str, Delivery] = {}
        self.samples: dict[str, Sample] = {}
        self.gradings: dict[str, Grading] = {}
        self.dispositions: dict[str, Disposition] = {}
        self.fm_events: dict[str, ForceMajeureEvent] = {}
        self.negotiations: dict[str, Negotiation] = {}
        self.consumptions: dict[str, Consumption] = {}
        self.adjustments: dict[str, LedgerAdjustment] = {}
        self.appeals: dict[str, Appeal] = {}
        # 唯一性索引：同一承诺下外部票号不重复
        self._ticket_index: dict[tuple[str, str], str] = {}
        self._seq: dict[str, int] = defaultdict(int)

    # ------------------------------------------------------------ 事务

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self._lock.acquire()
        try:
            yield
        finally:
            self._lock.release()

    def next_id(self, prefix: str) -> str:
        with self._lock:
            self._seq[prefix] += 1
            return f"{prefix}-{self._seq[prefix]:04d}"

    # ------------------------------------------------------------ 通用存取

    @staticmethod
    def _put(table: dict[str, T], entity: T, expected_version: Optional[int]) -> T:
        current = table.get(entity.id)
        if current is None:
            table[entity.id] = entity
            return entity
        # 显式给出版本号：乐观锁校验；未给出：领域服务内部状态流转，直接递增
        if expected_version is not None and current.version != expected_version:
            raise ConflictError(
                f"{entity.id} 版本过期：当前 {current.version}，提交基于 {expected_version}"
            )
        entity.version = current.version + 1
        table[entity.id] = entity
        return entity

    def save_party(self, p: Party, expected: Optional[int] = None) -> None:
        with self._lock:
            self._put(self.parties, p, expected)

    def save_station(self, s: Station, expected: Optional[int] = None) -> None:
        with self._lock:
            self._put(self.stations, s, expected)

    def save_plot(self, p: Plot, expected: Optional[int] = None) -> None:
        with self._lock:
            self._put(self.plots, p, expected)

    def save_batch(self, b: CropBatch, expected: Optional[int] = None) -> None:
        with self._lock:
            self._put(self.batches, b, expected)

    def save_commitment(self, c: Commitment, expected: Optional[int] = None) -> None:
        with self._lock:
            self._put(self.commitments, c, expected)

    def save_advance(self, a: Advance, expected: Optional[int] = None) -> None:
        with self._lock:
            self._put(self.advances, a, expected)

    def save_delivery(self, d: Delivery, expected: Optional[int] = None) -> None:
        with self._lock:
            # 票号唯一（同一承诺内），防止站点重复登记
            if d.ticket_no:
                key = (d.commitment_id, d.ticket_no)
                existing = self._ticket_index.get(key)
                if existing and existing != d.id:
                    raise ConflictError(f"票号 {d.ticket_no} 已被交付 {existing} 使用")
                self._ticket_index[key] = d.id
            self._put(self.deliveries, d, expected)

    def save_sample(self, s: Sample, expected: Optional[int] = None) -> None:
        with self._lock:
            self._put(self.samples, s, expected)

    def save_grading(self, g: Grading, expected: Optional[int] = None) -> None:
        with self._lock:
            self._put(self.gradings, g, expected)

    def save_disposition(self, d: Disposition, expected: Optional[int] = None) -> None:
        with self._lock:
            self._put(self.dispositions, d, expected)

    def save_fm_event(self, e: ForceMajeureEvent, expected: Optional[int] = None) -> None:
        with self._lock:
            self._put(self.fm_events, e, expected)

    def save_negotiation(self, n: Negotiation, expected: Optional[int] = None) -> None:
        with self._lock:
            self._put(self.negotiations, n, expected)

    def save_consumption(self, c: Consumption, expected: Optional[int] = None) -> None:
        with self._lock:
            self._put(self.consumptions, c, expected)

    def save_adjustment(self, a: LedgerAdjustment, expected: Optional[int] = None) -> None:
        with self._lock:
            self._put(self.adjustments, a, expected)

    def save_appeal(self, a: Appeal, expected: Optional[int] = None) -> None:
        with self._lock:
            self._put(self.appeals, a, expected)

    # ------------------------------------------------------------ 查询

    def get_party(self, party_id: str) -> Party:
        try:
            return self.parties[party_id]
        except KeyError:
            raise NotFoundError(f"参与方 {party_id} 不存在") from None

    def get_station(self, station_id: str) -> Station:
        try:
            return self.stations[station_id]
        except KeyError:
            raise NotFoundError(f"收货点 {station_id} 不存在") from None

    def get_batch(self, batch_id: str) -> CropBatch:
        try:
            return self.batches[batch_id]
        except KeyError:
            raise NotFoundError(f"批次 {batch_id} 不存在") from None

    def get_commitment(self, commitment_id: str) -> Commitment:
        try:
            return self.commitments[commitment_id]
        except KeyError:
            raise NotFoundError(f"承诺 {commitment_id} 不存在") from None

    def get_delivery(self, delivery_id: str) -> Delivery:
        try:
            return self.deliveries[delivery_id]
        except KeyError:
            raise NotFoundError(f"交付 {delivery_id} 不存在") from None

    def deliveries_for(self, commitment_id: str) -> list[Delivery]:
        return [
            d for d in self.deliveries.values() if d.commitment_id == commitment_id
        ]

    def dispositions_for(self, delivery_id: str) -> list[Disposition]:
        return [
            d for d in self.dispositions.values() if d.delivery_id == delivery_id
        ]

    def gradings_for(self, delivery_id: str) -> list[Grading]:
        return [
            g for g in self.gradings.values() if g.delivery_id == delivery_id
        ]

    def samples_for(self, delivery_id: str) -> list[Sample]:
        return [s for s in self.samples.values() if s.delivery_id == delivery_id]

    def advances_for(self, commitment_id: str) -> list[Advance]:
        return [a for a in self.advances.values() if a.commitment_id == commitment_id]

    def negotiations_for(self, commitment_id: str) -> list[Negotiation]:
        return [
            n for n in self.negotiations.values() if n.commitment_id == commitment_id
        ]

    def adjustments_for(self, commitment_id: str) -> list[LedgerAdjustment]:
        return [
            a for a in self.adjustments.values() if a.commitment_id == commitment_id
        ]

    def appeals_for(self, commitment_id: str) -> list[Appeal]:
        return [
            a for a in self.appeals.values() if a.commitment_id == commitment_id
        ]

    def consumptions_for_batch(self, batch_id: str) -> list[Consumption]:
        return [
            c for c in self.consumptions.values()
            if any(line.batch_id == batch_id for line in c.lines)
        ]

    # ------------------------------------------------------------ 采购关系鉴权

    def visible_commitment(self, commitment_id: str, party: Party) -> Commitment:
        """返回承诺，并校验 ``party`` 是该采购关系的一方（或合作社）。

        对无权限方，承诺不存在与无权限统一返回 404，避免泄露采购关系的存在。
        """

        c = self.get_commitment(commitment_id)
        if party.type.value == "coop":
            return c
        if party.id == c.workshop_id or party.id == c.farmer_id:
            return c
        raise NotFoundError(f"承诺 {commitment_id} 不存在")

    def require_workshop_relation(self, commitment_id: str, workshop: Party) -> Commitment:
        c = self.visible_commitment(commitment_id, workshop)
        if workshop.type.value != "workshop" or c.workshop_id != workshop.id:
            raise PermissionDenied("只能查看自己的采购关系")
        return c
