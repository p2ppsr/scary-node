"""The restart comparison requires both the same boundary and preservation of every prior payment."""

import copy
import io
import unittest
from unittest.mock import patch

from shadow_pending_restore import compare, dsha, inventory


class Checkpoint:
    def open(self, _mode):
        return io.BytesIO()


class StopComparison(unittest.TestCase):
    def setUp(self):
        self.path = Checkpoint()
        self.tx = b"payment observed over native peering"
        self.next_tx = b"payment observed after the checkpoint"
        self.first = {
            "_checkpoint_sha256": "00" * 32,
            "height": 42,
            "headers": [bytes(80)],
            "coins": {
                (bytes(32), 0): {
                    "script": b"\x51",
                    "height": 40,
                    "value": 7,
                    "coinbase": False,
                    "confiscation": False,
                },
            },
            "pending": {dsha(self.tx): {"raw": self.tx}},
            "applied_sequence": 1,
        }
        self.closed = copy.deepcopy(self.first)
        self.closed["_checkpoint_sha256"] = "ff" * 32
        self.closed["pending"][dsha(self.next_tx)] = {"raw": self.next_tx}

    def compare(self):
        with patch(
            "shadow_pending_restore.unpack_state", side_effect=(self.first, self.closed)
        ):
            return compare(self.path, self.path)

    def test_same_branch_preserves_old_and_adds_new(self):
        result = self.compare()
        self.assertEqual(
            (result["earlier_eligible"], result["closed_eligible"]), (1, 2)
        )
        self.assertEqual(result["removed_eligible"], 0)
        self.assertNotEqual(result["earlier_ids_sha256"], result["closed_ids_sha256"])

    def test_substitution_is_detected_even_when_the_pool_grows(self):
        del self.closed["pending"][dsha(self.tx)]
        replacement = b"replacement"
        self.closed["pending"][dsha(replacement)] = {"raw": replacement}
        with self.assertRaisesRegex(ValueError, "preserving the original"):
            self.compare()

    def test_changed_witnessed_value_is_detected(self):
        self.closed["coins"][(bytes(32), 0)]["value"] += 1
        with self.assertRaisesRegex(
            ValueError, "different witnessed branches or directives"
        ):
            self.compare()

    def test_payload_cannot_borrow_another_payment_id(self):
        self.first["pending"][dsha(self.tx)]["raw"] = self.next_tx
        with (
            patch("shadow_pending_restore.unpack_state", return_value=self.first),
            self.assertRaisesRegex(ValueError, "differs from its ID"),
        ):
            inventory(self.path)


if __name__ == "__main__":
    unittest.main()
