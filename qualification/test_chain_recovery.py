import copy
import unittest

from recover_sustained_chain import selected_order
from restore_witness_archive import canonical, verify_boot


class RecoveryTests(unittest.TestCase):
    def test_selected_after_detach_keeps_terminal_order(self):
        parent = {"hash": "first"}
        old_tip = {"hash": "previously-detached"}
        rows = [parent, old_tip]
        early = [(old_tip, b"second"), (parent, b"first")]
        self.assertEqual(list(selected_order(rows, early, {})), list(reversed(early)))
        with self.assertRaises(ValueError):
            list(selected_order(rows, early[:1], {}))

    def test_repaired_window_must_match_observed_script_counts(self):
        tip = "ab" * 32
        measured = {
            "release_sha256": "12" * 32,
            "blocks": [
                {
                    "hash": tip,
                    "height": 72,
                    "entrant_script_checks": 4,
                    "ordinary_transactions": 5,
                }
            ],
            "completion": {"final_hash": tip, "final_height": 72},
        }
        proof = {
            "measured_record_sha256": canonical(measured),
            "release_sha256": "12" * 32,
            "image_id": "sha256:" + "34" * 32,
            "connected_data_peers": 0,
            "checkpoint_sha256": "56" * 32,
            "checkpoint_bytes": 123,
            "retained_window_rows": 1,
            "compared_measured_block_rows": 1,
            "identical_complete_window": False,
            "restored": {
                "height": 72,
                "tip_hash": tip,
                "coverage_start": 72,
                "coverage_end": 72,
                "witnessed_outputs": 7,
            },
            "restored_validation_window": [
                {
                    "hash": tip,
                    "height": 72,
                    "script_checks_passed": 4,
                    "noncoinbase_transactions": 5,
                }
            ],
        }
        self.assertEqual(verify_boot(proof, measured)["matched_measured_blocks"], 1)
        changed = copy.deepcopy(proof)
        changed["restored_validation_window"][0]["script_checks_passed"] = 0
        with self.assertRaises(ValueError):
            verify_boot(changed, measured)
