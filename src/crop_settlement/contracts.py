"""读取项目已确认的最小数据合同，不包含业务流程实现。

信封字段（schema_version / record_id / domain / occurred_at / revision /
source）自 schema_version 1 起保持不变。schema_version 2 在信封之外新增了
parcels / crop_batches / commitments 等业务负载，由
`crop_settlement.loader` 负责解析；本模块只读取信封，忽略其余字段，
因此旧读者无需迁移即可继续读取新样例。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from pathlib import Path

@dataclass(frozen=True)
class DomainRecord:
    schema_version: int
    record_id: str
    domain: str
    occurred_at: str
    revision: int
    source: str

_ENVELOPE_FIELDS = frozenset(f.name for f in fields(DomainRecord))

def load_record(path: Path) -> DomainRecord:
    payload = json.loads(path.read_text(encoding="utf-8"))
    envelope = {key: value for key, value in payload.items() if key in _ENVELOPE_FIELDS}
    return DomainRecord(**envelope)
