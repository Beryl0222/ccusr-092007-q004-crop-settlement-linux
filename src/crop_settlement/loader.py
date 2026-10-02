"""把 supply_commitment.json 样例解析为领域对象并导入服务。

schema_version 1 -> 2 的迁移是纯新增：信封字段不变，新增 parcels /
crop_batches / commitments 三段业务负载；旧读者（contracts.load_record）
只读信封，不受影响。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .contracts import DomainRecord
from .domain import (
    COOP,
    Commitment,
    CommitmentTerms,
    CommitmentVersion,
    ConsentRecord,
    CropBatch,
    CropType,
    DeliveryWindow,
    Grade,
    GradeBand,
    GradingRule,
    Parcel,
    VersionStatus,
    kg,
    yuan,
)
from .service import SettlementService


@dataclass(frozen=True)
class SupplyRecord:
    """样例文件的完整解析结果：信封加业务负载。"""

    envelope: DomainRecord
    parcels: tuple[Parcel, ...]
    crop_batches: tuple[CropBatch, ...]
    commitments: tuple[Commitment, ...]
    prepayments: dict[str, datetime]  # commitment_id -> 预付款付款时间


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _parse_terms(payload: dict, prepayment_total) -> CommitmentTerms:
    rule = payload["grading_rule"]
    window = payload["delivery_window"]
    return CommitmentTerms(
        committed_quantity_kg=kg(payload["committed_quantity_kg"]),
        floor_price_per_kg=yuan(payload["floor_price_per_kg"]),
        grading=GradingRule(
            bands=tuple(
                GradeBand(
                    grade=Grade(band["grade"]),
                    max_moisture_pct=kg(band["max_moisture_pct"]),
                    max_impurity_pct=kg(band["max_impurity_pct"]),
                    price_delta_per_kg=yuan(band["price_delta_per_kg"]),
                )
                for band in rule["bands"]
            ),
            moisture_threshold_pct=kg(rule["moisture_threshold_pct"]),
            moisture_deduction_pct_per_point=kg(rule["moisture_deduction_pct_per_point"]),
            impurity_threshold_pct=kg(rule["impurity_threshold_pct"]),
            impurity_deduction_pct_per_point=kg(rule["impurity_deduction_pct_per_point"]),
        ),
        window=DeliveryWindow(
            opens_on=_parse_date(window["opens_on"]),
            closes_on=_parse_date(window["closes_on"]),
        ),
        payment_terms_days=int(payload["payment_terms_days"]),
        allowed_destinations=tuple(payload.get("allowed_destinations", ())),
        prepayment_total=yuan(prepayment_total),
    )


def load_supply_record(path: Path) -> SupplyRecord:
    payload = json.loads(path.read_text(encoding="utf-8"))
    envelope = DomainRecord(
        **{
            key: payload[key]
            for key in ("schema_version", "record_id", "domain", "occurred_at", "revision", "source")
        }
    )
    parcels = tuple(
        Parcel(
            parcel_id=item["parcel_id"],
            farmer_id=item["farmer_id"],
            village=item["village"],
            area_mu=kg(item["area_mu"]),
        )
        for item in payload.get("parcels", [])
    )
    batches = tuple(
        CropBatch(
            batch_id=item["batch_id"],
            parcel_id=item["parcel_id"],
            crop=CropType(item["crop"]),
        )
        for item in payload.get("crop_batches", [])
    )
    commitments: list[Commitment] = []
    prepayments: dict[str, datetime] = {}
    for item in payload.get("commitments", []):
        prepayment = item.get("prepayment") or {}
        prepayment_total = yuan(prepayment.get("amount", "0"))
        consent_payload = item["consent"]
        consent = ConsentRecord(
            farmer_id=consent_payload["farmer_id"],
            consented=bool(consent_payload["consented"]),
            at=_parse_dt(consent_payload["at"]),
            channel=consent_payload["channel"],
        )
        version = CommitmentVersion(
            version=1,
            terms=_parse_terms(item["terms"], prepayment_total),
            status=VersionStatus.ACTIVE,
            created_at=_parse_dt(item["created_at"]),
            consent=consent,
        )
        commitments.append(
            Commitment(
                commitment_id=item["commitment_id"],
                farmer_id=item["farmer_id"],
                workshop_id=item["workshop_id"],
                batch_id=item["batch_id"],
                versions=[version],
            )
        )
        if prepayment_total > 0:
            prepayments[item["commitment_id"]] = _parse_dt(prepayment["paid_at"])
    return SupplyRecord(
        envelope=envelope,
        parcels=parcels,
        crop_batches=batches,
        commitments=tuple(commitments),
        prepayments=prepayments,
    )


def import_supply_record(service: SettlementService, record: SupplyRecord) -> None:
    """把样例导入服务：档案、承诺（含农户同意）与预付款。"""
    for parcel in record.parcels:
        service.register_parcel(COOP, parcel)
    for batch in record.crop_batches:
        service.register_batch(COOP, batch)
    for commitment in record.commitments:
        service.register_commitment(
            COOP,
            commitment,
            prepayment_paid_at=record.prepayments.get(commitment.commitment_id),
        )
