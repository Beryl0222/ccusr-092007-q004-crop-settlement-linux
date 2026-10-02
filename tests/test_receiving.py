"""收货流程：称重累计、部分接收、并发不突破实收、退货与幂等。"""

from __future__ import annotations

import threading
import unittest
from decimal import Decimal

from support import BATCH_SESAME, C1, COOP, T0, W1, at, make_service, weigh

from crop_settlement.domain import QualityMetrics
from crop_settlement.errors import (
    OutsideDeliveryWindow,
    OverReceiveError,
    QuantityExceeded,
)

G2 = QualityMetrics(moisture_pct=Decimal("8.5"), impurity_pct=Decimal("1.5"))


class ReceivingTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()

    def test_weighing_accumulates_across_sites(self):
        weigh(self.service, BATCH_SESAME, [("WGH-1", "S-1", "500"), ("WGH-2", "S-2", "300")])
        stmt1 = self.service.accept_delivery(
            W1, "ACC-1", BATCH_SESAME, "S-1", Decimal("400"), at(21), field_metrics=G2
        )
        stmt2 = self.service.accept_delivery(
            W1, "ACC-2", BATCH_SESAME, "S-2", Decimal("300"), at(21), field_metrics=G2
        )
        self.assertEqual(stmt1.quantity_kg, Decimal("400.000"))
        self.assertEqual(stmt2.quantity_kg, Decimal("300.000"))
        with self.assertRaises(OverReceiveError):
            self.service.accept_delivery(
                W1, "ACC-3", BATCH_SESAME, "S-1", Decimal("100.001"), at(21), field_metrics=G2
            )

    def test_concurrent_sites_never_exceed_weighed_quantity(self):
        weigh(self.service, BATCH_SESAME, [("WGH-1", "S-1", "500")])
        outcomes = []
        lock = threading.Lock()

        def attempt(site: str, n: int):
            try:
                self.service.accept_delivery(
                    W1, f"ACC-{site}-{n}", BATCH_SESAME, site, Decimal("100"), at(21), field_metrics=G2
                )
                outcome = "ok"
            except OverReceiveError:
                outcome = "full"
            with lock:
                outcomes.append(outcome)

        threads = [
            threading.Thread(target=attempt, args=(site, n))
            for site in ("S-1", "S-2")
            for n in range(5)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(outcomes.count("ok"), 5)
        self.assertEqual(outcomes.count("full"), 5)
        ledger = self.service.recompute_ledger(COOP, C1)
        self.assertEqual(ledger.final_payables, Decimal("6200.00"))  # 500kg x 12.40

    def test_acceptance_is_idempotent_under_retry(self):
        weigh(self.service, BATCH_SESAME, [("WGH-1", "S-1", "200")])
        first = self.service.accept_delivery(
            W1, "ACC-1", BATCH_SESAME, "S-1", Decimal("200"), at(21), field_metrics=G2
        )
        second = self.service.accept_delivery(
            W1, "ACC-1", BATCH_SESAME, "S-1", Decimal("200"), at(21), field_metrics=G2
        )
        self.assertEqual(first.statement_id, second.statement_id)
        ledger = self.service.recompute_ledger(COOP, C1)
        finals = [e for e in ledger.events if e.kind.value == "final"]
        self.assertEqual(len(finals), 1)

    def test_delivery_outside_window_rejected(self):
        weigh(self.service, BATCH_SESAME, [("WGH-1", "S-1", "100")])
        with self.assertRaises(OutsideDeliveryWindow):
            self.service.accept_delivery(
                W1, "ACC-1", BATCH_SESAME, "S-1", Decimal("50"), at(11), field_metrics=G2
            )

    def test_acceptance_capped_by_committed_quantity(self):
        weigh(self.service, BATCH_SESAME, [("WGH-1", "S-1", "900")])
        self.service.accept_delivery(
            W1, "ACC-1", BATCH_SESAME, "S-1", Decimal("800"), at(21), field_metrics=G2
        )
        with self.assertRaises(QuantityExceeded):
            self.service.accept_delivery(
                W1, "ACC-2", BATCH_SESAME, "S-1", Decimal("1"), at(21), field_metrics=G2
            )

    def test_return_reduces_accepted_and_books_negative_entry(self):
        weigh(self.service, BATCH_SESAME, [("WGH-1", "S-1", "300")])
        self.service.accept_delivery(
            W1, "ACC-1", BATCH_SESAME, "S-1", Decimal("300"), at(21), field_metrics=G2
        )
        self.service.record_return(W1, "RET-1", "ACC-1", "S-1", Decimal("50"), "霉变", at(22))
        ledger = self.service.recompute_ledger(COOP, C1)
        self.assertEqual(ledger.final_payables, Decimal("3720.00"))
        self.assertEqual(ledger.returns, Decimal("620.00"))  # 50kg x 12.40
        # 退货释放的额度可以再次接收
        self.service.accept_delivery(
            W1, "ACC-2", BATCH_SESAME, "S-1", Decimal("50"), at(23), field_metrics=G2
        )
        with self.assertRaises(QuantityExceeded):
            self.service.record_return(W1, "RET-2", "ACC-1", "S-1", Decimal("251"), "超退", at(23))

    def test_return_retry_is_idempotent(self):
        weigh(self.service, BATCH_SESAME, [("WGH-1", "S-1", "100")])
        self.service.accept_delivery(
            W1, "ACC-1", BATCH_SESAME, "S-1", Decimal("100"), at(21), field_metrics=G2
        )
        self.service.record_return(W1, "RET-1", "ACC-1", "S-1", Decimal("40"), "雨淋", at(22))
        self.service.record_return(W1, "RET-1", "ACC-1", "S-1", Decimal("40"), "雨淋", at(22))
        ledger = self.service.recompute_ledger(COOP, C1)
        self.assertEqual(ledger.returns, Decimal("496.00"))


if __name__ == "__main__":
    unittest.main()
