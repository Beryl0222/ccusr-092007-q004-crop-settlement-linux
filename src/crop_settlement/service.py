"""保底收购结算服务：收货、结算、协商、申诉、总账与追溯的统一入口。

不变量：
- 称重、取样、实验室更正、部分接收、退货全部沿用原批次标识。
- 多个收货点并发登记时，同一批次的累计接收不超过实收数量
  （检查与入账在同一个锁临界区内完成，见 `Store.locked`）。
- 同一承诺的累计净接收不超过承诺总量。
- 价格或规则变更只进入农户明确同意的新版本；生效版本不可原地修改。
- 台账只由事件组成，任何时候都可以复算（预付款、尾款、退货、补差）。
- 工坊只能查看自己的采购关系，农户只能查看自己的账目。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from . import grading
from .domain import (
    Acceptance,
    Appeal,
    AppealStatus,
    Commitment,
    CommitmentTerms,
    CommitmentVersion,
    ConsentRecord,
    CropBatch,
    DeliveryStatement,
    DestinationRecord,
    Evidence,
    Grade,
    IngredientTrace,
    LabReport,
    LedgerEvent,
    LedgerView,
    NegotiationCase,
    NegotiationCause,
    NegotiationResolution,
    NegotiationStatus,
    ObligationReport,
    Parcel,
    PaymentKind,
    Principal,
    ProductBatch,
    ProductTrace,
    QualityMetrics,
    ReturnRecord,
    Role,
    SampleRecord,
    VersionStatus,
    Weighing,
    kg,
    yuan,
)
from .errors import (
    AccessDenied,
    ConsentRequired,
    DomainError,
    EvidenceRequired,
    InvalidState,
    NotFound,
    OutsideDeliveryWindow,
    OverReceiveError,
    QuantityExceeded,
)
from .store import Store


@dataclass
class _Intake:
    """批次收货台账：实收（称重净重累计）与当前有效接收量。"""

    weighed_kg: Decimal = Decimal("0")
    accepted_kg: Decimal = Decimal("0")


def _require_aware(at: datetime) -> None:
    if at.tzinfo is None or at.tzinfo.utcoffset(at) is None:
        raise DomainError("业务时间必须带时区")


class SettlementService:
    def __init__(self, store: Store | None = None) -> None:
        self.store = store or Store()

    # ------------------------------------------------------------------
    # 访问控制
    # ------------------------------------------------------------------
    def _commitment_or_404(self, commitment_id: str) -> Commitment:
        commitment = self.store.get("commitments", commitment_id)
        if commitment is None:
            raise NotFound(f"承诺不存在：{commitment_id}")
        return commitment

    def _check_commitment_access(self, principal: Principal, commitment: Commitment) -> None:
        if principal.role is Role.COOPERATIVE:
            return
        if principal.role is Role.WORKSHOP and commitment.workshop_id == principal.subject_id:
            return
        if principal.role is Role.FARMER and commitment.farmer_id == principal.subject_id:
            return
        raise AccessDenied(f"{principal.subject_id} 无权查看承诺 {commitment.commitment_id}")

    def _visible_commitment(self, principal: Principal, commitment_id: str) -> Commitment:
        commitment = self._commitment_or_404(commitment_id)
        self._check_commitment_access(principal, commitment)
        return commitment

    def _check_can_receive(self, principal: Principal, commitment: Commitment) -> None:
        if principal.role is Role.COOPERATIVE:
            return
        if principal.role is Role.WORKSHOP and commitment.workshop_id == principal.subject_id:
            return
        raise AccessDenied(f"{principal.subject_id} 无权在承诺 {commitment.commitment_id} 下登记收货")

    def _require_coop(self, principal: Principal) -> None:
        if principal.role is not Role.COOPERATIVE:
            raise AccessDenied("仅合作社可执行该操作")

    def list_commitments(self, principal: Principal) -> list[Commitment]:
        """任何工坊只能看到自己的采购关系。"""
        commitments = self.store.all("commitments")
        if principal.role is Role.COOPERATIVE:
            return commitments
        if principal.role is Role.WORKSHOP:
            return [c for c in commitments if c.workshop_id == principal.subject_id]
        return [c for c in commitments if c.farmer_id == principal.subject_id]

    def get_commitment(self, principal: Principal, commitment_id: str) -> Commitment:
        return self._visible_commitment(principal, commitment_id)

    # ------------------------------------------------------------------
    # 基础档案登记
    # ------------------------------------------------------------------
    def register_parcel(self, principal: Principal, parcel: Parcel) -> Parcel:
        self._require_coop(principal)
        record, _ = self.store.put("parcels", parcel.parcel_id, parcel)
        return record

    def register_batch(self, principal: Principal, batch: CropBatch) -> CropBatch:
        self._require_coop(principal)
        record, _ = self.store.put("batches", batch.batch_id, batch)
        return record

    def register_commitment(
        self,
        principal: Principal,
        commitment: Commitment,
        prepayment_paid_at: datetime | None = None,
    ) -> Commitment:
        """登记承诺。首个生效版本必须已附农户同意；预付款据此入账。"""
        self._require_coop(principal)
        active = commitment.active_version()
        if active.consent is None or not active.consent.consented:
            raise ConsentRequired("生效版本必须附农户明确同意")
        if self.store.get("batches", commitment.batch_id) is None:
            raise NotFound(f"批次不存在：{commitment.batch_id}")
        if active.terms.prepayment_total > 0:
            if prepayment_paid_at is None:
                raise DomainError("有预付款时必须提供付款时间")
            _require_aware(prepayment_paid_at)
        with self.store.locked():
            for other in self.store.all("commitments"):
                if other.batch_id == commitment.batch_id:
                    raise InvalidState(f"批次 {commitment.batch_id} 已绑定承诺 {other.commitment_id}")
            record, inserted = self.store.put("commitments", commitment.commitment_id, commitment)
            if inserted and active.terms.prepayment_total > 0:
                self._book(
                    LedgerEvent(
                        event_id=f"LEDGER-PREPAY-{commitment.commitment_id}",
                        commitment_id=commitment.commitment_id,
                        kind=PaymentKind.PREPAYMENT,
                        amount=-yuan(active.terms.prepayment_total),
                        reference_id=commitment.commitment_id,
                        note="保底预付款",
                        at=prepayment_paid_at,
                    )
                )
            return record

    # ------------------------------------------------------------------
    # 收货：称重、取样、实验室报告、部分接收、退货（均沿用原批次）
    # ------------------------------------------------------------------
    def _commitment_for_batch(self, batch_id: str) -> Commitment:
        for commitment in self.store.all("commitments"):
            if commitment.batch_id == batch_id:
                return commitment
        raise NotFound(f"批次 {batch_id} 没有关联承诺")

    def _intake(self, batch_id: str) -> _Intake:
        intake, _ = self.store.put("intake", batch_id, _Intake())
        return intake

    def record_weighing(self, principal: Principal, weighing: Weighing) -> Weighing:
        """登记称重。同一批次可在多个收货点分别称重，净重累计为实收数量。"""
        _require_aware(weighing.at)
        commitment = self._commitment_for_batch(weighing.batch_id)
        self._check_can_receive(principal, commitment)
        with self.store.locked():
            record, inserted = self.store.put("weighings", weighing.weighing_id, weighing)
            if inserted:
                self._intake(weighing.batch_id).weighed_kg += weighing.net_kg
            return record

    def record_sample(self, principal: Principal, sample: SampleRecord) -> SampleRecord:
        _require_aware(sample.at)
        commitment = self._commitment_for_batch(sample.batch_id)
        self._check_can_receive(principal, commitment)
        record, _ = self.store.put("samples", sample.sample_id, sample)
        return record

    def record_lab_report(self, principal: Principal, report: LabReport) -> LabReport:
        """登记实验室报告。更正报告（corrects 非空）沿用原批次，并对该批次

        已有接收按新指标重新定级，差额以补差事件入账。
        """
        self._require_coop(principal)
        _require_aware(report.issued_at)
        with self.store.locked():
            record, inserted = self.store.put("lab_reports", report.report_id, report)
            if inserted:
                self._regrade_batch(report.batch_id, report.metrics, report.issued_at)
            return record

    def _latest_metrics(self, batch_id: str) -> QualityMetrics | None:
        reports = self.store.filter("lab_reports", lambda r: r.batch_id == batch_id)
        if not reports:
            return None
        return max(reports, key=lambda r: r.issued_at).metrics

    def _latest_statement(self, acceptance_id: str) -> DeliveryStatement:
        statements = self.store.filter("statements", lambda s: s.acceptance_id == acceptance_id)
        if not statements:
            raise NotFound(f"接收 {acceptance_id} 没有结算单")
        return max(statements, key=lambda s: s.revision)

    def _build_statement(
        self,
        acceptance: Acceptance,
        commitment: Commitment,
        metrics: QualityMetrics,
        revision: int,
        issued_at: datetime,
        supersedes: str | None,
    ) -> DeliveryStatement:
        terms = commitment.version(acceptance.version).terms
        band = grading.select_band(metrics, terms.grading)
        deductions = grading.weight_deductions(metrics, terms.grading, acceptance.quantity_kg)
        deducted = sum((d.weight_kg for d in deductions if d.weight_kg), Decimal("0"))
        payable_kg = kg(max(acceptance.quantity_kg - deducted, Decimal("0")))
        price = grading.unit_price(
            terms.floor_price_per_kg, band, acceptance.market_reference_per_kg
        )
        gross = yuan(payable_kg * price)
        amount_deductions = sum((d.amount for d in deductions if d.amount), Decimal("0"))
        net = yuan(gross - amount_deductions)
        return DeliveryStatement(
            statement_id=f"STMT-{acceptance.acceptance_id}-r{revision}",
            acceptance_id=acceptance.acceptance_id,
            commitment_id=commitment.commitment_id,
            batch_id=acceptance.batch_id,
            farmer_id=commitment.farmer_id,
            workshop_id=commitment.workshop_id,
            quantity_kg=acceptance.quantity_kg,
            grade=band.grade,
            unit_price=price,
            payable_quantity_kg=payable_kg,
            deductions=deductions,
            gross_amount=gross,
            net_amount=net,
            expected_payment_date=(acceptance.at + timedelta(days=terms.payment_terms_days)).date(),
            terms_version=acceptance.version,
            revision=revision,
            issued_at=issued_at,
            supersedes=supersedes,
        )

    def _book(self, event: LedgerEvent) -> LedgerEvent:
        record, _ = self.store.put("ledger", event.event_id, event)
        return record

    def _regrade_batch(self, batch_id: str, metrics: QualityMetrics, at: datetime) -> None:
        """实验室更正后重算该批次所有接收的结算单，差额记补差。"""
        for acceptance in self.store.filter("acceptances", lambda a: a.batch_id == batch_id):
            commitment = self._commitment_or_404(acceptance.commitment_id)
            latest = self._latest_statement(acceptance.acceptance_id)
            revised = self._build_statement(
                acceptance, commitment, metrics, latest.revision + 1, at, latest.statement_id
            )
            if (
                revised.grade,
                revised.payable_quantity_kg,
                revised.unit_price,
                revised.net_amount,
            ) == (latest.grade, latest.payable_quantity_kg, latest.unit_price, latest.net_amount):
                continue
            self.store.put("statements", revised.statement_id, revised)
            delta = yuan(revised.net_amount - latest.net_amount)
            self._book(
                LedgerEvent(
                    event_id=f"LEDGER-ADJ-{revised.statement_id}",
                    commitment_id=commitment.commitment_id,
                    kind=PaymentKind.ADJUSTMENT,
                    amount=delta,
                    reference_id=revised.statement_id,
                    note=f"实验室更正重定级 {latest.grade.value}->{revised.grade.value}，补差",
                    at=at,
                )
            )

    def accept_delivery(
        self,
        principal: Principal,
        acceptance_id: str,
        batch_id: str,
        site_id: str,
        quantity_kg: Decimal,
        at: datetime,
        field_metrics: QualityMetrics | None = None,
        market_reference_per_kg: Decimal | None = None,
    ) -> DeliveryStatement:
        """（部分）接收一批交付，返回给农户的结算单。

        数量检查与入账在同一临界区：多个站点同时登记也不会突破实收数量。
        同一 acceptance_id 重试返回首次的结算单，不重复入账。
        """
        _require_aware(at)
        quantity_kg = kg(quantity_kg)
        if quantity_kg <= 0:
            raise DomainError("接收数量必须为正")
        commitment = self._commitment_for_batch(batch_id)
        self._check_can_receive(principal, commitment)
        terms = commitment.active_version().terms
        if not terms.window.contains(at.date()):
            raise OutsideDeliveryWindow(
                f"{at.date()} 不在交付窗口 {terms.window.opens_on}~{terms.window.closes_on} 内"
            )
        metrics = self._latest_metrics(batch_id) or field_metrics
        if metrics is None:
            raise DomainError("没有实验室报告时必须提供田间快检指标")

        with self.store.locked():
            existing = self.store.get("acceptances", acceptance_id)
            if existing is not None:
                return self._latest_statement(acceptance_id)

            intake = self._intake(batch_id)
            if intake.accepted_kg + quantity_kg > intake.weighed_kg:
                raise OverReceiveError(
                    f"批次 {batch_id} 实收 {intake.weighed_kg}kg，"
                    f"已接收 {intake.accepted_kg}kg，无法再接收 {quantity_kg}kg"
                )
            accepted_net = self._commitment_accepted_net(commitment.commitment_id)
            if accepted_net + quantity_kg > terms.committed_quantity_kg:
                raise QuantityExceeded(
                    f"承诺 {commitment.commitment_id} 总量 {terms.committed_quantity_kg}kg，"
                    f"已接收 {accepted_net}kg，超出部分请走替代去向"
                )

            acceptance = Acceptance(
                acceptance_id=acceptance_id,
                batch_id=batch_id,
                commitment_id=commitment.commitment_id,
                site_id=site_id,
                quantity_kg=quantity_kg,
                version=commitment.active_version().version,
                market_reference_per_kg=(
                    yuan(market_reference_per_kg) if market_reference_per_kg is not None else None
                ),
                at=at,
            )
            self.store.put("acceptances", acceptance_id, acceptance)
            intake.accepted_kg += quantity_kg
            statement = self._build_statement(acceptance, commitment, metrics, 1, at, None)
            self.store.put("statements", statement.statement_id, statement)
            self._book(
                LedgerEvent(
                    event_id=f"LEDGER-FINAL-{statement.statement_id}",
                    commitment_id=commitment.commitment_id,
                    kind=PaymentKind.FINAL,
                    amount=statement.net_amount,
                    reference_id=statement.statement_id,
                    note=f"{site_id} 接收 {quantity_kg}kg，等级 {statement.grade.value}",
                    at=at,
                )
            )
            return statement

    def _commitment_accepted_net(self, commitment_id: str) -> Decimal:
        acceptances = self.store.filter("acceptances", lambda a: a.commitment_id == commitment_id)
        accepted = sum((a.quantity_kg for a in acceptances), Decimal("0"))
        returned = sum(
            (r.quantity_kg for r in self.store.all("returns") if r.batch_id in {a.batch_id for a in acceptances}),
            Decimal("0"),
        )
        return accepted - returned

    def record_return(
        self,
        principal: Principal,
        return_id: str,
        acceptance_id: str,
        site_id: str,
        quantity_kg: Decimal,
        reason: str,
        at: datetime,
    ) -> ReturnRecord:
        """退货：冲减原接收，按该接收最新结算单价冲减尾款。"""
        _require_aware(at)
        quantity_kg = kg(quantity_kg)
        if quantity_kg <= 0:
            raise DomainError("退货数量必须为正")
        acceptance = self.store.get("acceptances", acceptance_id)
        if acceptance is None:
            raise NotFound(f"接收不存在：{acceptance_id}")
        commitment = self._commitment_or_404(acceptance.commitment_id)
        self._check_can_receive(principal, commitment)

        with self.store.locked():
            existing = self.store.get("returns", return_id)
            if existing is not None:
                return existing
            returned_so_far = sum(
                (r.quantity_kg for r in self.store.filter("returns", lambda r: r.acceptance_id == acceptance_id)),
                Decimal("0"),
            )
            if returned_so_far + quantity_kg > acceptance.quantity_kg:
                raise QuantityExceeded(
                    f"接收 {acceptance_id} 数量 {acceptance.quantity_kg}kg，"
                    f"已退 {returned_so_far}kg，无法再退 {quantity_kg}kg"
                )
            record = ReturnRecord(
                return_id=return_id,
                acceptance_id=acceptance_id,
                batch_id=acceptance.batch_id,
                site_id=site_id,
                quantity_kg=quantity_kg,
                reason=reason,
                at=at,
            )
            self.store.put("returns", return_id, record)
            self._intake(acceptance.batch_id).accepted_kg -= quantity_kg
            unit_price = self._latest_statement(acceptance_id).unit_price
            self._book(
                LedgerEvent(
                    event_id=f"LEDGER-RETURN-{return_id}",
                    commitment_id=commitment.commitment_id,
                    kind=PaymentKind.RETURN,
                    amount=-yuan(quantity_kg * unit_price),
                    reference_id=return_id,
                    note=f"退货 {quantity_kg}kg：{reason}",
                    at=at,
                )
            )
            return record

    # ------------------------------------------------------------------
    # 结算单与总账
    # ------------------------------------------------------------------
    def get_statement(self, principal: Principal, statement_id: str) -> DeliveryStatement:
        statement = self.store.get("statements", statement_id)
        if statement is None:
            raise NotFound(f"结算单不存在：{statement_id}")
        self._visible_commitment(principal, statement.commitment_id)
        return statement

    def list_statements(self, principal: Principal, commitment_id: str) -> list[DeliveryStatement]:
        self._visible_commitment(principal, commitment_id)
        statements = self.store.filter("statements", lambda s: s.commitment_id == commitment_id)
        latest: dict[str, DeliveryStatement] = {}
        for statement in statements:
            current = latest.get(statement.acceptance_id)
            if current is None or statement.revision > current.revision:
                latest[statement.acceptance_id] = statement
        return sorted(latest.values(), key=lambda s: s.issued_at)

    def recompute_ledger(self, principal: Principal, commitment_id: str) -> LedgerView:
        """从台账事件复算总账：预付款、尾款、退货、补差与应付余额。"""
        self._visible_commitment(principal, commitment_id)
        events = sorted(
            self.store.filter("ledger", lambda e: e.commitment_id == commitment_id),
            key=lambda e: (e.at, e.event_id),
        )

        def total(kind: PaymentKind) -> Decimal:
            return yuan(sum((e.amount for e in events if e.kind is kind), Decimal("0")))

        prepayments = -total(PaymentKind.PREPAYMENT)
        finals = total(PaymentKind.FINAL)
        returns = -total(PaymentKind.RETURN)
        adjustments = total(PaymentKind.ADJUSTMENT)
        balance = yuan(finals + adjustments - prepayments - returns)
        return LedgerView(
            commitment_id=commitment_id,
            prepayments=prepayments,
            final_payables=finals,
            returns=returns,
            adjustments=adjustments,
            balance_due=balance,
            events=tuple(events),
        )

    # ------------------------------------------------------------------
    # 条款版本：价格或规则变化只进入农户明确同意的新版本
    # ------------------------------------------------------------------
    def propose_terms(
        self,
        principal: Principal,
        commitment_id: str,
        terms: CommitmentTerms,
        at: datetime,
    ) -> CommitmentVersion:
        _require_aware(at)
        commitment = self._commitment_or_404(commitment_id)
        if principal.role is Role.FARMER:
            raise AccessDenied("农户不能发起条款变更")
        self._check_commitment_access(principal, commitment)
        with self.store.locked():
            number = max(v.version for v in commitment.versions) + 1
            version = CommitmentVersion(
                version=number, terms=terms, status=VersionStatus.DRAFT, created_at=at
            )
            commitment.versions.append(version)
            return version

    def consent_terms(
        self,
        principal: Principal,
        commitment_id: str,
        version_number: int,
        agree: bool,
        at: datetime,
        channel: str = "当面确认",
    ) -> CommitmentVersion:
        """农户对新版本表态。只有明确同意才生效，否则维持原版本。"""
        _require_aware(at)
        commitment = self._commitment_or_404(commitment_id)
        if principal.role is not Role.FARMER or principal.subject_id != commitment.farmer_id:
            raise AccessDenied("只有承诺对应的农户本人可以表态")
        with self.store.locked():
            draft = commitment.version(version_number)
            if draft.status is not VersionStatus.DRAFT:
                raise InvalidState(f"版本 {version_number} 当前为 {draft.status.value}，不能表态")
            draft.consent = ConsentRecord(
                farmer_id=principal.subject_id, consented=agree, at=at, channel=channel
            )
            if agree:
                for item in commitment.versions:
                    if item.status is VersionStatus.ACTIVE:
                        item.status = VersionStatus.SUPERSEDED
                draft.status = VersionStatus.ACTIVE
            else:
                draft.status = VersionStatus.REJECTED
            return draft

    # ------------------------------------------------------------------
    # 减产协商：先证据、后定案，不可抗与未履约分别记录
    # ------------------------------------------------------------------
    def open_negotiation(
        self,
        principal: Principal,
        case_id: str,
        commitment_id: str,
        cause: NegotiationCause,
        claimed_shortfall_kg: Decimal,
        at: datetime,
    ) -> NegotiationCase:
        _require_aware(at)
        commitment = self._visible_commitment(principal, commitment_id)
        if principal.role is Role.WORKSHOP:
            raise AccessDenied("工坊不能发起减产协商")
        case = NegotiationCase(
            case_id=case_id,
            commitment_id=commitment.commitment_id,
            cause=cause,
            claimed_shortfall_kg=kg(claimed_shortfall_kg),
            opened_by=principal.subject_id,
            opened_at=at,
        )
        record, _ = self.store.put("negotiations", case_id, case)
        return record

    def submit_evidence(
        self, principal: Principal, case_id: str, evidence: Evidence
    ) -> NegotiationCase:
        _require_aware(evidence.at)
        case = self._negotiation_or_404(case_id)
        self._visible_commitment(principal, case.commitment_id)
        if principal.role is Role.WORKSHOP:
            raise AccessDenied("工坊不能提交协商证据")
        if case.status not in (NegotiationStatus.PROPOSED, NegotiationStatus.EVIDENCED):
            raise InvalidState(f"协商 {case_id} 已定案，不能再补证据")
        case.evidence.append(evidence)
        case.status = NegotiationStatus.EVIDENCED
        return case

    def resolve_negotiation(
        self,
        principal: Principal,
        case_id: str,
        agree: bool,
        at: datetime,
        force_majeure_kg: Decimal = Decimal("0"),
        non_performance_kg: Decimal = Decimal("0"),
    ) -> NegotiationCase:
        self._require_coop(principal)
        _require_aware(at)
        case = self._negotiation_or_404(case_id)
        if case.status in (NegotiationStatus.AGREED, NegotiationStatus.REJECTED):
            raise InvalidState(f"协商 {case_id} 已定案")
        if not case.evidence:
            raise EvidenceRequired("定案前必须先提交证据")
        force_majeure_kg = kg(force_majeure_kg)
        non_performance_kg = kg(non_performance_kg)
        if agree:
            outstanding = self._outstanding_quantity(case.commitment_id)
            if force_majeure_kg + non_performance_kg > outstanding:
                raise QuantityExceeded(
                    f"不可抗与未履约合计 {force_majeure_kg + non_performance_kg}kg "
                    f"超过未履约余量 {outstanding}kg"
                )
            case.resolution = NegotiationResolution(
                force_majeure_kg=force_majeure_kg,
                non_performance_kg=non_performance_kg,
                decided_by=principal.subject_id,
                at=at,
            )
            case.status = NegotiationStatus.AGREED
        else:
            case.status = NegotiationStatus.REJECTED
        return case

    def _negotiation_or_404(self, case_id: str) -> NegotiationCase:
        case = self.store.get("negotiations", case_id)
        if case is None:
            raise NotFound(f"协商不存在：{case_id}")
        return case

    def _outstanding_quantity(self, commitment_id: str) -> Decimal:
        commitment = self._commitment_or_404(commitment_id)
        committed = commitment.active_version().terms.committed_quantity_kg
        excused = sum(
            (
                c.resolution.force_majeure_kg + c.resolution.non_performance_kg
                for c in self.store.filter(
                    "negotiations",
                    lambda c: c.commitment_id == commitment_id
                    and c.status is NegotiationStatus.AGREED
                    and c.resolution is not None,
                )
            ),
            Decimal("0"),
        )
        outstanding = committed - self._commitment_accepted_net(commitment_id) - excused
        return max(kg(outstanding), Decimal("0"))

    def obligation_report(self, principal: Principal, commitment_id: str) -> ObligationReport:
        """履约情况：实收、退货、不可抗减免与未履约分别列示。"""
        commitment = self._visible_commitment(principal, commitment_id)
        terms = commitment.active_version().terms
        agreed = self.store.filter(
            "negotiations",
            lambda c: c.commitment_id == commitment_id
            and c.status is NegotiationStatus.AGREED
            and c.resolution is not None,
        )
        excused = sum((c.resolution.force_majeure_kg for c in agreed), Decimal("0"))
        non_performance = sum((c.resolution.non_performance_kg for c in agreed), Decimal("0"))
        accepted_net = self._commitment_accepted_net(commitment_id)
        returned = sum(
            (
                r.quantity_kg
                for r in self.store.filter("returns", lambda r: r.batch_id == commitment.batch_id)
            ),
            Decimal("0"),
        )
        outstanding = max(
            terms.committed_quantity_kg - accepted_net - excused - non_performance, Decimal("0")
        )
        return ObligationReport(
            commitment_id=commitment_id,
            committed_quantity_kg=terms.committed_quantity_kg,
            accepted_quantity_kg=kg(accepted_net),
            returned_quantity_kg=kg(returned),
            force_majeure_excused_kg=kg(excused),
            non_performance_kg=kg(non_performance),
            outstanding_quantity_kg=kg(outstanding),
        )

    # ------------------------------------------------------------------
    # 申诉
    # ------------------------------------------------------------------
    def file_appeal(
        self,
        principal: Principal,
        appeal_id: str,
        statement_id: str,
        reason: str,
        at: datetime,
    ) -> Appeal:
        _require_aware(at)
        statement = self.store.get("statements", statement_id)
        if statement is None:
            raise NotFound(f"结算单不存在：{statement_id}")
        if principal.role is not Role.FARMER or principal.subject_id != statement.farmer_id:
            raise AccessDenied("只有结算单对应的农户本人可以申诉")
        appeal = Appeal(
            appeal_id=appeal_id,
            statement_id=statement_id,
            commitment_id=statement.commitment_id,
            farmer_id=statement.farmer_id,
            reason=reason,
            filed_at=at,
        )
        record, _ = self.store.put("appeals", appeal_id, appeal)
        return record

    def review_appeal(self, principal: Principal, appeal_id: str) -> Appeal:
        self._require_coop(principal)
        appeal = self._appeal_or_404(appeal_id)
        if appeal.status is not AppealStatus.FILED:
            raise InvalidState(f"申诉 {appeal_id} 当前为 {appeal.status.value}")
        appeal.status = AppealStatus.UNDER_REVIEW
        return appeal

    def resolve_appeal(
        self,
        principal: Principal,
        appeal_id: str,
        uphold: bool,
        note: str,
        at: datetime,
        adjustment_amount: Decimal = Decimal("0"),
    ) -> Appeal:
        """裁定申诉。裁补时以补差事件入账，金额可正可负。"""
        self._require_coop(principal)
        _require_aware(at)
        appeal = self._appeal_or_404(appeal_id)
        if appeal.status not in (AppealStatus.FILED, AppealStatus.UNDER_REVIEW):
            raise InvalidState(f"申诉 {appeal_id} 已裁定")
        if uphold:
            appeal.status = AppealStatus.UPHELD
        else:
            adjustment_amount = yuan(adjustment_amount)
            if adjustment_amount == 0:
                raise DomainError("裁补必须给出非零补差金额")
            appeal.status = AppealStatus.ADJUSTED
            appeal.adjustment_amount = adjustment_amount
            self._book(
                LedgerEvent(
                    event_id=f"LEDGER-APPEAL-{appeal_id}",
                    commitment_id=appeal.commitment_id,
                    kind=PaymentKind.ADJUSTMENT,
                    amount=adjustment_amount,
                    reference_id=appeal_id,
                    note=f"申诉裁定补差：{note}",
                    at=at,
                )
            )
        appeal.resolution_note = note
        appeal.decided_at = at
        return appeal

    def _appeal_or_404(self, appeal_id: str) -> Appeal:
        appeal = self.store.get("appeals", appeal_id)
        if appeal is None:
            raise NotFound(f"申诉不存在：{appeal_id}")
        return appeal

    # ------------------------------------------------------------------
    # 替代去向
    # ------------------------------------------------------------------
    def record_destination(
        self,
        principal: Principal,
        record_id: str,
        batch_id: str,
        kind: str,
        quantity_kg: Decimal,
        reason: str,
        at: datetime,
    ) -> DestinationRecord:
        """登记未进入工坊的批次数量去向（替代买家、饲料化等）。"""
        _require_aware(at)
        commitment = self._commitment_for_batch(batch_id)
        self._check_can_receive(principal, commitment)
        terms = commitment.active_version().terms
        if terms.allowed_destinations and kind not in terms.allowed_destinations:
            raise InvalidState(f"去向 {kind} 不在承诺允许的范围 {terms.allowed_destinations} 内")
        record = DestinationRecord(
            record_id=record_id,
            batch_id=batch_id,
            commitment_id=commitment.commitment_id,
            kind=kind,
            quantity_kg=kg(quantity_kg),
            reason=reason,
            at=at,
        )
        stored, _ = self.store.put("destinations", record_id, record)
        return stored

    def list_destinations(self, principal: Principal, batch_id: str) -> list[DestinationRecord]:
        commitment = self._commitment_for_batch(batch_id)
        self._check_commitment_access(principal, commitment)
        return self.store.filter("destinations", lambda d: d.batch_id == batch_id)

    # ------------------------------------------------------------------
    # 成品追溯：从成品所用原料反查批次与结清状态
    # ------------------------------------------------------------------
    def register_product_batch(self, principal: Principal, product: ProductBatch) -> ProductBatch:
        _require_aware(product.produced_at)
        if principal.role is Role.WORKSHOP and product.workshop_id != principal.subject_id:
            raise AccessDenied("工坊只能登记自己的成品批次")
        if principal.role is Role.FARMER:
            raise AccessDenied("农户不能登记成品批次")
        for ingredient in product.ingredients:
            if self.store.get("batches", ingredient.batch_id) is None:
                raise NotFound(f"投料批次不存在：{ingredient.batch_id}")
        record, _ = self.store.put("products", product.product_id, product)
        return record

    def trace_product(self, principal: Principal, product_id: str) -> ProductTrace:
        product = self.store.get("products", product_id)
        if product is None:
            raise NotFound(f"成品批次不存在：{product_id}")
        if principal.role is Role.WORKSHOP and product.workshop_id != principal.subject_id:
            raise AccessDenied("工坊只能追溯自己的成品批次")
        if principal.role is Role.FARMER:
            raise AccessDenied("农户不能追溯成品批次")
        ingredients: list[IngredientTrace] = []
        for usage in product.ingredients:
            batch = self.store.get("batches", usage.batch_id)
            parcel = self.store.get("parcels", batch.parcel_id)
            commitment = self._commitment_for_batch(usage.batch_id)
            accepted_net = self._commitment_accepted_net(commitment.commitment_id)
            ledger = self.recompute_ledger(principal, commitment.commitment_id)
            if accepted_net <= 0:
                status = "no_delivery"
            elif ledger.balance_due == 0:
                status = "settled"
            else:
                status = "open"
            ingredients.append(
                IngredientTrace(
                    batch_id=usage.batch_id,
                    parcel_id=batch.parcel_id,
                    farmer_id=parcel.farmer_id,
                    commitment_id=commitment.commitment_id,
                    used_quantity_kg=usage.quantity_kg,
                    accepted_quantity_kg=kg(accepted_net),
                    settlement_status=status,
                    balance_due=ledger.balance_due,
                )
            )
        return ProductTrace(
            product_id=product.product_id,
            workshop_id=product.workshop_id,
            ingredients=tuple(ingredients),
        )

    def products_using_batch(self, principal: Principal, batch_id: str) -> list[str]:
        commitment = self._commitment_for_batch(batch_id)
        self._check_commitment_access(principal, commitment)
        products = self.store.filter(
            "products", lambda p: any(u.batch_id == batch_id for u in p.ingredients)
        )
        if principal.role is Role.WORKSHOP:
            products = [p for p in products if p.workshop_id == principal.subject_id]
        return sorted(p.product_id for p in products)
