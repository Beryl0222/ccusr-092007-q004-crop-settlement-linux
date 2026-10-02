"""申诉处理与总账复算。"""

from __future__ import annotations

import unittest
from decimal import Decimal

from support import BATCH_SESAME, C1, COOP, F1, F2, W1, at, make_service, weigh

from crop_settlement.domain import AppealStatus, QualityMetrics
from crop_settlement.errors import AccessDenied, DomainError

WET = QualityMetrics(moisture_pct=Decimal("9.5"), impurity_pct=Decimal("1.5"))


class AppealTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()
        weigh(self.service, BATCH_SESAME, [("WGH-1", "S-1", "200")])
        self.stmt = self.service.accept_delivery(
            W1, "ACC-1", BATCH_SESAME, "S-1", Decimal("200"), at(21), field_metrics=WET
        )

    def test_farmer_files_appeal_on_own_statement(self):
        appeal = self.service.file_appeal(F1, "APL-1", self.stmt.statement_id, "水分扣重有争议", at(22))
        self.assertEqual(appeal.status, AppealStatus.FILED)
        with self.assertRaises(AccessDenied):
            self.service.file_appeal(F2, "APL-2", self.stmt.statement_id, "越权", at(22))

    def test_adjusted_appeal_books_compensation_into_ledger(self):
        self.service.file_appeal(F1, "APL-1", self.stmt.statement_id, "水分扣重有争议", at(22))
        self.service.review_appeal(COOP, "APL-1")
        appeal = self.service.resolve_appeal(
            COOP, "APL-1", uphold=False, note="复测后返还扣重一半", at=at(24),
            adjustment_amount=Decimal("6.20"),
        )
        self.assertEqual(appeal.status, AppealStatus.ADJUSTED)
        ledger = self.service.recompute_ledger(COOP, C1)
        self.assertEqual(ledger.adjustments, Decimal("6.20"))
        # 总账 = 尾款 2467.60 + 补差 6.20 - 预付 3000
        self.assertEqual(ledger.balance_due, Decimal("-526.20"))

    def test_upheld_appeal_leaves_ledger_untouched(self):
        self.service.file_appeal(F1, "APL-1", self.stmt.statement_id, "等级有争议", at(22))
        self.service.resolve_appeal(COOP, "APL-1", uphold=True, note="检验流程合规", at=at(24))
        ledger = self.service.recompute_ledger(COOP, C1)
        self.assertEqual(ledger.adjustments, Decimal("0.00"))

    def test_adjusted_appeal_requires_nonzero_amount(self):
        self.service.file_appeal(F1, "APL-1", self.stmt.statement_id, "扣重有争议", at(22))
        with self.assertRaises(DomainError):
            self.service.resolve_appeal(COOP, "APL-1", uphold=False, note="漏填金额", at=at(24))

    def test_ledger_recomputes_from_events(self):
        """总账四部分（预付款、尾款、退货、补差）与事件逐笔一致。"""
        self.service.record_return(W1, "RET-1", "ACC-1", "S-1", Decimal("20"), "雨淋霉变", at(22))
        self.service.file_appeal(F1, "APL-1", self.stmt.statement_id, "扣重有争议", at(23))
        self.service.resolve_appeal(
            COOP, "APL-1", uphold=False, note="补差", at=at(24), adjustment_amount=Decimal("10.00")
        )
        ledger = self.service.recompute_ledger(COOP, C1)
        prepay = sum(e.amount for e in ledger.events if e.kind.value == "prepayment")
        finals = sum(e.amount for e in ledger.events if e.kind.value == "final")
        returns = sum(e.amount for e in ledger.events if e.kind.value == "return")
        adjustments = sum(e.amount for e in ledger.events if e.kind.value == "adjustment")
        self.assertEqual(ledger.prepayments, -prepay)
        self.assertEqual(ledger.final_payables, finals)
        self.assertEqual(ledger.returns, -returns)
        self.assertEqual(ledger.adjustments, adjustments)
        self.assertEqual(
            ledger.balance_due,
            ledger.final_payables + ledger.adjustments - ledger.prepayments - ledger.returns,
        )


if __name__ == "__main__":
    unittest.main()
