import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from crop_settlement import load_dataset, load_record  # noqa: E402

FIXTURE = Path(__file__).parents[1] / "fixtures" / "supply_commitment.json"


class ContractTest(unittest.TestCase):
    def test_example_uses_current_contract(self):
        item = load_record(FIXTURE)
        self.assertEqual(item.domain, "crop_settlement")
        self.assertGreater(item.revision, 0)

    def test_envelope_fields_preserved(self):
        # revision 升级不得改变信封字段的标识与时间语义
        item = load_record(FIXTURE)
        self.assertEqual(item.schema_version, 1)
        self.assertEqual(item.record_id, "sample-004")
        self.assertEqual(item.occurred_at, "2026-09-20T09:00:00+08:00")

    def test_revision2_loads_dataset(self):
        record, store = load_dataset(FIXTURE)
        self.assertEqual(record.revision, 2)
        self.assertEqual(len(store.parties), 5)
        self.assertEqual(len(store.commitments), 3)
        # 数量守恒：每个交付的处置之和等于净重
        for d in store.deliveries.values():
            total = sum(x.quantity_kg for x in store.dispositions_for(d.id))
            self.assertEqual(total, d.net_kg, d.id)

    def test_revision1_still_loads_as_empty_dataset(self):
        legacy = {
            "schema_version": 1,
            "record_id": "sample-004",
            "domain": "crop_settlement",
            "occurred_at": "2026-09-20T09:00:00+08:00",
            "revision": 1,
            "source": "旧样例",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.json"
            path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
            record = load_record(path)
            self.assertEqual(record.revision, 1)
            record2, store = load_dataset(path)
            self.assertEqual(store.parties, {})
            self.assertEqual(record2.record_id, "sample-004")


if __name__ == "__main__":
    unittest.main()
