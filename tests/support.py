"""测试共享的装置：从样例文件构建服务，提供常用身份与时间。"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))

from crop_settlement import (  # noqa: E402
    COOP,
    Principal,
    Role,
    SettlementService,
    import_supply_record,
    load_supply_record,
)

TZ = timezone(timedelta(hours=8))
T0 = datetime(2026, 9, 20, 9, 0, tzinfo=TZ)  # 与样例 occurred_at 一致

W1 = Principal(Role.WORKSHOP, "W-01")
W2 = Principal(Role.WORKSHOP, "W-02")
F1 = Principal(Role.FARMER, "F-01")
F2 = Principal(Role.FARMER, "F-02")

BATCH_SESAME = "B-2026-ZHIMA-01"   # C-0001: F-01 -> W-01，保底 12.40，承诺 800kg
BATCH_PEANUT = "B-2026-HUASHENG-01"  # C-0002: F-02 -> W-02，保底 9.80，承诺 1200kg
C1 = "C-0001"
C2 = "C-0002"


def at(day: int, hour: int = 9) -> datetime:
    """2026 年 9 月某日的北京时间。"""
    return datetime(2026, 9, day, hour, tzinfo=TZ)


def make_service() -> SettlementService:
    service = SettlementService()
    import_supply_record(service, load_supply_record(ROOT / "fixtures" / "supply_commitment.json"))
    return service


def weigh(service, batch_id: str, entries) -> None:
    """entries: (weighing_id, site_id, net_kg) 列表，皮重简化并入净重。"""
    from decimal import Decimal

    from crop_settlement.domain import Weighing, kg

    for weighing_id, site_id, net in entries:
        net = kg(Decimal(str(net)))
        service.record_weighing(
            COOP,
            Weighing(
                weighing_id=weighing_id,
                batch_id=batch_id,
                site_id=site_id,
                gross_kg=net,
                tare_kg=kg(0),
                net_kg=net,
                at=T0,
            ),
        )
