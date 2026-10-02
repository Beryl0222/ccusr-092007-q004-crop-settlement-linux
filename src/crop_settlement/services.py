"""领域服务：保底收购、浮动分级、交付验收、不可抗协商、总账复算与申诉。

所有写操作都在单次 ``store.transaction()`` 内完成读-校验-写；
``expected_version`` 由调用方（HTTP 层）传入，实现乐观并发。

数量守恒（存储层加锁后由本服务校验）：

    某交付的全部处置数量之和 == 该交付净重

因此多个收货点、多笔处置并发登记时，最后一笔会因
:class:`~crop_settlement.errors.QuantityOverflow` 被拒绝，绝不突破实收数量。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional

from .errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    QuantityOverflow,
    ValidationError,
)
from .models import (
    Advance,
    Appeal,
    AppealStatus,
    Commitment,
    CommitmentStatus,
    CommitmentTerms,
    CommitmentVersion,
    Consumption,
    ConsumptionLine,
    CropBatch,
    CropType,
    Deduction,
    Delivery,
    DeliveryStatus,
    Disposition,
    DispositionKind,
    FMEventType,
    ForceMajeureEvent,
    Grading,
    GradingStatus,
    LedgerAdjustment,
    Negotiation,
    NegotiationStatus,
    Party,
    PartyType,
    PaymentState,
    Plot,
    PriceTier,
    Sample,
    Settlement,
    Station,
    to_dict,
)
from .store import Store

ZERO = Decimal("0")


# ---------------------------------------------------------------- 辅助


def _now() -> datetime:
    return datetime.now().astimezone()


def _q(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _grade_price(terms: CommitmentTerms, grade: str) -> PriceTier:
    for tier in terms.grade_prices:
        if tier.grade == grade:
            return tier
    raise ValidationError(f"等级 {grade} 不在承诺分级表中")


def _agreed(commitment: Commitment) -> CommitmentVersion:
    for v in reversed(commitment.versions):
        if v.status == CommitmentStatus.AGREED:
            return v
    raise ValidationError(f"承诺 {commitment.id} 尚无农户明确同意的版本")


# ---------------------------------------------------------------- 服务


class SettlementService:
    def __init__(self, store: Store) -> None:
        self.store = store

    # ================================================================
    # 目录：参与方 / 站点 / 地块 / 批次
    # ================================================================

    def register_party(
        self,
        party_id: str,
        type_: str,
        name: str,
        token: str = "",
    ) -> Party:
        party = Party(id=party_id, type=_enum(type_, PartyType), name=name, token=token)
        self.store.save_party(party)
        return party

    def open_station(self, workshop: Party, station_id: str, name: str, location: str = "") -> Station:
        self._require_type(workshop, "workshop")
        station = Station(id=station_id, workshop_id=workshop.id, name=name, location=location)
        self.store.save_station(station)
        return station

    def register_plot(
        self, farmer: Party, plot_id: str, crop: str, location: str = "", area_mu=ZERO
    ) -> Plot:
        self._require_type(farmer, "farmer")
        plot = Plot(
            id=plot_id, farmer_id=farmer.id, crop=_enum(crop, CropType),
            location=location, area_mu=_q(area_mu),
        )
        self.store.save_plot(plot)
        return plot

    def register_batch(
        self,
        batch_id: str,
        plot_id: str,
        crop: str,
        planted_at: Optional[date] = None,
        harvest_expected_at: Optional[date] = None,
        estimated_yield_kg=ZERO,
    ) -> CropBatch:
        plot = self.store.plots.get(plot_id)
        if plot is None:
            raise NotFoundError(f"地块 {plot_id} 不存在")
        batch = CropBatch(
            id=batch_id,
            plot_id=plot_id,
            farmer_id=plot.farmer_id,
            crop=_enum(crop, CropType),
            planted_at=planted_at,
            harvest_expected_at=harvest_expected_at,
            estimated_yield_kg=_q(estimated_yield_kg),
        )
        self.store.save_batch(batch)
        return batch

    # ================================================================
    # 承诺：版本化；价格/规则变化只进农户明确同意的新版本
    # ================================================================

    def create_commitment(
        self,
        commitment_id: str,
        farmer_id: str,
        workshop_id: str,
        batch_id: str,
        crop: str,
        floor_quantity_kg,
        terms: CommitmentTerms,
        created_by: str,
    ) -> Commitment:
        farmer = self.store.get_party(farmer_id)
        workshop = self.store.get_party(workshop_id)
        if farmer.type.value != "farmer":
            raise ValidationError(f"{farmer_id} 不是农户")
        if workshop.type.value != "workshop":
            raise ValidationError(f"{workshop_id} 不是工坊")
        batch = self.store.get_batch(batch_id)
        if batch.farmer_id != farmer_id:
            raise ValidationError("批次不属于该农户")
        if terms.floor_price_per_kg < 0:
            raise ValidationError("保底价不能为负")
        self._validate_terms(terms)
        commitment = Commitment(
            id=commitment_id,
            farmer_id=farmer_id,
            workshop_id=workshop_id,
            batch_id=batch_id,
            crop=_enum(crop, CropType),
            status=CommitmentStatus.PROPOSED,
        )
        commitment.versions.append(
            CommitmentVersion(
                version_no=1,
                status=CommitmentStatus.PROPOSED,
                terms=terms,
                floor_quantity_kg=_q(floor_quantity_kg),
                created_at=_now(),
                created_by=created_by,
                change_summary="首版条款",
            )
        )
        self.store.save_commitment(commitment)
        return commitment

    def agree_commitment(
        self, commitment_id: str, farmer: Party, expected_version: Optional[int] = None
    ) -> Commitment:
        """农户明确同意当前最新版本。"""

        with self.store.transaction():
            c = self.store.get_commitment(commitment_id)
            if farmer.id != c.farmer_id:
                raise PermissionDenied("只有农户本人能同意承诺")
            cv = c.current
            if cv.status == CommitmentStatus.AGREED:
                raise ConflictError("该版本已同意")
            # 新版本生效：冻结此前所有已同意版本；待同意期间旧版本继续有效
            for prev in c.versions[:-1]:
                if prev.status == CommitmentStatus.AGREED:
                    prev.status = CommitmentStatus.SUPERSEDED
            cv.status = CommitmentStatus.AGREED
            cv.agreed_at = _now()
            cv.agreed_by = farmer.id
            c.status = CommitmentStatus.AGREED
            self.store.save_commitment(c, expected_version)
            return c

    def revise_commitment(
        self,
        commitment_id: str,
        terms: CommitmentTerms,
        created_by: str,
        change_summary: str,
        expected_version: Optional[int] = None,
        floor_quantity_kg=None,
    ) -> Commitment:
        """发起新版本：旧 AGREED 版本冻结为 superseded，新版本保持 PROPOSED 直到农户同意。"""

        with self.store.transaction():
            c = self.store.get_commitment(commitment_id)
            current = c.current
            if current.status != CommitmentStatus.AGREED:
                raise ValidationError("只能在已同意版本的基础上发起修订")
            self._validate_terms(terms)
            # 旧 AGREED 版本在新版本待同意期间继续有效；农户同意新版本时才冻结
            c.status = CommitmentStatus.PROPOSED
            c.versions.append(
                CommitmentVersion(
                    version_no=current.version_no + 1,
                    status=CommitmentStatus.PROPOSED,
                    terms=terms,
                    floor_quantity_kg=_q(floor_quantity_kg) if floor_quantity_kg is not None else current.floor_quantity_kg,
                    created_at=_now(),
                    created_by=created_by,
                    change_summary=change_summary,
                )
            )
            self.store.save_commitment(c, expected_version)
            return c

    # ================================================================
    # 预付款
    # ================================================================

    def pay_advance(
        self, commitment_id: str, amount, workshop: Party, note: str = "",
        advance_id: Optional[str] = None,
    ) -> Advance:
        with self.store.transaction():
            c = self.store.require_workshop_relation(commitment_id, workshop)
            amount = _q(amount)
            if amount <= 0:
                raise ValidationError("预付款金额必须为正")
            total = sum((a.amount for a in self.store.advances_for(commitment_id)), ZERO)
            limit = _agreed(c).terms.prepayment_amount
            if total + amount > limit:
                raise ValidationError(
                    f"预付款累计 {total + amount} 超过承诺预付额 {limit}"
                )
            advance = Advance(
                id=advance_id or self.store.next_id("adv"),
                commitment_id=commitment_id,
                amount=amount,
                paid_at=_now(),
                paid_by=workshop.id,
                note=note,
            )
            self.store.save_advance(advance)
            return advance

    # ================================================================
    # 交付称重（多站点）
    # ================================================================

    def register_delivery(
        self,
        commitment_id: str,
        station_id: str,
        gross_kg,
        tare_kg,
        weighed_by: str,
        ticket_no: str = "",
        delivery_id: Optional[str] = None,
        expected_commitment_version: Optional[int] = None,
        on_date: Optional[date] = None,
    ) -> Delivery:
        gross, tare = _q(gross_kg), _q(tare_kg)
        if gross < 0 or tare < 0:
            raise ValidationError("重量不能为负")
        if tare > gross:
            raise ValidationError("皮重不能大于毛重")
        net = gross - tare
        today = on_date or date.today()
        with self.store.transaction():
            c = self.store.get_commitment(commitment_id)
            station = self.store.get_station(station_id)
            if station.workshop_id != c.workshop_id:
                raise PermissionDenied("收货点不属于该采购关系的工坊")
            agreed = _agreed(c)
            if not (agreed.terms.delivery_window.start <= today <= agreed.terms.delivery_window.end):
                raise ValidationError(
                    f"当前日期不在交付窗口 {agreed.terms.delivery_window.start}~"
                    f"{agreed.terms.delivery_window.end} 内"
                )
            existing_net = sum(
                (d.net_kg for d in self.store.deliveries_for(commitment_id)), ZERO
            )
            if existing_net + net > agreed.floor_quantity_kg * 2:
                # 物理合理性护栏：登记总量不得超过保底量的 200%，防止站点错单
                raise QuantityOverflow(
                    f"交付累计净重 {existing_net + net} 超过保底数量的两倍"
                )
            delivery = Delivery(
                id=delivery_id or self.store.next_id("dlv"),
                batch_id=c.batch_id,
                commitment_id=commitment_id,
                station_id=station_id,
                workshop_id=c.workshop_id,
                farmer_id=c.farmer_id,
                gross_kg=gross,
                tare_kg=tare,
                net_kg=net,
                weighed_at=(
                    datetime.combine(on_date, datetime.min.time()).astimezone()
                    if on_date else _now()
                ),
                weighed_by=weighed_by,
                ticket_no=ticket_no,
            )
            self.store.save_delivery(delivery)
            return delivery

    # ================================================================
    # 封样 / 初检 / 实验室更正（沿用原批次与原交付）
    # ================================================================

    def seal_sample(self, delivery_id: str, sealed_code: str, sampled_by: str,
                    sample_id: Optional[str] = None) -> Sample:
        sample = Sample(
            id=sample_id or self.store.next_id("smp"),
            delivery_id=delivery_id,
            sealed_code=sealed_code,
            sampled_at=_now(),
            sampled_by=sampled_by,
        )
        self.store.save_sample(sample)
        return sample

    def grade_sample(
        self,
        sample_id: str,
        grade: str,
        moisture_pct,
        impurity_pct,
        graded_by: str,
        is_lab_result: bool = False,
        note: str = "",
        grading_id: Optional[str] = None,
    ) -> Grading:
        with self.store.transaction():
            sample = self.store.samples.get(sample_id)
            if sample is None:
                raise NotFoundError(f"样品 {sample_id} 不存在")
            delivery = self.store.get_delivery(sample.delivery_id)
            commitment = self.store.get_commitment(delivery.commitment_id)
            _grade_price(_agreed(commitment).terms, grade)
            grading = Grading(
                id=grading_id or self.store.next_id("grd"),
                sample_id=sample_id,
                delivery_id=delivery.id,
                grade=grade,
                moisture_pct=_q(moisture_pct),
                impurity_pct=_q(impurity_pct),
                graded_at=_now(),
                graded_by=graded_by,
                is_lab_result=is_lab_result,
                note=note,
            )
            self.store.save_grading(grading)
            return grading

    def correct_grade(
        self,
        previous_grading_id: str,
        new_grade: str,
        new_moisture_pct,
        new_impurity_pct,
        corrected_by: str,
        note: str = "",
    ) -> Grading:
        """实验室更正：旧记录置 corrected 并指向新记录；单据仍挂原样品/原交付/原批次。"""

        with self.store.transaction():
            old = self.store.gradings.get(previous_grading_id)
            if old is None:
                raise NotFoundError(f"检验记录 {previous_grading_id} 不存在")
            delivery = self.store.get_delivery(old.delivery_id)
            commitment = self.store.get_commitment(delivery.commitment_id)
            _grade_price(_agreed(commitment).terms, new_grade)
            old.status = GradingStatus.CORRECTED
            new = Grading(
                id=self.store.next_id("grd"),
                sample_id=old.sample_id,
                delivery_id=old.delivery_id,
                grade=new_grade,
                moisture_pct=_q(new_moisture_pct),
                impurity_pct=_q(new_impurity_pct),
                graded_at=_now(),
                graded_by=corrected_by,
                is_lab_result=True,
                note=note,
            )
            old.superseded_by = new.id
            self.store.save_grading(old, old.version)
            self.store.save_grading(new)
            return new

    # ================================================================
    # 处置：部分接收 / 退货 / 替代去向（数量守恒，原子校验）
    # ================================================================

    def record_disposition(
        self,
        delivery_id: str,
        kind: str,
        quantity_kg,
        actor: Party,
        grade: Optional[str] = None,
        deductions: Optional[list[Deduction]] = None,
        note: str = "",
        alternate_destination: str = "",
        farmer_consent: bool = False,
        disposition_id: Optional[str] = None,
        expected_delivery_version: Optional[int] = None,
    ) -> Disposition:
        kind_enum = _enum(kind, DispositionKind)
        qty = _q(quantity_kg)
        if qty <= 0:
            raise ValidationError("处置数量必须为正")
        with self.store.transaction():
            delivery = self.store.get_delivery(delivery_id)
            c = self.store.get_commitment(delivery.commitment_id)
            terms = _agreed(c).terms

            # 权限：工坊登记接收/退货；替代去向还需农户明确同意
            if actor.type.value == "coop":
                pass
            elif actor.id != c.workshop_id:
                raise PermissionDenied("只有采购工坊可以登记处置")
            if actor.id == c.farmer_id:
                raise PermissionDenied("农户不登记处置")

            # 等级与价格校验
            if kind_enum == DispositionKind.ACCEPT:
                if grade is None:
                    raise ValidationError("接收必须指定等级")
                _grade_price(terms, grade)
            if kind_enum == DispositionKind.ALTERNATE:
                if not terms.alternate_allowed:
                    raise ValidationError("当前条款不允许替代去向")
                if not alternate_destination:
                    raise ValidationError("替代去向必须写明目的地")
                if not farmer_consent:
                    raise PermissionDenied("替代去向必须取得农户明确同意")

            # 数量守恒：所有处置之和不得超过净重
            used = sum(
                (d.quantity_kg for d in self.store.dispositions_for(delivery_id)), ZERO
            )
            if used + qty > delivery.net_kg:
                raise QuantityOverflow(
                    f"交付 {delivery_id} 净重 {delivery.net_kg}kg，"
                    f"已处置 {used}kg，再登记 {qty}kg 将突破实收数量"
                )

            for d in deductions or []:
                if d.amount < 0:
                    raise ValidationError("扣减金额不能为负")

            disposition = Disposition(
                id=disposition_id or self.store.next_id("dsp"),
                delivery_id=delivery_id,
                kind=kind_enum,
                quantity_kg=qty,
                grade=grade,
                deductions=list(deductions or []),
                decided_at=_now(),
                decided_by=actor.id,
                note=note,
                alternate_destination=alternate_destination,
                farmer_consent_at=_now() if farmer_consent else None,
            )
            self.store.save_disposition(disposition)

            # 更新交付聚合状态（乐观版本）
            self._refresh_delivery_status(delivery, expected_delivery_version)
            return disposition

    def _refresh_delivery_status(self, delivery: Delivery, expected: Optional[int]) -> None:
        items = self.store.dispositions_for(delivery.id)
        total = sum((d.quantity_kg for d in items), ZERO)
        accepted = sum((d.quantity_kg for d in items if d.kind == DispositionKind.ACCEPT), ZERO)
        returned = sum((d.quantity_kg for d in items if d.kind == DispositionKind.RETURN), ZERO)
        alternate = sum((d.quantity_kg for d in items if d.kind == DispositionKind.ALTERNATE), ZERO)
        if total == 0:
            return
        if total < delivery.net_kg:
            if accepted > 0:
                delivery.status = DeliveryStatus.PARTIALLY_ACCEPTED
            elif returned > 0:
                delivery.status = DeliveryStatus.PARTIALLY_RETURNED
        else:  # total == net_kg，全部处置完
            if accepted == delivery.net_kg:
                delivery.status = DeliveryStatus.ACCEPTED
            else:
                delivery.status = DeliveryStatus.SETTLED_CLOSED
        self.store.save_delivery(delivery, expected)

    # ================================================================
    # 成品投料登记：成品反查批次与结清状态
    # ================================================================

    def record_consumption(
        self,
        consumption_id: str,
        product_lot: str,
        lines: list[tuple[str, Optional[str], Decimal]],
        workshop: Party,
    ) -> Consumption:
        with self.store.transaction():
            self._require_type(workshop, "workshop")
            parsed: list[ConsumptionLine] = []
            for batch_id, delivery_id, qty in lines:
                qty = _q(qty)
                if qty <= 0:
                    raise ValidationError("投料数量必须为正")
                batch = self.store.get_batch(batch_id)
                # 只能耗用本工坊采购的原料
                related = [
                    c for c in self.store.commitments.values()
                    if c.batch_id == batch_id and c.workshop_id == workshop.id
                ]
                if not related:
                    raise PermissionDenied(f"批次 {batch_id} 与本工坊无采购关系")
                if delivery_id is not None:
                    d = self.store.get_delivery(delivery_id)
                    if d.batch_id != batch_id:
                        raise ValidationError("交付与批次不匹配")
                parsed.append(ConsumptionLine(batch_id=batch_id, delivery_id=delivery_id, quantity_kg=qty))
            consumption = Consumption(
                id=consumption_id,
                workshop_id=workshop.id,
                product_lot=product_lot,
                used_at=_now(),
                lines=parsed,
            )
            self.store.save_consumption(consumption)
            return consumption

    def trace_product(self, product_lot: str, viewer: Party) -> list[dict]:
        """从成品批次反查原料批次、交付与结清状态。"""

        result = []
        for c in self.store.consumptions.values():
            if c.product_lot != product_lot:
                continue
            if viewer.type.value != "coop" and c.workshop_id != viewer.id:
                raise PermissionDenied("只能查看自己工坊的成品")
            for line in c.lines:
                entry: dict = {
                    "batch_id": line.batch_id,
                    "delivery_id": line.delivery_id,
                    "quantity_kg": str(line.quantity_kg),
                    "commitments": [],
                }
                for cm in self.store.commitments.values():
                    if cm.batch_id != line.batch_id:
                        continue
                    if viewer.type.value == "workshop" and cm.workshop_id != viewer.id:
                        continue
                    entry["commitments"].append(
                        {
                            "commitment_id": cm.id,
                            "settled": cm.settled,
                            "status": cm.status.value,
                        }
                    )
                result.append(entry)
        return result

    # ================================================================
    # 不可抗事件 + 有证据的协商
    # ================================================================

    def record_fm_event(
        self,
        event_id: str,
        type_: str,
        title: str,
        started_at: date,
        region: str,
        evidence_refs: list[str],
        recorded_by: str,
        ended_at: Optional[date] = None,
    ) -> ForceMajeureEvent:
        if not evidence_refs:
            raise ValidationError("不可抗事件必须附证据（气象证明等）")
        event = ForceMajeureEvent(
            id=event_id,
            type=_enum(type_, FMEventType),
            title=title,
            started_at=started_at,
            ended_at=ended_at,
            region=region,
            evidence_refs=list(evidence_refs),
            recorded_by=recorded_by,
            recorded_at=_now(),
            confirmed=True,
        )
        self.store.save_fm_event(event)
        return event

    def open_negotiation(
        self,
        negotiation_id: str,
        commitment_id: str,
        fm_event_id: str,
        opened_by: Party,
        evidence_refs: list[str],
        shortfall_total_kg,
    ) -> Negotiation:
        if not evidence_refs:
            raise ValidationError("协商必须附减产证据")
        shortfall = _q(shortfall_total_kg)
        if shortfall <= 0:
            raise ValidationError("减产数量必须为正")
        with self.store.transaction():
            c = self.store.visible_commitment(commitment_id, opened_by)
            event = self.store.fm_events.get(fm_event_id)
            if event is None:
                raise NotFoundError(f"不可抗事件 {fm_event_id} 不存在")
            if not event.confirmed:
                raise ValidationError("不可抗事件证据尚未复核确认")
            floor_qty = _agreed(c).floor_quantity_kg
            if shortfall > floor_qty:
                raise ValidationError("申报减产超过保底数量")
            n = Negotiation(
                id=negotiation_id,
                commitment_id=commitment_id,
                fm_event_id=fm_event_id,
                opened_at=_now(),
                opened_by=opened_by.id,
                evidence_refs=list(evidence_refs),
                shortfall_total_kg=shortfall,
                status=NegotiationStatus.EVIDENCE_REVIEWED,
            )
            self.store.save_negotiation(n)
            return n

    def resolve_negotiation(
        self,
        negotiation_id: str,
        fm_quantity_kg,
        nonperformance_quantity_kg,
        resolved_by: Party,
        farmer_agreed: bool,
        coop_witness: str = "",
        advance_recovery_amount=ZERO,
        advance_relief_amount=ZERO,
        cost_share_amount=ZERO,
        resolution_note: str = "",
    ) -> Negotiation:
        """记录协商结论：不可抗部分与未履约部分必须分别入账且合计等于减产总量。

        - 不可抗部分：对应预付款可协商减免（advance_relief），互不追偿；
        - 未履约部分：对应预付款由农户退还（advance_recovery），进入总账挂账；
        - 工坊另行分担的损失记 cost_share（计入农户应收）。
        """

        fm_q = _q(fm_quantity_kg)
        np_q = _q(nonperformance_quantity_kg)
        if fm_q < 0 or np_q < 0:
            raise ValidationError("数量不能为负")
        if resolved_by.type.value != "coop":
            raise PermissionDenied("减产协商结论必须由合作社见证并记录")
        with self.store.transaction():
            n = self.store.negotiations.get(negotiation_id)
            if n is None:
                raise NotFoundError(f"协商 {negotiation_id} 不存在")
            if n.status not in (NegotiationStatus.OPEN, NegotiationStatus.EVIDENCE_REVIEWED):
                raise ConflictError("协商已结束")
            if fm_q + np_q != n.shortfall_total_kg:
                raise ValidationError(
                    f"不可抗 {fm_q}kg + 未履约 {np_q}kg 必须等于减产总量 {n.shortfall_total_kg}kg"
                )
            if not farmer_agreed:
                n.status = NegotiationStatus.REJECTED
                n.resolution_note = resolution_note or "农户未同意"
                n.resolved_at = _now()
                n.resolved_by = resolved_by.id
                self.store.save_negotiation(n)
                return n
            c = self.store.get_commitment(n.commitment_id)
            # 预付款恢复 + 减免之和不得超过已付预付款
            paid = sum((a.amount for a in self.store.advances_for(c.id)), ZERO)
            recovery, relief = _q(advance_recovery_amount), _q(advance_relief_amount)
            if recovery + relief > paid:
                raise ValidationError(
                    f"退还 {recovery} + 减免 {relief} 超过已付预付款 {paid}"
                )
            n.fm_quantity_kg = fm_q
            n.nonperformance_quantity_kg = np_q
            n.advance_recovery_amount = recovery
            n.advance_relief_amount = relief
            n.cost_share_amount = _q(cost_share_amount)
            n.resolution_note = resolution_note
            n.farmer_agreed = True
            n.coop_witness = coop_witness
            n.status = NegotiationStatus.AGREED
            n.resolved_at = _now()
            n.resolved_by = resolved_by.id
            self.store.save_negotiation(n)
            return n

    # ================================================================
    # 总账复算（确定性；预付款、尾款、退货、补差、申诉调整）
    # ================================================================

    def compute_settlement(
        self, commitment_id: str, viewer: Party, as_of: Optional[date] = None
    ) -> Settlement:
        with self.store.transaction():
            c = self.store.visible_commitment(commitment_id, viewer)
            agreed = _agreed(c)
            terms = agreed.terms
            price_by_grade = {t.grade: t.price_per_kg for t in terms.grade_prices}

            accepted_kg = ZERO
            alternate_kg = ZERO
            returned_kg = ZERO
            graded_payable = ZERO
            deductions_total = ZERO
            last_accept_date: Optional[date] = None
            detail_lines: list[dict] = []

            for delivery in self.store.deliveries_for(commitment_id):
                # 有效检验：取该交付最新一条未被更正的记录
                gradings = sorted(
                    (g for g in self.store.gradings_for(delivery.id)
                     if g.status == GradingStatus.GRADED),
                    key=lambda g: g.graded_at,
                )
                effective_grade = gradings[-1] if gradings else None
                for d in self.store.dispositions_for(delivery.id):
                    line = {
                        "delivery_id": delivery.id,
                        "station_id": delivery.station_id,
                        "kind": d.kind.value,
                        "quantity_kg": str(d.quantity_kg),
                        "deductions": [to_dict(x) for x in d.deductions],
                    }
                    deductions_total += d.deduction_total
                    if d.kind == DispositionKind.ACCEPT:
                        grade = effective_grade.grade if effective_grade else d.grade
                        unit = price_by_grade.get(grade) or _grade_price(terms, grade).price_per_kg
                        line["grade"] = grade
                        line["unit_price"] = str(unit)
                        line["amount"] = str(unit * d.quantity_kg)
                        graded_payable += unit * d.quantity_kg
                        accepted_kg += d.quantity_kg
                        day = delivery.weighed_at.date()
                        if last_accept_date is None or day > last_accept_date:
                            last_accept_date = day
                    elif d.kind == DispositionKind.RETURN:
                        returned_kg += d.quantity_kg
                        line["amount"] = "0"
                    else:
                        alternate_kg += d.quantity_kg
                        # 替代去向：工坊只按条款收代办费，不计货款；
                        # 代办费为负向行（从农户款中扣除），已在 deductions 体现或单独记录
                        fee = terms.alternate_handling_fee_per_kg * d.quantity_kg
                        line["handling_fee"] = str(fee)
                        deductions_total += fee
                    detail_lines.append(line)

            # 保底补差：已接收数量按保底价计的金额低于分级应付不补；
            # 保底体现在“单价不低于保底价”与“达到 floor_quantity 时的总保底”。
            # 规则：分级单价低于保底价的接收部分，按保底价补差。
            floor_topup = ZERO
            for line in detail_lines:
                if line["kind"] != "accept":
                    continue
                qty = Decimal(line["quantity_kg"])
                unit = Decimal(line["unit_price"])
                if unit < terms.floor_price_per_kg:
                    topup = (terms.floor_price_per_kg - unit) * qty
                    line["floor_topup"] = str(topup)
                    floor_topup += topup

            advance_paid = sum((a.amount for a in self.store.advances_for(commitment_id)), ZERO)
            advance_recovery = ZERO
            advance_relief = ZERO
            cost_share = ZERO
            for n in self.store.negotiations_for(commitment_id):
                if n.status == NegotiationStatus.AGREED:
                    advance_recovery += n.advance_recovery_amount
                    advance_relief += n.advance_relief_amount
                    cost_share += n.cost_share_amount

            appeal_adjustments = sum(
                (adj.amount for adj in self.store.adjustments_for(commitment_id)), ZERO
            )

            # 农户应收总额
            gross_receivable = graded_payable + floor_topup - deductions_total + cost_share + appeal_adjustments
            # 最终尾款 = 应收 - 已付预付 + 农户应退未收（recovery 不抵扣，单独挂账催收）
            final_payment = gross_receivable - (advance_paid - advance_relief)

            projected = None
            if last_accept_date is not None:
                projected = last_accept_date + timedelta(days=terms.payment_terms_days)

            settlement = Settlement(
                commitment_id=commitment_id,
                computed_at=_now(),
                accepted_kg=accepted_kg,
                alternate_kg=alternate_kg,
                returned_kg=returned_kg,
                graded_payable=graded_payable,
                deductions_total=deductions_total,
                floor_topup=floor_topup,
                advance_paid=advance_paid,
                advance_recovery=advance_recovery,
                advance_relief=advance_relief,
                cost_share=cost_share,
                appeal_adjustments=appeal_adjustments,
                final_payment=final_payment,
                advance_receivable=advance_recovery,
                payment_state=PaymentState.PROJECTED,
                projected_pay_at=projected,
                detail_lines=detail_lines,
            )
            return settlement

    def close_settlement(self, commitment_id: str, coop: Party) -> Settlement:
        """合作社关账：所有已登记交付必须处置完毕，复算后锁定。"""

        self._require_type(coop, "coop")
        with self.store.transaction():
            c = self.store.get_commitment(commitment_id)
            for d in self.store.deliveries_for(commitment_id):
                used = sum((x.quantity_kg for x in self.store.dispositions_for(d.id)), ZERO)
                if used != d.net_kg:
                    raise ConflictError(
                        f"交付 {d.id} 尚有 {d.net_kg - used}kg 未处置，不能关账"
                    )
            settlement = self.compute_settlement(commitment_id, coop)
            c.settled = True
            self.store.save_commitment(c)
            return settlement

    # ================================================================
    # 申诉：农户对检验/扣减/退货/结算提出，合作社裁决并产生总账调整
    # ================================================================

    def open_appeal(
        self,
        appeal_id: str,
        commitment_id: str,
        farmer: Party,
        target_type: str,
        target_ref: str,
        reason: str,
        evidence_refs: Optional[list[str]] = None,
    ) -> Appeal:
        with self.store.transaction():
            c = self.store.visible_commitment(commitment_id, farmer)
            if farmer.id != c.farmer_id:
                raise PermissionDenied("只有农户可以发起申诉")
            appeal = Appeal(
                id=appeal_id,
                commitment_id=commitment_id,
                raised_by=farmer.id,
                target_type=target_type,
                target_ref=target_ref,
                reason=reason,
                evidence_refs=list(evidence_refs or []),
                raised_at=_now(),
            )
            self.store.save_appeal(appeal)
            return appeal

    def resolve_appeal(
        self,
        appeal_id: str,
        coop: Party,
        uphold: str,
        resolution_note: str,
        adjustment_amount=ZERO,
    ) -> Appeal:
        """合作社裁决。upheld/partially_upheld 且金额非零时生成总账调整，下次复算自动入账。"""

        self._require_type(coop, "coop")
        status_map = {
            "upheld": AppealStatus.UPHELD,
            "partially_upheld": AppealStatus.PARTIALLY_UPHELD,
            "rejected": AppealStatus.REJECTED,
        }
        if uphold not in status_map:
            raise ValidationError("裁决结果必须是 upheld / partially_upheld / rejected")
        with self.store.transaction():
            appeal = self.store.appeals.get(appeal_id)
            if appeal is None:
                raise NotFoundError(f"申诉 {appeal_id} 不存在")
            if appeal.status not in (AppealStatus.OPEN,):
                raise ConflictError("申诉已裁决")
            amount = _q(adjustment_amount)
            if uphold != "rejected" and amount != 0:
                adj = LedgerAdjustment(
                    id=self.store.next_id("adj"),
                    commitment_id=appeal.commitment_id,
                    appeal_id=appeal.id,
                    amount=amount,
                    reason=resolution_note,
                    created_at=_now(),
                    created_by=coop.id,
                )
                self.store.save_adjustment(adj)
                appeal.adjustment_id = adj.id
            appeal.status = status_map[uphold]
            appeal.resolution_note = resolution_note
            appeal.resolved_at = _now()
            appeal.resolved_by = coop.id
            self.store.save_appeal(appeal)
            return appeal

    # ================================================================
    # 农户视图：每次交付后立即可见数量、等级、扣减理由、预计付款日
    # ================================================================

    def delivery_statement(self, delivery_id: str, viewer: Party) -> dict:
        with self.store.transaction():
            delivery = self.store.get_delivery(delivery_id)
            c = self.store.visible_commitment(delivery.commitment_id, viewer)
            terms = _agreed(c).terms
            gradings = sorted(
                (g for g in self.store.gradings_for(delivery_id)
                 if g.status == GradingStatus.GRADED),
                key=lambda g: g.graded_at,
            )
            latest = gradings[-1] if gradings else None
            dispositions = []
            for d in self.store.dispositions_for(delivery_id):
                dispositions.append({
                    "id": d.id,
                    "kind": d.kind.value,
                    "quantity_kg": str(d.quantity_kg),
                    "grade": d.grade,
                    "deductions": [
                        {"reason_code": x.reason_code, "amount": str(x.amount), "note": x.note}
                        for x in d.deductions
                    ],
                    "alternate_destination": d.alternate_destination,
                    "note": d.note,
                })
            projected = delivery.weighed_at.date() + timedelta(days=terms.payment_terms_days)
            return {
                "delivery_id": delivery.id,
                "batch_id": delivery.batch_id,
                "station_id": delivery.station_id,
                "ticket_no": delivery.ticket_no,
                "weighed_at": delivery.weighed_at.isoformat(),
                "gross_kg": str(delivery.gross_kg),
                "tare_kg": str(delivery.tare_kg),
                "net_kg": str(delivery.net_kg),
                "status": delivery.status.value,
                "grade": None if latest is None else {
                    "grade": latest.grade,
                    "moisture_pct": str(latest.moisture_pct),
                    "impurity_pct": str(latest.impurity_pct),
                    "is_lab_result": latest.is_lab_result,
                    "note": latest.note,
                },
                "dispositions": dispositions,
                "projected_pay_at": projected.isoformat(),
            }

    # ================================================================
    # 工具
    # ================================================================

    @staticmethod
    def _require_type(party: Party, type_value: str) -> None:
        if party.type.value != type_value:
            raise PermissionDenied(f"该操作要求身份 {type_value}")

    @staticmethod
    def _validate_terms(terms: CommitmentTerms) -> None:
        if terms.delivery_window.end < terms.delivery_window.start:
            raise ValidationError("交付窗口结束日早于开始日")
        if terms.payment_terms_days < 0:
            raise ValidationError("付款账期不能为负")
        if not terms.grade_prices:
            raise ValidationError("至少要有一个分级价格")
        grades = [t.grade for t in terms.grade_prices]
        if len(grades) != len(set(grades)):
            raise ValidationError("分级价格表中等级重复")
        for t in terms.grade_prices:
            if t.price_per_kg < 0:
                raise ValidationError("分级单价不能为负")
        if terms.prepayment_amount < 0 or terms.alternate_handling_fee_per_kg < 0:
            raise ValidationError("金额不能为负")


def _enum(value, enum_cls):
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(value)
    except ValueError:
        raise ValidationError(f"非法枚举值 {value!r}，允许：{[e.value for e in enum_cls]}") from None
