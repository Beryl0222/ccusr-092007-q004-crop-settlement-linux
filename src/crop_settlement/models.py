"""枚举与领域实体。

所有实体都带 ``id`` 与 ``version``：
- ``id`` 由调用方给出（如 ``commitment-001``），跨系统稳定；
- ``version`` 每次状态迁移 +1，用于乐观并发控制；
- 金额一律使用 ``Decimal`` 并以字符串序列化，避免浮点误差。

称重、取样、实验室更正、部分接收、退货、替代去向全部通过 ``batch_id``
与 ``commitment_id`` 沿用原批次，不产生脱离批次的新单据。
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Optional, Union, get_args, get_origin


# ---------------------------------------------------------------- 枚举


class PartyType(str, Enum):
    COOP = "coop"           # 合作社：平台运营与申诉裁决
    WORKSHOP = "workshop"   # 工坊：采购方
    FARMER = "farmer"       # 农户：供货方


class CropType(str, Enum):
    SESAME = "sesame"       # 芝麻
    PEANUT = "peanut"       # 花生
    KERNEL = "kernel"       # 果仁


class CommitmentStatus(str, Enum):
    PROPOSED = "proposed"           # 首版待农户同意
    AGREED = "agreed"               # 当前版本已同意
    SUPERSEDED = "superseded"       # 被新版本取代（历史版本）
    TERMINATED = "terminated"       # 终止（如协商后解除）


class DeliveryStatus(str, Enum):
    REGISTERED = "registered"               # 已称重，尚无任何处置
    PARTIALLY_ACCEPTED = "partially_accepted"
    ACCEPTED = "accepted"                   # 全部按等级接收
    PARTIALLY_RETURNED = "partially_returned"
    SETTLED_CLOSED = "closed"               # 全部处置完毕（含替代去向）


class GradingStatus(str, Enum):
    GRADED = "graded"               # 站点初检
    CORRECTED = "corrected"         # 被实验室更正取代


class DispositionKind(str, Enum):
    ACCEPT = "accept"               # 按等级接收
    RETURN = "return"              # 退货（不计入收购数量）
    ALTERNATE = "alternate"         # 农户同意的替代去向


class FMEventType(str, Enum):
    FLOOD = "flood"                 # 洪涝/连续降雨
    DROUGHT = "drought"             # 持续干旱


class NegotiationStatus(str, Enum):
    OPEN = "open"                           # 已发起，等待证据/协商
    EVIDENCE_REVIEWED = "evidence_reviewed"  # 证据已齐备并复核
    AGREED = "agreed"                       # 双方达成一致
    REJECTED = "rejected"
    CLOSED = "closed"                       # 未达成一致，按非不可抗处理


class AppealStatus(str, Enum):
    OPEN = "open"
    UPHELD = "upheld"                       # 申诉成立
    PARTIALLY_UPHELD = "partially_upheld"
    REJECTED = "rejected"
    CLOSED = "closed"


class PaymentState(str, Enum):
    PROJECTED = "projected"     # 预计付款日（每次交付即可见）
    SCHEDULED = "scheduled"     # 关账后确认
    PAID = "paid"


# ---------------------------------------------------------------- 简单值对象


@dataclass(frozen=True)
class DateWindow:
    """交付窗口（含端点）。"""

    start: date
    end: date


@dataclass(frozen=True)
class PriceTier:
    """浮动分级中的一档：等级单价（元/kg）。"""

    grade: str
    price_per_kg: Decimal


@dataclass(frozen=True)
class Deduction:
    """验收时的一条扣减：理由代码 + 金额（元，正数表示扣减）。"""

    reason_code: str
    amount: Decimal
    note: str = ""


# ---------------------------------------------------------------- 参与方与地块


@dataclass
class Party:
    id: str
    type: PartyType
    name: str
    token: str = ""                      # 工坊/农户访问令牌；合作社为管理令牌
    version: int = 1


@dataclass
class Station:
    """收货点，必须归属某一工坊。"""

    id: str
    workshop_id: str
    name: str
    location: str = ""
    version: int = 1


@dataclass
class Plot:
    """地块。"""

    id: str
    farmer_id: str
    crop: CropType
    location: str = ""
    area_mu: Decimal = Decimal("0")
    version: int = 1


@dataclass
class CropBatch:
    """作物批次：一块地一个季的一次种植。所有交付单据沿用此标识。"""

    id: str
    plot_id: str
    farmer_id: str
    crop: CropType
    planted_at: Optional[date] = None
    harvest_expected_at: Optional[date] = None
    estimated_yield_kg: Decimal = Decimal("0")
    version: int = 1


# ---------------------------------------------------------------- 承诺（版本化）


@dataclass
class CommitmentTerms:
    """某一版本的完整条款快照。

    价格或规则的任何变化都必须产生新版本，并经农户明确同意；
    历史版本保留，状态置为 ``superseded``。
    """

    floor_price_per_kg: Decimal                  # 保底价
    grade_prices: list[PriceTier]                # 浮动分级价
    prepayment_amount: Decimal                   # 保底预付款总额
    delivery_window: DateWindow                  # 交付窗口
    payment_terms_days: int = 7                  # 验收后多少天付尾款
    alternate_allowed: bool = True               # 是否允许替代去向
    alternate_handling_fee_per_kg: Decimal = Decimal("0")  # 工坊收取的代办费
    notes: str = ""


@dataclass
class CommitmentVersion:
    version_no: int
    status: CommitmentStatus
    terms: CommitmentTerms
    floor_quantity_kg: Decimal                   # 保底收购数量
    created_at: datetime
    created_by: str
    change_summary: str = ""
    agreed_at: Optional[datetime] = None
    agreed_by: Optional[str] = None


@dataclass
class Commitment:
    """保底收购承诺：农户 ↔ 工坊，按作物批次关联，条款版本化。"""

    id: str
    farmer_id: str
    workshop_id: str
    batch_id: str
    crop: CropType
    versions: list[CommitmentVersion] = field(default_factory=list)
    status: CommitmentStatus = CommitmentStatus.PROPOSED
    version: int = 1
    settled: bool = False

    @property
    def current(self) -> CommitmentVersion:
        return self.versions[-1]

    def agreed_terms(self) -> CommitmentTerms:
        """返回最近一个被农户明确同意的版本（结算只允许依据它）。"""

        for v in reversed(self.versions):
            if v.status == CommitmentStatus.AGREED:
                return v.terms
        raise ValueError(f"commitment {self.id} 尚无农户同意的版本")


# ---------------------------------------------------------------- 资金与单据


@dataclass
class Advance:
    """保底预付款。"""

    id: str
    commitment_id: str
    amount: Decimal
    paid_at: datetime
    paid_by: str
    note: str = ""
    version: int = 1


@dataclass
class Delivery:
    """一次称重交付（毛重/皮重/净重）。多站点共用同一批次标识。"""

    id: str
    batch_id: str
    commitment_id: str
    station_id: str
    workshop_id: str
    farmer_id: str
    gross_kg: Decimal
    tare_kg: Decimal
    net_kg: Decimal
    weighed_at: datetime
    weighed_by: str
    ticket_no: str = ""
    status: DeliveryStatus = DeliveryStatus.REGISTERED
    version: int = 1


@dataclass
class Sample:
    """封样。"""

    id: str
    delivery_id: str
    sealed_code: str
    sampled_at: datetime
    sampled_by: str
    version: int = 1


@dataclass
class Grading:
    """检验结果。实验室更正不改单据标识，只把旧记录置为 corrected。"""

    id: str
    sample_id: str
    delivery_id: str
    grade: str
    moisture_pct: Decimal
    impurity_pct: Decimal
    graded_at: datetime
    graded_by: str
    is_lab_result: bool = False
    status: GradingStatus = GradingStatus.GRADED
    superseded_by: Optional[str] = None
    note: str = ""
    version: int = 1


@dataclass
class Disposition:
    """对一次交付的处置明细。

    同一交付可拆成多条（部分接收 + 部分退货 + 部分替代去向），
    所有明细数量之和必须等于净重，由存储层原子校验。
    """

    id: str
    delivery_id: str
    kind: DispositionKind
    quantity_kg: Decimal
    grade: Optional[str] = None
    deductions: list[Deduction] = field(default_factory=list)
    decided_at: Optional[datetime] = None
    decided_by: str = ""
    note: str = ""
    alternate_destination: str = ""          # 仅 kind=alternate
    farmer_consent_at: Optional[datetime] = None
    version: int = 1

    @property
    def deduction_total(self) -> Decimal:
        return sum((d.amount for d in self.deductions), Decimal("0"))


@dataclass
class ForceMajeureEvent:
    """洪涝 / 持续干旱等不可抗因素，必须带证据。"""

    id: str
    type: FMEventType
    title: str
    started_at: date
    ended_at: Optional[date]
    region: str
    evidence_refs: list[str] = field(default_factory=list)
    recorded_by: str = ""
    recorded_at: Optional[datetime] = None
    confirmed: bool = False
    version: int = 1


@dataclass
class Negotiation:
    """有证据的减产协商；分别记录不可抗与未履约部分。"""

    id: str
    commitment_id: str
    fm_event_id: str
    status: NegotiationStatus = NegotiationStatus.OPEN
    opened_at: Optional[datetime] = None
    opened_by: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    # —— 协商结论（resolve 时写入）——
    shortfall_total_kg: Optional[Decimal] = None
    fm_quantity_kg: Optional[Decimal] = None           # 不可抗部分
    nonperformance_quantity_kg: Optional[Decimal] = None  # 未履约部分
    advance_recovery_amount: Decimal = Decimal("0")    # 农户应退还的预付款
    advance_relief_amount: Decimal = Decimal("0")      # 因不可抗减免的预付款
    cost_share_amount: Decimal = Decimal("0")          # 工坊另行分担的损失
    resolution_note: str = ""
    resolved_at: Optional[datetime] = None
    resolved_by: str = ""
    farmer_agreed: bool = False
    coop_witness: str = ""
    version: int = 1


@dataclass
class ConsumptionLine:
    """成品投料行：某成品耗用了某批次（经某交付）的原料数量。"""

    batch_id: str
    delivery_id: Optional[str]
    quantity_kg: Decimal


@dataclass
class Consumption:
    """一个成品批次的投料记录（工坊只登记自己采购的原料）。"""

    id: str
    workshop_id: str
    product_lot: str
    used_at: datetime
    lines: list[ConsumptionLine] = field(default_factory=list)
    version: int = 1


@dataclass
class LedgerAdjustment:
    """申诉裁决产生的总账调整（正数补付农户，负数农户退还）。"""

    id: str
    commitment_id: str
    appeal_id: str
    amount: Decimal
    reason: str
    created_at: datetime
    created_by: str
    version: int = 1


@dataclass
class Appeal:
    """农户对检验、扣减、退货或结算的申诉。"""

    id: str
    commitment_id: str
    raised_by: str                       # 农户 id
    target_type: str                     # disposition / grading / settlement
    target_ref: str
    reason: str
    evidence_refs: list[str] = field(default_factory=list)
    status: AppealStatus = AppealStatus.OPEN
    raised_at: Optional[datetime] = None
    resolution_note: str = ""
    adjustment_id: Optional[str] = None
    resolved_at: Optional[datetime] = None
    resolved_by: str = ""
    version: int = 1


@dataclass
class Settlement:
    """一次关账复算的结果快照（总账可随时按同一规则重新复算）。"""

    commitment_id: str
    computed_at: datetime
    accepted_kg: Decimal
    alternate_kg: Decimal
    returned_kg: Decimal
    graded_payable: Decimal
    deductions_total: Decimal
    floor_topup: Decimal
    advance_paid: Decimal
    advance_recovery: Decimal
    advance_relief: Decimal
    cost_share: Decimal
    appeal_adjustments: Decimal
    final_payment: Decimal               # 正数：工坊尾款；负数：农户应退
    payment_state: PaymentState
    projected_pay_at: Optional[date]
    detail_lines: list[dict[str, Any]] = field(default_factory=list)
    advance_receivable: Decimal = Decimal("0")  # 协商确定农户应退还的预付款（单独挂账）


# ---------------------------------------------------------------- 序列化编解码


_SCALAR_TYPES = {str, int, bool, Decimal, date, datetime, type(None)}


def _decode(field_type: Any, value: Any) -> Any:
    if value is None:
        return None
    origin = get_origin(field_type)
    if origin is Union:
        args = [a for a in get_args(field_type) if a is not type(None)]
        return _decode(args[0], value)
    if field_type is Any:
        return value
    if origin in (list,):
        item_type = get_args(field_type)[0]
        return [_decode(item_type, v) for v in value]
    if field_type is Decimal:
        return Decimal(str(value))
    if field_type is date:
        return date.fromisoformat(value)
    if field_type is datetime:
        return datetime.fromisoformat(value)
    if isinstance(field_type, type) and issubclass(field_type, Enum):
        return field_type(value)
    if isinstance(value, dict) and hasattr(field_type, "__dataclass_fields__"):
        return _decode_dataclass(field_type, value)
    return value


def _decode_dataclass(cls: type, data: dict[str, Any]) -> Any:
    kwargs: dict[str, Any] = {}
    hints = {f.name: f.type for f in fields(cls)}
    for name, raw in data.items():
        if name not in hints:
            continue
        kwargs[name] = _decode(_resolve_hint(cls, name), raw)
    return cls(**kwargs)


def _resolve_hint(cls: type, name: str) -> Any:
    import sys

    pkg = sys.modules[cls.__module__]
    f = next(f for f in fields(cls) if f.name == name)
    t = f.type
    if isinstance(t, str):
        return eval(t, vars(pkg))  # noqa: S307 - 仅解析本包 dataclass 注解
    return t


def from_dict(cls: type, data: dict[str, Any]) -> Any:
    return _decode_dataclass(cls, data)


def to_dict(obj: Any) -> Any:
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if hasattr(obj, "__dataclass_fields__"):
        return {f.name: to_dict(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, list):
        return [to_dict(v) for v in obj]
    if isinstance(obj, dict):
        return {k: to_dict(v) for k, v in obj.items()}
    return obj
