"""成品追溯：从成品所用原料反查批次与结清状态。"""

from __future__ import annotations

import unittest
from decimal import Decimal

from support import BATCH_SESAME, C1, COOP, F1, W1, W2, at, make_service, weigh

from crop_settlement.domain import IngredientUsage, ProductBatch, QualityMetrics
from crop_settlement.errors import AccessDenied

G2 = QualityMetrics(moisture_pct=Decimal("8.5"), impurity_pct=Decimal("1.5"))


def product(product_id: str = "PROD-1", workshop_id: str = "W-01") -> ProductBatch:
    return ProductBatch(
        product_id=product_id,
        workshop_id=workshop_id,
        produced_at=at(28),
        ingredients=(IngredientUsage(batch_id=BATCH_SESAME, quantity_kg=Decimal("150")),),
    )


class TraceabilityTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()
        weigh(self.service, BATCH_SESAME, [("WGH-1", "S-1", "200")])

    def test_trace_product_back_to_batch_and_settlement(self):
        self.service.accept_delivery(
            W1, "ACC-1", BATCH_SESAME, "S-1", Decimal("200"), at(21), field_metrics=G2
        )
        self.service.register_product_batch(W1, product())
        trace = self.service.trace_product(W1, "PROD-1")
        self.assertEqual(len(trace.ingredients), 1)
        item = trace.ingredients[0]
        self.assertEqual(item.batch_id, BATCH_SESAME)
        self.assertEqual(item.parcel_id, "P-101")
        self.assertEqual(item.farmer_id, "F-01")
        self.assertEqual(item.commitment_id, C1)
        self.assertEqual(item.accepted_quantity_kg, Decimal("200.000"))
        # 尾款 2480 - 预付 3000 = -520，尚未结清
        self.assertEqual(item.settlement_status, "open")
        self.assertEqual(item.balance_due, Decimal("-520.00"))

    def test_settlement_status_transitions(self):
        self.service.register_product_batch(W1, product())
        before = self.service.trace_product(COOP, "PROD-1").ingredients[0]
        self.assertEqual(before.settlement_status, "no_delivery")

        stmt = self.service.accept_delivery(
            W1, "ACC-1", BATCH_SESAME, "S-1", Decimal("200"), at(21), field_metrics=G2
        )
        # 申诉裁补 520，使尾款与预付款恰好相抵
        self.service.file_appeal(F1, "APL-1", stmt.statement_id, "等级争议", at(22))
        self.service.resolve_appeal(
            COOP, "APL-1", uphold=False, note="补差", at=at(23),
            adjustment_amount=Decimal("520.00"),
        )
        after = self.service.trace_product(COOP, "PROD-1").ingredients[0]
        self.assertEqual(after.settlement_status, "settled")
        self.assertEqual(after.balance_due, Decimal("0.00"))

    def test_workshop_can_only_trace_own_products(self):
        self.service.register_product_batch(W1, product())
        with self.assertRaises(AccessDenied):
            self.service.trace_product(W2, "PROD-1")
        with self.assertRaises(AccessDenied):
            self.service.register_product_batch(W1, product("PROD-2", workshop_id="W-02"))

    def test_reverse_lookup_products_using_batch(self):
        self.service.register_product_batch(W1, product())
        self.assertEqual(self.service.products_using_batch(W1, BATCH_SESAME), ["PROD-1"])
        self.assertEqual(self.service.products_using_batch(COOP, BATCH_SESAME), ["PROD-1"])


if __name__ == "__main__":
    unittest.main()
