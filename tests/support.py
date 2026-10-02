"""测试夹具：搭建一个合作社 + 两个工坊 + 两个农户 + 芝麻承诺的最小场景。"""

from __future__ import annotations

import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from crop_settlement.models import (  # noqa: E402
    CommitmentTerms,
    DateWindow,
    PriceTier,
)
from crop_settlement.services import SettlementService  # noqa: E402
from crop_settlement.store import Store  # noqa: E402


class World:
    def __init__(self) -> None:
        self.store = Store()
        self.svc = SettlementService(self.store)

        self.coop = self.svc.register_party("coop", "coop", "合作社", token="t-coop")
        self.ws1 = self.svc.register_party("ws1", "workshop", "甲工坊", token="t-ws1")
        self.ws2 = self.svc.register_party("ws2", "workshop", "乙工坊", token="t-ws2")
        self.f1 = self.svc.register_party("f1", "farmer", "甲农户", token="t-f1")
        self.f2 = self.svc.register_party("f2", "farmer", "乙农户", token="t-f2")

        self.st1a = self.svc.open_station(self.ws1, "st1a", "甲工坊东收货点")
        self.st1b = self.svc.open_station(self.ws1, "st1b", "甲工坊西收货点")
        self.st2 = self.svc.open_station(self.ws2, "st2", "乙工坊收货点")

        self.svc.register_plot(self.f1, "plot1", "sesame", area_mu="5")
        self.svc.register_batch("batch1", "plot1", "sesame")
        self.svc.register_plot(self.f2, "plot2", "peanut", area_mu="3")
        self.svc.register_batch("batch2", "plot2", "peanut")

        self.terms = CommitmentTerms(
            floor_price_per_kg=Decimal("12"),
            grade_prices=[
                PriceTier("A", Decimal("16")),
                PriceTier("B", Decimal("13")),
                PriceTier("C", Decimal("10")),
            ],
            prepayment_amount=Decimal("5000"),
            delivery_window=DateWindow(date(2026, 9, 1), date(2026, 9, 30)),
            payment_terms_days=7,
            alternate_allowed=True,
            alternate_handling_fee_per_kg=Decimal("0.2"),
        )
        self.svc.create_commitment(
            "com1", self.f1.id, self.ws1.id, "batch1", "sesame",
            Decimal("1000"), self.terms, created_by=self.ws1.id,
        )
        self.svc.agree_commitment("com1", self.f1)

        # 乙工坊与乙农户的独立采购关系，用于隔离测试
        self.terms2 = CommitmentTerms(
            floor_price_per_kg=Decimal("7"),
            grade_prices=[PriceTier("A", Decimal("9")), PriceTier("B", Decimal("7.5"))],
            prepayment_amount=Decimal("1000"),
            delivery_window=DateWindow(date(2026, 9, 1), date(2026, 10, 15)),
            payment_terms_days=10,
            alternate_allowed=False,
            alternate_handling_fee_per_kg=Decimal("0"),
        )
        self.svc.create_commitment(
            "com2", self.f2.id, self.ws2.id, "batch2", "peanut",
            Decimal("500"), self.terms2, created_by=self.ws2.id,
        )
        self.svc.agree_commitment("com2", self.f2)


class WorldCase(unittest.TestCase):
    def setUp(self) -> None:
        self.w = World()
