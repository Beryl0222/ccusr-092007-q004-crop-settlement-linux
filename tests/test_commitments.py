"""承诺版本化：价格/规则变化只进入农户明确同意的新版本。"""

from __future__ import annotations

import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from crop_settlement.errors import ConflictError, PermissionDenied, ValidationError  # noqa: E402
from crop_settlement.models import CommitmentStatus, CommitmentTerms, DateWindow, PriceTier  # noqa: E402
from tests.support import WorldCase  # noqa: E402


def _agreed_version(commitment) -> int:
    for v in reversed(commitment.versions):
        if v.status == CommitmentStatus.AGREED:
            return v.version_no
    raise AssertionError("无已同意版本")


class CommitmentVersioningTest(WorldCase):
    def test_initial_version_proposed_until_farmer_agrees(self):
        self.w.svc.create_commitment(
            "comX", self.w.f1.id, self.w.ws1.id, "batch1", "sesame",
            Decimal("100"), self.w.terms, created_by="ws1",
        )
        c = self.w.store.get_commitment("comX")
        self.assertEqual(c.status, CommitmentStatus.PROPOSED)
        # 没有同意版本时不能登记交付
        with self.assertRaises(ValidationError):
            self.w.svc.register_delivery(
                "comX", "st1a", "100", "0", "op", on_date=date(2026, 9, 5),
            )

    def test_only_farmer_can_agree(self):
        self.w.svc.create_commitment(
            "comX", self.w.f1.id, self.w.ws1.id, "batch1", "sesame",
            Decimal("100"), self.w.terms, created_by="ws1",
        )
        with self.assertRaises(PermissionDenied):
            self.w.svc.agree_commitment("comX", self.w.ws1)
        self.w.svc.agree_commitment("comX", self.w.f1)
        # 重复同意被拒绝
        with self.assertRaises(ConflictError):
            self.w.svc.agree_commitment("comX", self.w.f1)

    def test_price_change_creates_new_version_not_applied_until_agreed(self):
        new_terms = CommitmentTerms(
            floor_price_per_kg=Decimal("13"),
            grade_prices=[
                PriceTier("A", Decimal("17")),
                PriceTier("B", Decimal("14")),
                PriceTier("C", Decimal("11")),
            ],
            prepayment_amount=Decimal("5000"),
            delivery_window=DateWindow(date(2026, 9, 1), date(2026, 9, 30)),
        )
        self.w.svc.revise_commitment(
            "com1", new_terms, created_by=self.w.ws1.id,
            change_summary="市场价上调",
        )
        c = self.w.store.get_commitment("com1")
        self.assertEqual(c.current.version_no, 2)
        self.assertEqual(c.status, CommitmentStatus.PROPOSED)
        # 待同意期间旧版本继续有效，结算仍依据它
        self.assertEqual(c.versions[0].status, CommitmentStatus.AGREED)
        self.assertEqual(_agreed_version(c), 1)

        self.w.svc.agree_commitment("com1", self.w.f1)
        self.assertEqual(_agreed_version(self.w.store.get_commitment("com1")), 2)
        self.assertEqual(c.versions[0].status, CommitmentStatus.SUPERSEDED)
        self.assertEqual(c.current.terms.floor_price_per_kg, Decimal("13"))

    def test_cannot_revise_without_agreed_base(self):
        self.w.svc.create_commitment(
            "comY", self.w.f1.id, self.w.ws1.id, "batch1", "sesame",
            Decimal("100"), self.w.terms, created_by="ws1",
        )
        with self.assertRaises(ValidationError):
            self.w.svc.revise_commitment("comY", self.w.terms, "ws1", "x")

    def test_invalid_terms_rejected(self):
        bad = CommitmentTerms(
            floor_price_per_kg=Decimal("12"),
            grade_prices=[PriceTier("A", Decimal("16"))],
            prepayment_amount=Decimal("5000"),
            delivery_window=DateWindow(date(2026, 10, 1), date(2026, 9, 1)),
        )
        with self.assertRaises(ValidationError):
            self.w.svc.create_commitment(
                "comZ", self.w.f1.id, self.w.ws1.id, "batch1", "sesame",
                Decimal("100"), bad, created_by="ws1",
            )


if __name__ == "__main__":
    unittest.main()
