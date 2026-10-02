"""减产协商：先证据后定案，不可抗与未履约分别记录。"""

from __future__ import annotations

import unittest
from decimal import Decimal

from support import BATCH_SESAME, C1, COOP, F1, W1, at, make_service, weigh

from crop_settlement.domain import Evidence, NegotiationCause, NegotiationStatus, QualityMetrics
from crop_settlement.errors import AccessDenied, EvidenceRequired, QuantityExceeded

G2 = QualityMetrics(moisture_pct=Decimal("8.5"), impurity_pct=Decimal("1.5"))


def evidence(evidence_id: str = "EV-1") -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        kind="气象记录",
        reference="县气象台 2026-09 连续降雨证明",
        submitted_by="F-01",
        at=at(26),
    )


class NegotiationTest(unittest.TestCase):
    def setUp(self):
        self.service = make_service()

    def test_resolution_requires_evidence(self):
        self.service.open_negotiation(F1, "NEG-1", C1, NegotiationCause.FLOOD, Decimal("300"), at(25))
        with self.assertRaises(EvidenceRequired):
            self.service.resolve_negotiation(
                COOP, "NEG-1", agree=True, at=at(26), force_majeure_kg=Decimal("300")
            )

    def test_force_majeure_and_non_performance_recorded_separately(self):
        self.service.open_negotiation(F1, "NEG-1", C1, NegotiationCause.FLOOD, Decimal("300"), at(25))
        self.service.submit_evidence(F1, "NEG-1", evidence())
        case = self.service.resolve_negotiation(
            COOP,
            "NEG-1",
            agree=True,
            at=at(27),
            force_majeure_kg=Decimal("200"),
            non_performance_kg=Decimal("100"),
        )
        self.assertEqual(case.status, NegotiationStatus.AGREED)
        report = self.service.obligation_report(COOP, C1)
        self.assertEqual(report.committed_quantity_kg, Decimal("800.000"))
        self.assertEqual(report.force_majeure_excused_kg, Decimal("200.000"))
        self.assertEqual(report.non_performance_kg, Decimal("100.000"))
        self.assertEqual(report.outstanding_quantity_kg, Decimal("500.000"))

    def test_resolution_cannot_exceed_outstanding(self):
        weigh(self.service, BATCH_SESAME, [("WGH-1", "S-1", "700")])
        self.service.accept_delivery(
            W1, "ACC-1", BATCH_SESAME, "S-1", Decimal("700"), at(21), field_metrics=G2
        )
        self.service.open_negotiation(F1, "NEG-1", C1, NegotiationCause.DROUGHT, Decimal("200"), at(25))
        self.service.submit_evidence(F1, "NEG-1", evidence())
        with self.assertRaises(QuantityExceeded):
            self.service.resolve_negotiation(
                COOP, "NEG-1", agree=True, at=at(26), force_majeure_kg=Decimal("200")
            )

    def test_workshop_cannot_open_negotiation(self):
        with self.assertRaises(AccessDenied):
            self.service.open_negotiation(W1, "NEG-1", C1, NegotiationCause.FLOOD, Decimal("100"), at(25))

    def test_rejected_case_keeps_obligation_untouched(self):
        self.service.open_negotiation(F1, "NEG-1", C1, NegotiationCause.OTHER, Decimal("100"), at(25))
        self.service.submit_evidence(F1, "NEG-1", evidence())
        self.service.resolve_negotiation(COOP, "NEG-1", agree=False, at=at(26))
        report = self.service.obligation_report(COOP, C1)
        self.assertEqual(report.force_majeure_excused_kg, Decimal("0.000"))
        self.assertEqual(report.outstanding_quantity_kg, Decimal("800.000"))


if __name__ == "__main__":
    unittest.main()
