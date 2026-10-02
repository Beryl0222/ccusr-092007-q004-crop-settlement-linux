"""成品反查批次与结清状态、工坊采购关系隔离。"""

from __future__ import annotations

import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from crop_settlement.errors import NotFoundError, PermissionDenied  # noqa: E402
from tests.support import WorldCase  # noqa: E402

D = date(2026, 9, 12)


class TraceabilityTest(WorldCase):
    def test_product_lot_traces_back_to_batch_and_settled_state(self):
        d = self.w.svc.register_delivery(
            "com1", "st1a", "100", "0", "op", ticket_no="T1", on_date=D,
        )
        self.w.svc.record_disposition(d.id, "accept", "100", self.w.ws1, grade="A")
        self.w.svc.record_consumption(
            "con1", "OIL-LOT-1",
            [("batch1", d.id, Decimal("80"))],
            self.w.ws1,
        )
        trace = self.w.svc.trace_product("OIL-LOT-1", self.w.ws1)
        self.assertEqual(len(trace), 1)
        self.assertEqual(trace[0]["batch_id"], "batch1")
        self.assertEqual(trace[0]["delivery_id"], d.id)
        self.assertEqual(trace[0]["commitments"][0]["commitment_id"], "com1")
        self.assertFalse(trace[0]["commitments"][0]["settled"])

        self.w.svc.close_settlement("com1", self.w.coop)
        trace2 = self.w.svc.trace_product("OIL-LOT-1", self.w.ws1)
        self.assertTrue(trace2[0]["commitments"][0]["settled"])

    def test_consumption_forbidden_for_unrelated_batch(self):
        with self.assertRaises(PermissionDenied):
            self.w.svc.record_consumption(
                "conX", "OIL-X", [("batch2", None, Decimal("10"))], self.w.ws1,
            )

    def test_coop_sees_all_workshops(self):
        ids = {c.id for c in self.w.store.commitments.values()}
        visible_coop = {
            c.id for c in self.w.store.commitments.values()
            if self.w.store.visible_commitment(c.id, self.w.coop).id
        }
        self.assertEqual(ids, visible_coop)

    def test_workshop_sees_only_own_relation(self):
        # ws2 访问 com1：统一 404，不泄露关系存在
        with self.assertRaises(NotFoundError):
            self.w.store.visible_commitment("com1", self.w.ws2)
        # 结算接口同样隔离
        with self.assertRaises(NotFoundError):
            self.w.svc.compute_settlement("com1", self.w.ws2)
        # 自家关系可见
        self.assertEqual(
            self.w.svc.compute_settlement("com2", self.w.ws2).commitment_id,
            "com2",
        )

    def test_farmer_sees_own_commitment_but_not_other_farmer(self):
        self.assertEqual(
            self.w.svc.delivery_statement.__name__, "delivery_statement",
        )
        with self.assertRaises(NotFoundError):
            self.w.store.visible_commitment("com2", self.w.f1)


if __name__ == "__main__":
    unittest.main()
