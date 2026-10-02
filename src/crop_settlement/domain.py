"""保底收购结算后端的领域实体、值对象与状态机。

约定：
- 金额一律用 Decimal 表示，单位为元，保留两位小数（见 `yuan`）。
- 数量一律用 Decimal 表示，单位为公斤，保留三位小数（见 `kg`）。
- 所有业务时间都是带时区的 datetime；`occurred_at` 等既有字段的含义不变。

状态机（新增状态必须同步 README「状态与迁移」一节）：
- VersionStatus:     DRAFT -> ACTIVE -> SUPERSEDED；DRAFT -> REJECTED
- NegotiationStatus: PROPOSED -> EVIDENCED -> AGREED | REJECTED
- AppealStatus:      FILED -> UNDER_REVIEW -> UPHELD | ADJUSTED
以上状态均随 schema_version 2 引入；schema_version 1 的样例不含这些状态，
无需数据迁移。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum

CENT = Decimal("0.01")
MILLI_KG = Decimal("0.001")


def yuan(value: str | int | Decimal) -> Decimal:
    """把输入规整为两位小数的金额（元）。"""
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def kg(value: str | int | Decimal) -> Decimal:
    """把输入规整为三位小数的数量（公斤）。"""
    return Decimal(str(value)).quantize(MILLI_KG, rounding=ROUND_HALF_UP)


class CropType(str, Enum):
    SESAME = "sesame"   # 芝麻
    PEANUT = "peanut"   # 花生
    NUT = "nut"         # 果仁


class Role(str, Enum):
    COOPERATIVE = "cooperative"  # 合作社，可见全部
    WORKSHOP = "workshop"        # 工坊，仅可见自己的采购关系
    FARMER = "farmer"            # 农户，仅可见自己的交付与账目


class Grade(str, Enum):
    G1 = "G1"
    G2 = "G2"
    G3 = "G3"
    OFF = "OFF"  # 等外


class DeductionReason(str, Enum):
    MOISTURE_OVER = "moisture_over"    # 水分超标扣重
    IMPURITY_OVER = "impurity_over"    # 杂质超标扣重
    RETURN_HANDLING = "return_handling"  # 退货处置
    OTHER = "other"


class PaymentKind(str, Enum):
    PREPAYMENT = "prepayment"  # 保底预付款
    FINAL = "final"            # 尾款（按接收结算）
    RETURN = "return"          # 退货冲减
    ADJUSTMENT = "adjustment"  # 补差（实验室更正、申诉裁定等）


class VersionStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


class NegotiationCause(str, Enum):
    FLOOD = "flood"      # 洪涝
    DROUGHT = "drought"  # 持续干旱
    OTHER = "other"


class NegotiationStatus(str, Enum):
    PROPOSED = "proposed"
    EVIDENCED = "evidenced"
    AGREED = "agreed"
    REJECTED = "rejected"


class AppealStatus(str, Enum):
    FILED = "filed"
    UNDER_REVIEW = "under_review"
    UPHELD = "upheld"      # 维持原结算
    ADJUSTED = "adjusted"  # 裁定补差


@dataclass(frozen=True)
class Principal:
    """调用方身份。所有服务方法都以它做访问控制。"""

    role: Role
    subject_id: str


COOP = Principal(Role.COOPERATIVE, "coop")


@dataclass(frozen=True)
class Parcel:
    parcel_id: str
    farmer_id: str
    village: str
    area_mu: Decimal


@dataclass(frozen=True)
class CropBatch:
    """作物批次。称重、取样、实验室更正、部分接收、退货都挂在原批次上。"""

    batch_id: str
    parcel_id: str
    crop: CropType


@dataclass(frozen=True)
class GradeBand:
    grade: Grade
    max_moisture_pct: Decimal
    max_impurity_pct: Decimal
    price_delta_per_kg: Decimal  # 相对参考价的浮动（可正可负）


@dataclass(frozen=True)
class GradingRule:
    """浮动分级规则：按实验室指标定级，超标按比例扣重。"""

    bands: tuple[GradeBand, ...]
    moisture_threshold_pct: Decimal
    moisture_deduction_pct_per_point: Decimal
    impurity_threshold_pct: Decimal
    impurity_deduction_pct_per_point: Decimal


@dataclass(frozen=True)
class DeliveryWindow:
    opens_on: date
    closes_on: date

    def contains(self, day: date) -> bool:
        return self.opens_on <= day <= self.closes_on


@dataclass(frozen=True)
class CommitmentTerms:
    """一个承诺版本下的完整条款。条款不可原地修改，变更只能走新版本。"""

    committed_quantity_kg: Decimal
    floor_price_per_kg: Decimal
    grading: GradingRule
    window: DeliveryWindow
    payment_terms_days: int
    allowed_destinations: tuple[str, ...]  # 允许的替代去向，空表示不限制
    prepayment_total: Decimal


@dataclass(frozen=True)
class ConsentRecord:
    farmer_id: str
    consented: bool
    at: datetime
    channel: str  # 例如「纸质合同」「小程序确认」


@dataclass
class CommitmentVersion:
    version: int
    terms: CommitmentTerms
    status: VersionStatus
    created_at: datetime
    consent: ConsentRecord | None = None


@dataclass
class Commitment:
    """保底收购承诺：把农户、工坊和作物批次绑定到一组可版本化的条款。"""

    commitment_id: str
    farmer_id: str
    workshop_id: str
    batch_id: str
    versions: list[CommitmentVersion] = field(default_factory=list)

    def active_version(self) -> CommitmentVersion:
        for item in self.versions:
            if item.status is VersionStatus.ACTIVE:
                return item
        raise LookupError(f"承诺 {self.commitment_id} 没有生效版本")

    def version(self, number: int) -> CommitmentVersion:
        for item in self.versions:
            if item.version == number:
                return item
        raise LookupError(f"承诺 {self.commitment_id} 没有版本 {number}")


@dataclass(frozen=True)
class QualityMetrics:
    moisture_pct: Decimal
    impurity_pct: Decimal


@dataclass(frozen=True)
class Weighing:
    weighing_id: str
    batch_id: str
    site_id: str
    gross_kg: Decimal
    tare_kg: Decimal
    net_kg: Decimal
    at: datetime


@dataclass(frozen=True)
class SampleRecord:
    sample_id: str
    batch_id: str
    site_id: str
    taken_by: str
    at: datetime


@dataclass(frozen=True)
class LabReport:
    """实验室报告。更正报告通过 corrects 指向前一份报告，批次不变。"""

    report_id: str
    batch_id: str
    sample_id: str
    metrics: QualityMetrics
    issued_at: datetime
    corrects: str | None = None


@dataclass(frozen=True)
class Acceptance:
    """一次（部分）接收。数量计入批次实收，结算沿用接收时的承诺版本。"""

    acceptance_id: str
    batch_id: str
    commitment_id: str
    site_id: str
    quantity_kg: Decimal
    version: int
    market_reference_per_kg: Decimal | None
    at: datetime


@dataclass(frozen=True)
class ReturnRecord:
    return_id: str
    acceptance_id: str
    batch_id: str
    site_id: str
    quantity_kg: Decimal
    reason: str
    at: datetime


@dataclass(frozen=True)
class Deduction:
    reason: DeductionReason
    label: str  # 给农户看的扣除理由
    weight_kg: Decimal | None = None
    amount: Decimal | None = None


@dataclass(frozen=True)
class DeliveryStatement:
    """每次交付后给农户看的结算单：数量、等级、扣除理由、预计付款日。"""

    statement_id: str
    acceptance_id: str
    commitment_id: str
    batch_id: str
    farmer_id: str
    workshop_id: str
    quantity_kg: Decimal
    grade: Grade
    unit_price: Decimal
    payable_quantity_kg: Decimal
    deductions: tuple[Deduction, ...]
    gross_amount: Decimal
    net_amount: Decimal
    expected_payment_date: date
    terms_version: int
    revision: int
    issued_at: datetime
    supersedes: str | None = None


@dataclass(frozen=True)
class LedgerEvent:
    """台账事件。金额符号约定：正数表示合作社欠农户增加，负数表示减少。"""

    event_id: str
    commitment_id: str
    kind: PaymentKind
    amount: Decimal
    reference_id: str
    note: str
    at: datetime


@dataclass(frozen=True)
class LedgerView:
    """总账复算结果：预付款、尾款、退货、补差四个组成部分加余额。"""

    commitment_id: str
    prepayments: Decimal
    final_payables: Decimal
    returns: Decimal
    adjustments: Decimal
    balance_due: Decimal
    events: tuple[LedgerEvent, ...]


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    kind: str  # 例如「气象记录」「田间照片」「测产报告」
    reference: str
    submitted_by: str
    at: datetime


@dataclass(frozen=True)
class NegotiationResolution:
    """协商结论：不可抗因素与未履约部分分别记录。"""

    force_majeure_kg: Decimal
    non_performance_kg: Decimal
    decided_by: str
    at: datetime


@dataclass
class NegotiationCase:
    case_id: str
    commitment_id: str
    cause: NegotiationCause
    claimed_shortfall_kg: Decimal
    opened_by: str
    opened_at: datetime
    status: NegotiationStatus = NegotiationStatus.PROPOSED
    evidence: list[Evidence] = field(default_factory=list)
    resolution: NegotiationResolution | None = None


@dataclass
class Appeal:
    appeal_id: str
    statement_id: str
    commitment_id: str
    farmer_id: str
    reason: str
    filed_at: datetime
    status: AppealStatus = AppealStatus.FILED
    resolution_note: str | None = None
    adjustment_amount: Decimal | None = None
    decided_at: datetime | None = None


@dataclass(frozen=True)
class DestinationRecord:
    """替代去向记录：未进入工坊的批次数量流向哪里。"""

    record_id: str
    batch_id: str
    commitment_id: str
    kind: str
    quantity_kg: Decimal
    reason: str
    at: datetime


@dataclass(frozen=True)
class IngredientUsage:
    batch_id: str
    quantity_kg: Decimal


@dataclass(frozen=True)
class ProductBatch:
    """成品批次及其投料构成，用于从成品反查原料批次。"""

    product_id: str
    workshop_id: str
    produced_at: datetime
    ingredients: tuple[IngredientUsage, ...]


@dataclass(frozen=True)
class IngredientTrace:
    batch_id: str
    parcel_id: str
    farmer_id: str
    commitment_id: str
    used_quantity_kg: Decimal
    accepted_quantity_kg: Decimal
    settlement_status: str  # no_delivery / open / settled
    balance_due: Decimal


@dataclass(frozen=True)
class ProductTrace:
    product_id: str
    workshop_id: str
    ingredients: tuple[IngredientTrace, ...]


@dataclass(frozen=True)
class ObligationReport:
    """承诺履约情况：实收、不可抗减免与未履约分别列示。"""

    commitment_id: str
    committed_quantity_kg: Decimal
    accepted_quantity_kg: Decimal
    returned_quantity_kg: Decimal
    force_majeure_excused_kg: Decimal
    non_performance_kg: Decimal
    outstanding_quantity_kg: Decimal
