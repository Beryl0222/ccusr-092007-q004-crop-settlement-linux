"""样例加载与替代去向登记。"""

from __future__ import annotations

import unittest
from decimal import Decimal
from pathlib import Path

from support import BATCH_SESAME, C1, COOP, W1, at, make_service

from crop_settlement import load_record, load_supply_record
from crop_settlement.errors import InvalidState

FIXTURE = Path(__file__).parents[1] / "fixtures" / "supply_commitment.json"


class LoaderTest(unittest.TestCase):
    def test_envelope_keeps_existing_identifiers(self):
        record = load_record(FIXTURE)
        self.assertEqual(record.record_id, "sample-004")
        self.assertEqual(record.domain, "crop_settlement")
        self.assertEqual(record.occurred_at, "2026-09-20T09:00:00+08:00")
        self.assertEqual(record.schema_version, 2)

    def test_supply_record_loads_parcels_batches_commitments(self):
        record = load_supply_record(FIXTURE)
        self.assertEqual(len(record.parcels), 2)
        self.assertEqual(len(record.crop_batches), 2)
        self.assertEqual(len(record.commitments), 2)
        c1 = next(c for c in record.commitments if c.commitment_id == C1)
        self.assertEqual(c1.batch_id, BATCH_SESAME)
        self.assertEqual(c1.active_version().terms.floor_price_per_kg, Decimal("12.40"))
        self.assertTrue(c1.active_version().consent.consented)

    def test_import_books_prepayments(self):
        service = make_service()
        ledger = service.recompute_ledger(COOP, C1)
        self.assertEqual(ledger.prepayments, Decimal("3000.00"))
        self.assertEqual(ledger.balance_due, Decimal("-3000.00"))
        ledger2 = service.recompute_ledger(COOP, "C-0002")
        self.assertEqual(ledger2.prepayments, Decimal("5000.00"))


class DestinationTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()

    def test_record_alternative_destination(self):
        record = self.service.record_destination(
            COOP, "DST-1", BATCH_SESAME, "alternative_buyer", Decimal("120"), "工坊拒收等外品", at(25)
        )
        self.assertEqual(record.quantity_kg, Decimal("120.000"))
        listed = self.service.list_destinations(W1, BATCH_SESAME)
        self.assertEqual([d.record_id for d in listed], ["DST-1"])

    def test_destination_must_be_allowed_by_terms(self):
        with self.assertRaises(InvalidState):
            self.service.record_destination(
                COOP, "DST-1", BATCH_SESAME, "export", Decimal("50"), "未约定的去向", at(25)
            )


if __name__ == "__main__":
    unittest.main()
