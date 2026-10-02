"""节令原料保底结算后端。

- contracts：样例信封的最小合同（既有标识与时间含义不变）
- domain：实体、值对象与状态机
- grading：浮动分级与保底价计算
- service：收货、结算、协商、申诉、总账与追溯
- loader：读取 supply_commitment.json 样例并导入服务
"""

from .contracts import DomainRecord, load_record
from .domain import (
    COOP,
    Commitment,
    CommitmentTerms,
    CropBatch,
    CropType,
    DeliveryStatement,
    Grade,
    LedgerView,
    Parcel,
    Principal,
    QualityMetrics,
    Role,
)
from .errors import (
    AccessDenied,
    ConsentRequired,
    DomainError,
    EvidenceRequired,
    InvalidState,
    NotFound,
    OutsideDeliveryWindow,
    OverReceiveError,
    QuantityExceeded,
)
from .loader import SupplyRecord, import_supply_record, load_supply_record
from .service import SettlementService
from .store import Store

__all__ = [
    "AccessDenied",
    "COOP",
    "Commitment",
    "CommitmentTerms",
    "ConsentRequired",
    "CropBatch",
    "CropType",
    "DeliveryStatement",
    "DomainError",
    "DomainRecord",
    "EvidenceRequired",
    "Grade",
    "InvalidState",
    "LedgerView",
    "NotFound",
    "OutsideDeliveryWindow",
    "OverReceiveError",
    "Parcel",
    "Principal",
    "QualityMetrics",
    "QuantityExceeded",
    "Role",
    "SettlementService",
    "Store",
    "SupplyRecord",
    "import_supply_record",
    "load_record",
    "load_supply_record",
]
