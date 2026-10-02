"""洪涝/干旱减产的有证据协商，以及总账复算与申诉调整。"""

from __future__ import annotations

import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from crop_settlement.errors import ConflictError, PermissionDenied, ValidationError  # noqa: E402
from crop_settlement.models import (  # noqa: E402
    AppealStatus,
    Deduction,
    NegotiationStatus,
)
from tests.support import WorldCase  # noqa: E402

D = date(2026, 9, 12)


def _accept(svc, delivery_id, qty, grade, ws, deductions=None):
    return svc.record_disposition(
        delivery_id, "accept", qty, ws, grade=grade, deductions=deductions or [],
    )


class NegotiationTest(WorldCase):
    def _event(self):
        return self.w.svc.record_fm_event(
            "fm1", "flood", "成熟期连续降雨", date(2026, 9, 5), "东庄",
            evidence_refs=["evidence/weather.pdf"],
            recorded_by="coop",
            ended_at=date(2026, 9, 8),
        )

    def test_fm_event_requires_evidence(self):
        with self.assertRaises(ValidationError):
            self.w.svc.record_fm_event(
                "fmX", "flood", "雨", date(2026, 9, 5), "x", evidence_refs=[],
                recorded_by="coop",
            )

    def test_negotiation_requires_evidence_and_splits_must_sum_to_shortfall(self):
        self._event()
        with self.assertRaises(ValidationError):
            self.w.svc.open_negotiation(
                "nX", "com1", "fm1", self.w.f1, evidence_refs=[],
                shortfall_total_kg="100",
            )
        self.w.svc.open_negotiation(
            "n1", "com1", "fm1", self.w.f1,
            evidence_refs=["evidence/yield.jpg"], shortfall_total_kg="200",
        )
        with self.assertRaises(ValidationError):
            # 170 + 40 != 200
            self.w.svc.resolve_negotiation(
                "n1", "170", "40", self.w.coop, farmer_agreed=True,
            )

    def test_fm_and_nonperformance_recorded_separately(self):
        self.w.svc.pay_advance("com1", "5000", self.w.ws1)
        self._event()
        self.w.svc.open_negotiation(
            "n1", "com1", "fm1", self.w.f1,
            evidence_refs=["evidence/yield.jpg"], shortfall_total_kg="200",
        )
        n = self.w.svc.resolve_negotiation(
            "n1", fm_quantity_kg="170", nonperformance_quantity_kg="30",
            resolved_by=self.w.coop, farmer_agreed=True,
            advance_recovery_amount="150", advance_relief_amount="850",
            cost_share_amount="200",
        )
        self.assertEqual(n.status, NegotiationStatus.AGREED)
        self.assertEqual(n.fm_quantity_kg, Decimal("170"))
        self.assertEqual(n.nonperformance_quantity_kg, Decimal("30"))

        s = self.w.svc.compute_settlement("com1", self.w.coop)
        self.assertEqual(s.advance_paid, Decimal("5000"))
        self.assertEqual(s.advance_relief, Decimal("850"))
        self.assertEqual(s.advance_recovery, Decimal("150"))
        self.assertEqual(s.cost_share, Decimal("200"))
        # 未履约应退预付款单独挂账，不从尾款抵扣
        self.assertEqual(s.advance_receivable, Decimal("150"))
        # 无交付时：应收 = 分担 200；净预付 4150；尾款 -3950
        # （减免 850 已不追回，另 150 单独挂账）
        self.assertEqual(s.final_payment, Decimal("200") - Decimal("4150"))

    def test_recovery_plus_relief_cannot_exceed_advance_paid(self):
        self.w.svc.pay_advance("com1", "5000", self.w.ws1)
        self._event()
        self.w.svc.open_negotiation(
            "n1", "com1", "fm1", self.w.f1,
            evidence_refs=["e"], shortfall_total_kg="200",
        )
        with self.assertRaises(ValidationError):
            self.w.svc.resolve_negotiation(
                "n1", "100", "100", self.w.coop, farmer_agreed=True,
                advance_recovery_amount="3000", advance_relief_amount="3000",
            )

    def test_farmer_rejecting_leads_to_rejected_status(self):
        self._event()
        self.w.svc.open_negotiation(
            "n1", "com1", "fm1", self.w.f1,
            evidence_refs=["e"], shortfall_total_kg="10",
        )
        n = self.w.svc.resolve_negotiation(
            "n1", "0", "10", self.w.coop, farmer_agreed=False,
        )
        self.assertEqual(n.status, NegotiationStatus.REJECTED)
        # 已结束的协商不能再次裁决
        with self.assertRaises(ConflictError):
            self.w.svc.resolve_negotiation(
                "n1", "0", "10", self.w.coop, farmer_agreed=True,
            )

    def test_drought_event_type_supported(self):
        self.w.svc.record_fm_event(
            "fmD", "drought", "伏旱", date(2026, 7, 1), "x",
            evidence_refs=["evidence/drought.pdf"], recorded_by="coop",
        )
        self.assertEqual(self.w.store.fm_events["fmD"].type.value, "drought")


class SettlementTest(WorldCase):
    def test_full_ledger_recompute_is_deterministic(self):
        # 预付 5000
        self.w.svc.pay_advance("com1", "5000", self.w.ws1)
        # 交付：净重 600，接收 550(B) + 退货 50
        d = self.w.svc.register_delivery(
            "com1", "st1a", "620", "20", "op", ticket_no="T1", on_date=D,
        )
        _accept(self.w.svc, d.id, "550", "B", self.w.ws1,
                deductions=[Deduction("moisture", Decimal("40"), "烘干")])
        self.w.svc.record_disposition(d.id, "return", "50", self.w.ws1)

        s1 = self.w.svc.compute_settlement("com1", self.w.coop)
        s2 = self.w.svc.compute_settlement("com1", self.w.coop)
        # 复算确定性（computed_at 外的所有金额/数量一致）
        for attr in ("accepted_kg", "returned_kg", "graded_payable",
                     "deductions_total", "floor_topup", "advance_paid",
                     "final_payment", "advance_receivable"):
            self.assertEqual(getattr(s1, attr), getattr(s2, attr))

        # 550*13 = 7150；扣 40；净预付 5000；尾款 = 7150-40-5000 = 2110
        self.assertEqual(s1.graded_payable, Decimal("7150"))
        self.assertEqual(s1.deductions_total, Decimal("40"))
        self.assertEqual(s1.final_payment, Decimal("2110"))
        self.assertEqual(s1.projected_pay_at, date(2026, 9, 19))  # 9/12+7
        self.assertEqual(s1.accepted_kg, Decimal("550"))
        self.assertEqual(s1.returned_kg, Decimal("50"))

    def test_floor_topup_when_grade_price_below_floor(self):
        d = self.w.svc.register_delivery(
            "com1", "st1a", "100", "0", "op", ticket_no="T2", on_date=D,
        )
        # C 级 10 < 保底价 12，保底补差 (12-10)*100 = 200
        _accept(self.w.svc, d.id, "100", "C", self.w.ws1)
        s = self.w.svc.compute_settlement("com1", self.w.coop)
        self.assertEqual(s.floor_topup, Decimal("200"))
        self.assertEqual(s.final_payment, Decimal("1000") + Decimal("200"))

    def test_close_requires_all_dispositions_balanced(self):
        d = self.w.svc.register_delivery(
            "com1", "st1a", "100", "0", "op", ticket_no="T3", on_date=D,
        )
        _accept(self.w.svc, d.id, "80", "A", self.w.ws1)
        with self.assertRaises(ConflictError):
            self.w.svc.close_settlement("com1", self.w.coop)
        self.w.svc.record_disposition(d.id, "return", "20", self.w.ws1)
        s = self.w.svc.close_settlement("com1", self.w.coop)
        self.assertTrue(self.w.store.get_commitment("com1").settled)
        self.assertEqual(s.accepted_kg, Decimal("80"))

    def test_only_coop_can_close(self):
        with self.assertRaises(PermissionDenied):
            self.w.svc.close_settlement("com1", self.w.ws1)


class AppealTest(WorldCase):
    def test_appeal_adjustment_enters_next_recompute(self):
        d = self.w.svc.register_delivery(
            "com1", "st1a", "100", "0", "op", ticket_no="T4", on_date=D,
        )
        _accept(self.w.svc, d.id, "100", "B", self.w.ws1,
                deductions=[Deduction("impurity", Decimal("100"), "争议扣杂")])
        before = self.w.svc.compute_settlement("com1", self.w.coop)
        self.assertEqual(before.final_payment, Decimal("1300") - Decimal("100"))

        self.w.svc.open_appeal(
            "ap1", "com1", self.w.f1, "disposition", d.id,
            "扣杂比例过高，封样复检杂质仅 0.6%",
            evidence_refs=["evidence/relab.pdf"],
        )
        self.w.svc.resolve_appeal(
            "ap1", self.w.coop, uphold="upheld",
            resolution_note="复检属实，退还多扣 60 元",
            adjustment_amount="60",
        )
        after = self.w.svc.compute_settlement("com1", self.w.coop)
        self.assertEqual(after.appeal_adjustments, Decimal("60"))
        self.assertEqual(after.final_payment, before.final_payment + Decimal("60"))
        self.assertEqual(
            self.w.store.appeals["ap1"].status, AppealStatus.UPHELD,
        )

    def test_only_farmer_opens_and_only_coop_resolves(self):
        from crop_settlement.errors import PermissionDenied

        with self.assertRaises(PermissionDenied):
            self.w.svc.open_appeal(
                "apX", "com1", self.w.ws1, "disposition", "x", "y",
            )
        self.w.svc.open_appeal(
            "ap2", "com1", self.w.f1, "settlement", "com1", "有异议",
        )
        with self.assertRaises(PermissionDenied):
            self.w.svc.resolve_appeal(
                "ap2", self.w.ws1, "upheld", "x", "1",
            )

    def test_rejected_appeal_has_no_adjustment(self):
        self.w.svc.open_appeal(
            "ap3", "com1", self.w.f1, "grading", "grd-x", "不认可",
        )
        a = self.w.svc.resolve_appeal(
            "ap3", self.w.coop, uphold="rejected",
            resolution_note="检验流程合规",
        )
        self.assertIsNone(a.adjustment_id)
        self.assertEqual(
            self.w.svc.compute_settlement("com1", self.w.coop).appeal_adjustments,
            Decimal("0"),
        )


if __name__ == "__main__":
    unittest.main()
