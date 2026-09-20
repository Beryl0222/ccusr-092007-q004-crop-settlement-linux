import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from crop_settlement import load_record

class ContractTest(unittest.TestCase):
    def test_example_uses_current_contract(self):
        item = load_record(Path(__file__).parents[1] / "fixtures" / "supply_commitment.json")
        self.assertEqual(item.domain, "crop_settlement")
        self.assertGreater(item.revision, 0)

if __name__ == "__main__":
    unittest.main()
