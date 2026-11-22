"""The worker checks initial and appended transaction order exactly as issued."""
import unittest

from prove import coinbase_branches, dsha


class WorkerMerkleTests(unittest.TestCase):
    def test_append_preserves_admission_order_and_duplicates_only_odd_tail(self):
        first, second, third = (bytes([b]) * 32 for b in (240, 7, 120))
        ids = [d[::-1].hex() for d in (first, second, third)]
        self.assertEqual(coinbase_branches(ids[:1]), [first.hex()])
        self.assertEqual(coinbase_branches(ids[:2]), [first.hex(), dsha(second * 2).hex()])
        self.assertEqual(coinbase_branches(ids), [first.hex(), dsha(second + third).hex()])
        self.assertNotEqual(coinbase_branches(ids), coinbase_branches(sorted(ids)))


if __name__ == "__main__":
    unittest.main()
