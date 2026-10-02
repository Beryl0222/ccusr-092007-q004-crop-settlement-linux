"""结算单内容、浮动分级、保底价与实验室更正补差。"""

from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from support import BATCH_SESAME, C1, COOP, W1, at, make_service, weigh

from crop_settlement.domain import LabReport, QualityMetrics, SampleRecord
from crop_settlement.domain import Grade

G2 = QualityMetrics(moisture_pct=Decimal("8.5"), impurity_pct=Decimal("1.5"))
WET = QualityMetrics(moisture_pct=Decimal("9.5"), impurity_pct=Decimal("1.5"))  # 超水分阈值 0.5
G1_LAB = QualityMetrics(moisture_pct=Decimal("7.8"), impurity_pct=Decimal("0.9"))


class StatementTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()
        weigh(self.service, BATCH_SESAME, [("WGH-1", "S-1", "600")])

    def test_statement_shows_quantity_grade_deduction_and_pay_date(self):
        stmt = self.service.accept_delivery(
            W1, "ACC-1", BATCH_SESAME, "S-1", Decimal("200"), at(21), field_metrics=WET
        )
        self.assertEqual(stmt.grade, Grade.G3)
        self.assertEqual(stmt.unit_price, Decimal("12.40"))
        # 水分超阈值 0.5 个点，扣重 0.5%：200kg -> 1kg
        self.assertEqual(len(stmt.deductions), 1)
        self.assertEqual(stmt.deductions[0].reason.value, "moisture_over")
        self.assertIn("水分", stmt.deductions[0].label)
        self.assertEqual(stmt.deductions[0].weight_kg, Decimal("1.000"))
        self.assertEqual(stmt.payable_quantity_kg, Decimal("199.000"))
        self.assertEqual(stmt.net_amount, Decimal("2467.60"))  # 199 x 12.40
        self.assertEqual(stmt.expected_payment_date, date(2026, 9, 28))  # 接收后 7 天

    def test_floor_price_holds_without_market_reference(self):
        low = QualityMetrics(moisture_pct=Decimal("9.8"), impurity_pct=Decimal("2.5"))  # G3
        stmt = self.service.accept_delivery(
            W1, "ACC-1", BATCH_SESAME, "S-1", Decimal("100"), at(21), field_metrics=low
        )
        self.assertEqual(stmt.grade, Grade.G3)
        # 没有市场参考价时向下浮动不生效，农户至少拿保底价
        self.assertEqual(stmt.unit_price, Decimal("12.40"))

    def test_market_reference_floats_price_up(self):
        stmt = self.service.accept_delivery(
            W1,
            "ACC-1",
            BATCH_SESAME,
            "S-1",
            Decimal("100"),
            at(21),
            field_metrics=G2,
            market_reference_per_kg=Decimal("13.00"),
        )
        self.assertEqual(stmt.unit_price, Decimal("13.00"))
        stmt_g1 = self.service.accept_delivery(
            W1,
            "ACC-2",
            BATCH_SESAME,
            "S-1",
            Decimal("100"),
            at(21),
            field_metrics=QualityMetrics(moisture_pct=Decimal("7.5"), impurity_pct=Decimal("0.8")),
            market_reference_per_kg=Decimal("13.00"),
        )
        self.assertEqual(stmt_g1.unit_price, Decimal("13.60"))  # 13.00 + 0.60

    def test_lab_correction_regrades_and_books_adjustment(self):
        stmt = self.service.accept_delivery(
            W1, "ACC-1", BATCH_SESAME, "S-1", Decimal("200"), at(21), field_metrics=G2
        )
        self.assertEqual(stmt.grade, Grade.G2)
        self.assertEqual(stmt.net_amount, Decimal("2480.00"))

        self.service.record_sample(
            COOP, SampleRecord(sample_id="SMP-1", batch_id=BATCH_SESAME, site_id="S-1", taken_by="质检员", at=at(21))
        )
        self.service.record_lab_report(
            COOP,
            LabReport(
                report_id="LAB-1",
                batch_id=BATCH_SESAME,
                sample_id="SMP-1",
                metrics=G1_LAB,
                issued_at=at(22),
            ),
        )
        latest = self.service.get_statement(COOP, "STMT-ACC-1-r2")
        self.assertEqual(latest.grade, Grade.G1)
        self.assertEqual(latest.unit_price, Decimal("13.00"))  # 保底 12.40 + 0.60
        self.assertEqual(latest.net_amount, Decimal("2600.00"))
        self.assertEqual(latest.supersedes, "STMT-ACC-1-r1")

        ledger = self.service.recompute_ledger(COOP, C1)
        self.assertEqual(ledger.adjustments, Decimal("120.00"))  # 200kg x 0.60 补差
        self.assertEqual(ledger.balance_due, Decimal("2480.00") + Decimal("120.00") - Decimal("3000.00"))

    def test_lab_correction_keeps_original_batch_and_history(self):
        self.service.accept_delivery(
            W1, "ACC-1", BATCH_SESAME, "S-1", Decimal("100"), at(21), field_metrics=G2
        )
        self.service.record_sample(
            COOP, SampleRecord(sample_id="SMP-1", batch_id=BATCH_SESAME, site_id="S-1", taken_by="质检员", at=at(21))
        )
        self.service.record_lab_report(
            COOP,
            LabReport(report_id="LAB-1", batch_id=BATCH_SESAME, sample_id="SMP-1", metrics=G1_LAB, issued_at=at(22)),
        )
        # 实验室再次更正，仍沿用原批次
        self.service.record_lab_report(
            COOP,
            LabReport(
                report_id="LAB-2",
                batch_id=BATCH_SESAME,
                sample_id="SMP-1",
                metrics=G2,
                issued_at=at(23),
                corrects="LAB-1",
            ),
        )
        r1 = self.service.get_statement(COOP, "STMT-ACC-1-r1")
        r2 = self.service.get_statement(COOP, "STMT-ACC-1-r2")
        r3 = self.service.get_statement(COOP, "STMT-ACC-1-r3")
        self.assertEqual((r1.grade, r2.grade, r3.grade), (Grade.G2, Grade.G1, Grade.G2))
        self.assertTrue(all(s.batch_id == BATCH_SESAME for s in (r1, r2, r3)))
        ledger = self.service.recompute_ledger(COOP, C1)
        # 先补 +60 再冲回 -60，净补差为 0，历史完整保留
        self.assertEqual(ledger.adjustments, Decimal("0.00"))
        adjustments = [e for e in ledger.events if e.kind.value == "adjustment"]
        self.assertEqual(len(adjustments), 2)


if __name__ == "__main__":
    unittest.main()
