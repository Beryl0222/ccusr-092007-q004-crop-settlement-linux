"""条款版本：价格或规则变化只进入农户明确同意的新版本。"""

from __future__ import annotations

import unittest
from dataclasses import replace
from decimal import Decimal

from support import BATCH_SESAME, C1, COOP, F1, F2, W1, at, make_service, weigh

from crop_settlement.domain import Commitment, CommitmentVersion, VersionStatus
from crop_settlement.errors import AccessDenied, ConsentRequired, InvalidState
from crop_settlement.domain import QualityMetrics

G2 = QualityMetrics(moisture_pct=Decimal("8.5"), impurity_pct=Decimal("1.5"))


class VersioningTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()
        weigh(self.service, BATCH_SESAME, [("WGH-1", "S-1", "800")])
        self.commitment = self.service.get_commitment(COOP, C1)
        self.v1_terms = self.commitment.active_version().terms

    def _propose(self, floor: str, version: int = 2):
        new_terms = replace(self.v1_terms, floor_price_per_kg=Decimal(floor))
        return self.service.propose_terms(COOP, C1, new_terms, at(25))

    def test_unconsented_version_does_not_apply(self):
        self._propose("13.00")
        stmt = self.service.accept_delivery(
            W1, "ACC-1", BATCH_SESAME, "S-1", Decimal("100"), at(26), field_metrics=G2
        )
        self.assertEqual(stmt.unit_price, Decimal("12.40"))  # 仍按旧版本
        self.assertEqual(stmt.terms_version, 1)

    def test_rejected_version_keeps_old_terms(self):
        self._propose("13.00")
        self.service.consent_terms(F1, C1, 2, agree=False, at=at(26))
        self.assertEqual(self.commitment.active_version().version, 1)
        self.assertEqual(self.commitment.version(2).status, VersionStatus.REJECTED)

    def test_consented_version_applies_to_new_deliveries(self):
        self._propose("13.00")
        self.service.consent_terms(F1, C1, 2, agree=True, at=at(26), channel="小程序确认")
        self.assertEqual(self.commitment.active_version().version, 2)
        self.assertEqual(self.commitment.version(1).status, VersionStatus.SUPERSEDED)
        stmt = self.service.accept_delivery(
            W1, "ACC-1", BATCH_SESAME, "S-1", Decimal("100"), at(27), field_metrics=G2
        )
        self.assertEqual(stmt.unit_price, Decimal("13.00"))
        self.assertEqual(stmt.terms_version, 2)

    def test_only_the_farmer_can_consent(self):
        self._propose("13.00")
        with self.assertRaises(AccessDenied):
            self.service.consent_terms(F2, C1, 2, agree=True, at=at(26))
        with self.assertRaises(AccessDenied):
            self.service.consent_terms(COOP, C1, 2, agree=True, at=at(26))

    def test_cannot_consent_twice(self):
        self._propose("13.00")
        self.service.consent_terms(F1, C1, 2, agree=True, at=at(26))
        with self.assertRaises(InvalidState):
            self.service.consent_terms(F1, C1, 2, agree=False, at=at(27))

    def test_register_commitment_requires_farmer_consent(self):
        draft_only = Commitment(
            commitment_id="C-0009",
            farmer_id="F-01",
            workshop_id="W-01",
            batch_id="B-2026-HUASHENG-01",
            versions=[
                CommitmentVersion(
                    version=1,
                    terms=self.v1_terms,
                    status=VersionStatus.ACTIVE,
                    created_at=at(25),
                    consent=None,
                )
            ],
        )
        with self.assertRaises(ConsentRequired):
            self.service.register_commitment(COOP, draft_only)


if __name__ == "__main__":
    unittest.main()
