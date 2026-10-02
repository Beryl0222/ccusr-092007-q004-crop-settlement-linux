"""访问控制：工坊只能查看自己的采购关系。"""

from __future__ import annotations

import unittest
from decimal import Decimal

from support import BATCH_SESAME, C1, C2, COOP, F1, F2, W1, W2, at, make_service, weigh

from crop_settlement.domain import QualityMetrics
from crop_settlement.errors import AccessDenied

G2 = QualityMetrics(moisture_pct=Decimal("8.5"), impurity_pct=Decimal("1.5"))


class AccessTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()
        weigh(self.service, BATCH_SESAME, [("WGH-1", "S-1", "100")])
        self.stmt = self.service.accept_delivery(
            W1, "ACC-1", BATCH_SESAME, "S-1", Decimal("100"), at(21), field_metrics=G2
        )

    def test_workshop_lists_only_own_commitments(self):
        self.assertEqual([c.commitment_id for c in self.service.list_commitments(W1)], [C1])
        self.assertEqual([c.commitment_id for c in self.service.list_commitments(W2)], [C2])
        self.assertEqual(len(self.service.list_commitments(COOP)), 2)

    def test_workshop_cannot_read_other_workshops_commitment(self):
        with self.assertRaises(AccessDenied):
            self.service.get_commitment(W1, C2)
        with self.assertRaises(AccessDenied):
            self.service.recompute_ledger(W1, C2)
        with self.assertRaises(AccessDenied):
            self.service.list_statements(W1, C2)

    def test_workshop_cannot_receive_for_other_workshop(self):
        from support import BATCH_PEANUT

        with self.assertRaises(AccessDenied):
            self.service.accept_delivery(
                W1, "ACC-9", BATCH_PEANUT, "S-1", Decimal("10"), at(21), field_metrics=G2
            )

    def test_farmer_sees_only_own_records(self):
        self.assertEqual([c.commitment_id for c in self.service.list_commitments(F1)], [C1])
        with self.assertRaises(AccessDenied):
            self.service.get_commitment(F1, C2)
        # 农户可以看自己的结算单
        stmt = self.service.get_statement(F1, self.stmt.statement_id)
        self.assertEqual(stmt.farmer_id, "F-01")
        # 其他农户不能看
        with self.assertRaises(AccessDenied):
            self.service.get_statement(F2, self.stmt.statement_id)

    def test_farmer_cannot_register_or_grade(self):
        from crop_settlement.domain import LabReport

        with self.assertRaises(AccessDenied):
            self.service.record_lab_report(
                F1,
                LabReport(
                    report_id="LAB-X",
                    batch_id=BATCH_SESAME,
                    sample_id="SMP-X",
                    metrics=G2,
                    issued_at=at(22),
                ),
            )


if __name__ == "__main__":
    unittest.main()
