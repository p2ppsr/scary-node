"""The restart comparison requires both the same boundary and preservation of every prior payment."""

import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shadow_native import parse_tx, read_varint
from shadow_pending_restore import compare, dsha, follow_successor, inventory
from shadow_restore_boot import restore, rollback


def sample_raw(tag):
    # These small, framed payments exercise only source and arrival-order commitments.
    return bytes.fromhex(
        "0100000001"
        + f"{tag:02x}" * 32
        + "0000000000ffffffff010100000000000000015100000000"
    )


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

    def compare(self, *, awaiting_successor=False):
        with patch(
            "shadow_pending_restore.unpack_state", side_effect=(self.first, self.closed)
        ):
            return compare(self.path, self.path, awaiting_successor=awaiting_successor)

    def test_same_branch_preserves_old_and_adds_new(self):
        result = self.compare()
        self.assertEqual(
            (result["earlier_eligible"], result["closed_eligible"]), (1, 2)
        )
        self.assertEqual(result["removed_eligible"], 0)
        self.assertNotEqual(result["earlier_ids_sha256"], result["closed_ids_sha256"])

    def test_waiting_only_stop_preserves_attribution_and_order(self):
        first, last = sample_raw(1), sample_raw(2)
        self.first["waiting"] = [("sv", first)]
        self.closed["pending"] = self.first["pending"]
        self.closed["waiting"] = [("sv", first), ("subtree", last)]
        result = self.compare(awaiting_successor=True)
        self.assertEqual(result["added_eligible"], 0)
        self.assertEqual(result["closed_waiting"]["sources"], {"sv": 1, "subtree": 1})
        self.closed["waiting"].reverse()
        with self.assertRaisesRegex(ValueError, "earlier ordered offers changed"):
            self.compare(awaiting_successor=True)

    def test_duplicate_across_offer_pools_is_rejected(self):
        payment = sample_raw(1)
        self.closed["pending"][dsha(payment)] = {"raw": payment}
        self.closed["waiting"] = [("sv", payment)]
        with self.assertRaisesRegex(ValueError, "duplicate payment"):
            self.compare(awaiting_successor=True)

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


class SuccessorDisposition(unittest.TestCase):
    def setUp(self):
        evidence = Path(__file__).resolve().parents[1] / "docs/evidence"
        receipt = json.loads(
            (evidence / "chronicle-mainnet-waiting-restart.json").read_bytes()
        )
        wire = bytes.fromhex(receipt["first_successor_wire_hex"])
        self.body = Path(self.enterContext(tempfile.TemporaryDirectory())) / "block"
        self.body.write_bytes(wire)
        row = receipt["first_successor"]
        self.before = {
            "height": row["closed_height"],
            "parent": row["closed_parent"],
            "directive_sequence": 1,
        }
        self.after = {
            "height": row["successor_height"],
            "parent": row["successor"],
            "directive_sequence": 1,
        }
        _, start = read_varint(wire, 80)
        _, _, start = parse_tx(wire, start)
        payment, _, _ = parse_tx(wire, start)
        self.in_block = dsha(payment)
        self.in_pool, self.held, self.absent = [dsha(sample_raw(i)) for i in range(3)]
        self.waiting = [
            ("sv", ident, size)
            for ident, size in (
                (self.in_block, len(payment)),
                (self.in_pool, 92),
                (self.held, 100),
                (self.absent, 110),
            )
        ]

    def assess(self, admitted):
        states = [
            (self.before, "before", set(), self.waiting),
            (self.after, "after", admitted, [self.waiting[2]]),
        ]
        with patch("shadow_pending_restore.snapshot_inventory", side_effect=states):
            return follow_successor(None, None, self.body)

    def test_each_waiting_payment_receives_one_disposition(self):
        report = self.assess({self.in_pool})
        keys = (
            "confirmed_in_successor",
            "eligible_after_successor",
            "waiting_after_successor",
            "absent_from_successor_and_pools",
        )
        self.assertEqual([report[k] for k in keys], [1, 1, 1, 1])

    def test_confirmed_payment_cannot_be_counted_as_eligible(self):
        with self.assertRaisesRegex(ValueError, "more than one"):
            self.assess({self.in_pool, self.in_block})

    def test_body_must_extend_the_original_observed_tip(self):
        self.before["parent"] = "00" * 32
        with self.assertRaisesRegex(ValueError, "does not extend"):
            self.assess(set())


class IsolatedRollback(unittest.TestCase):
    def setUp(self):
        self.current = {
            "checkpoint_sha256": "same-file",
            "checkpoint_waiting": {"count": 12, "bytes": 36013},
            "executable_sha256": "current",
            "restored_waiting_count": 12,
            "restored_waiting_bytes": 36013,
        }
        self.older = dict(
            self.current,
            executable_sha256="older",
            restored_waiting_count=0,
            restored_waiting_bytes=0,
        )

    def test_current_restore_still_rejects_a_missing_queue(self):
        with (
            patch("shadow_restore_boot.observe", return_value=self.older),
            self.assertRaisesRegex(ValueError, "inventory differs"),
        ):
            restore(None, None, None)

    def test_earlier_boot_reports_the_unavailable_queue(self):
        with patch(
            "shadow_restore_boot.observe", side_effect=[self.older, self.current]
        ):
            result = rollback(None, None, None, None)
        self.assertEqual(result["rollback_boot"]["restored_waiting_count"], 0)
        self.assertEqual(result["return_to_release"]["restored_waiting_count"], 12)

    def test_comparison_rejects_substituting_another_snapshot(self):
        self.older["checkpoint_sha256"] = "other-file"
        with (
            patch(
                "shadow_restore_boot.observe", side_effect=[self.older, self.current]
            ),
            self.assertRaisesRegex(ValueError, "same immutable checkpoint"),
        ):
            rollback(None, None, None, None)


if __name__ == "__main__":
    unittest.main()
