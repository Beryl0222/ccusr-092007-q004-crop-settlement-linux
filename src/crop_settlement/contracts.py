"""读取项目已确认的数据合同。

revision 1：仅含信封字段（``schema_version``/``record_id``/``domain``/
``occurred_at``/``revision``/``source``）。
revision 2：在保留同一信封语义的前提下，追加参与方、地块、批次、
版本化承诺、预付款、交付、检验、处置、不可抗协商、成品投料等业务数据。

旧 revision 1 的文件仍可通过 :func:`load_record` 读取；
:func:`load_dataset` 对缺少业务段的文件返回空仓储，不做破坏性迁移。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    from_dict,
)
from .store import Store

SUPPORTED_SCHEMA_VERSION = 1
CURRENT_REVISION = 2


@dataclass(frozen=True)
class DomainRecord:
    schema_version: int
    record_id: str
    domain: str
    occurred_at: str
    revision: int
    source: str


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_record(path: Path) -> DomainRecord:
    """读取信封字段；revision 1 与 revision 2 的文件均兼容。"""

    payload = _read(path)
    envelope = {k: payload[k] for k in DomainRecord.__dataclass_fields__ if k in payload}
    return DomainRecord(**envelope)


def _load_many(store: Store, payload: dict[str, Any], key: str, cls: type, saver: str) -> int:
    count = 0
    for raw in payload.get(key, []):
        entity = from_dict(cls, raw)
        getattr(store, saver)(entity)
        count += 1
    return count


def load_dataset(path: Path) -> tuple[DomainRecord, Store]:
    """加载完整样例到一个空仓储并返回 ``(信封记录, 仓储)``。

    对 revision 1 文件：返回空仓储（无业务段可加载），调用方据此
    判断需要走新建流程而不是读取历史数据。
    """

    payload = _read(path)
    if payload.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
        raise ValueError(
            f"不支持的 schema_version={payload.get('schema_version')}，"
            f"当前支持 {SUPPORTED_SCHEMA_VERSION}"
        )
    record = load_record(path)
    store = Store()
    if record.revision < 2:
        return record, store

    with store.transaction():
        _load_many(store, payload, "parties", Party, "save_party")
        _load_many(store, payload, "stations", Station, "save_station")
        _load_many(store, payload, "plots", Plot, "save_plot")
        _load_many(store, payload, "batches", CropBatch, "save_batch")
        _load_many(store, payload, "commitments", Commitment, "save_commitment")
        _load_many(store, payload, "advances", Advance, "save_advance")
        _load_many(store, payload, "deliveries", Delivery, "save_delivery")
        _load_many(store, payload, "samples", Sample, "save_sample")
        _load_many(store, payload, "gradings", Grading, "save_grading")
        _load_many(store, payload, "dispositions", Disposition, "save_disposition")
        _load_many(store, payload, "fm_events", ForceMajeureEvent, "save_fm_event")
        _load_many(store, payload, "negotiations", Negotiation, "save_negotiation")
        _load_many(store, payload, "consumptions", Consumption, "save_consumption")
        _load_many(store, payload, "adjustments", LedgerAdjustment, "save_adjustment")
        _load_many(store, payload, "appeals", Appeal, "save_appeal")
    return record, store
