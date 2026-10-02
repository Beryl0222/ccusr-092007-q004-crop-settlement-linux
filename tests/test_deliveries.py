"""称重、取样、实验室更正、部分接收、退货、替代去向与多站点并发。"""

from __future__ import annotations

import sys
import threading
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from crop_settlement.errors import ConflictError, PermissionDenied, QuantityOverflow, ValidationError  # noqa: E402
from crop_settlement.models import (  # noqa: E402
    Deduction,
    DeliveryStatus,
    DispositionKind,
    GradingStatus,
)
from tests.support import WorldCase  # noqa: E402

D = date(2026, 9, 10)


class DeliveryFlowTest(WorldCase):
    def _delivery(self, station="st1a", gross="600", tare="20", ticket="T1"):
        return self.w.svc.register_delivery(
            "com1", station, gross, tare, "op", ticket_no=ticket, on_date=D,
        )

    def test_window_outside_rejected(self):
        with self.assertRaises(ValidationError):
            self.w.svc.register_delivery(
                "com1", "st1a", "10", "0", "op", on_date=date(2026, 8, 31),
            )

    def test_station_must_belong_to_workshop(self):
        with self.assertRaises(PermissionDenied):
            self.w.svc.register_delivery(
                "com1", "st2", "10", "0", "op", on_date=D,
            )

    def test_ticket_duplicate_rejected(self):
        self._delivery(ticket="DUP")
        with self.assertRaises(ConflictError):
            self._delivery(gross="10", tare="0", ticket="DUP")

    def test_weighing_math_and_statement(self):
        d = self._delivery()
        self.assertEqual(d.net_kg, Decimal("580"))
        stmt = self.w.svc.delivery_statement(d.id, self.w.f1)
        self.assertEqual(stmt["net_kg"], "580")
        self.assertEqual(stmt["projected_pay_at"], "2026-09-17")  # +7 天

    # ------------------------------------------------------- 处置守恒

    def test_dispositions_must_balance_net_weight(self):
        d = self._delivery()
        self.w.svc.record_disposition(
            d.id, "accept", "500", self.w.ws1, grade="B",
            deductions=[Deduction("moisture", Decimal("30"), "烘干")],
        )
        with self.assertRaises(QuantityOverflow):
            self.w.svc.record_disposition(d.id, "accept", "81", self.w.ws1, grade="B")
        # 恰好配平：500 + 50 退货 + 30 替代
        self.w.svc.record_disposition(d.id, "return", "50", self.w.ws1)
        self.w.svc.record_disposition(
            d.id, "alternate", "30", self.w.ws1,
            alternate_destination="王记炒货坊", farmer_consent=True,
        )
        delivery = self.w.store.get_delivery(d.id)
        self.assertEqual(delivery.status, DeliveryStatus.SETTLED_CLOSED)

    def test_partial_accept_status(self):
        d = self._delivery()
        self.w.svc.record_disposition(d.id, "accept", "100", self.w.ws1, grade="A")
        self.assertEqual(
            self.w.store.get_delivery(d.id).status,
            DeliveryStatus.PARTIALLY_ACCEPTED,
        )

    def test_alternate_requires_consent(self):
        d = self._delivery(gross="20", tare="0", ticket="T2")
        with self.assertRaises(PermissionDenied):
            self.w.svc.record_disposition(
                d.id, "alternate", "20", self.w.ws1,
                alternate_destination="x", farmer_consent=False,
            )

    def test_alternate_disallowed_when_terms_forbid(self):
        d = self.w.svc.register_delivery(
            "com2", "st2", "100", "0", "op", on_date=date(2026, 9, 20),
        )
        with self.assertRaises(ValidationError):
            self.w.svc.record_disposition(
                d.id, "alternate", "100", self.w.ws2,
                alternate_destination="x", farmer_consent=True,
            )

    def test_farmer_cannot_record_disposition(self):
        d = self._delivery(gross="10", tare="0", ticket="T3")
        with self.assertRaises(PermissionDenied):
            self.w.svc.record_disposition(d.id, "accept", "10", self.w.f1, grade="A")

    # ------------------------------------------------------- 实验室更正

    def test_lab_correction_keeps_batch_and_supersedes(self):
        d = self._delivery()
        sample = self.w.svc.seal_sample(d.id, "SEAL-1", "检验员")
        g1 = self.w.svc.grade_sample(sample.id, "C", "12", "2", "站点检验员")
        g2 = self.w.svc.correct_grade(g1.id, "A", "8", "0.5", "实验室")
        self.assertEqual(g2.delivery_id, d.id)
        self.assertEqual(g2.sample_id, sample.id)
        self.assertTrue(g2.is_lab_result)
        self.assertEqual(self.w.store.gradings[g1.id].status, GradingStatus.CORRECTED)
        self.assertEqual(self.w.store.gradings[g1.id].superseded_by, g2.id)

        # 结算时以更正后的 A 级计价
        self.w.svc.record_disposition(d.id, "accept", "580", self.w.ws1, grade="A")
        s = self.w.svc.compute_settlement("com1", self.w.coop)
        self.assertEqual(s.graded_payable, Decimal("580") * Decimal("16"))

    # ------------------------------------------------------- 并发不超收

    def test_concurrent_dispositions_cannot_exceed_net_weight(self):
        # 两个站点线程同时对各自交付登记处置；更关键的是对同一交付并发
        d1 = self._delivery(station="st1a", gross="100", tare="0", ticket="C1")
        d2 = self.w.svc.register_delivery(
            "com1", "st1b", "100", "0", "op", ticket_no="C2", on_date=D,
        )
        results: list[Exception] = []

        def hammer(delivery_id):
            try:
                for _ in range(20):
                    self.w.svc.record_disposition(
                        delivery_id, "accept", "10", self.w.ws1, grade="B",
                    )
            except QuantityOverflow:
                results.append("overflow")
            except Exception as exc:  # pragma: no cover
                results.append(exc)

        threads = [threading.Thread(target=hammer, args=(x,)) for x in (d1.id, d2.id)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for delivery in (d1, d2):
            used = sum(
                x.quantity_kg for x in self.w.store.dispositions_for(delivery.id)
            )
            self.assertLessEqual(used, Decimal("100"))
            self.assertEqual(used, Decimal("100"))  # 恰好收满，再无突破
        self.assertIn("overflow", results)

    def test_concurrent_deliveries_do_not_double_count_ticket(self):
        errors = []

        def register(i):
            try:
                self.w.svc.register_delivery(
                    "com1", "st1a", "10", "0", f"op{i}",
                    ticket_no="SAME", on_date=D,
                )
            except ConflictError:
                errors.append("dup")

        threads = [threading.Thread(target=register, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len([d for d in self.w.store.deliveries.values()
                              if d.commitment_id == "com1" and d.ticket_no == "SAME"]), 1)
        self.assertEqual(len(errors), 7)


if __name__ == "__main__":
    unittest.main()
