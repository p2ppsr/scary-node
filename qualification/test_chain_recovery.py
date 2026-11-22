import copy
import hashlib
import io
import struct
import unittest

from cadence_transport import MAGIC
from recover_stock_archive import identity, inspect_block_file
from recover_sustained_chain import selected_order
from restore_witness_archive import canonical, verify_boot


class RecoveryTests(unittest.TestCase):
    def test_durable_block_file_framing_and_padding(self):
        raw = b"b" * 80 + b"payment"
        ident = identity(raw)
        expected = {
            ident: {
                "wire_sha256": hashlib.sha256(raw).hexdigest(),
                "wire_bytes": len(raw),
            }
        }
        wire = MAGIC[::-1] + struct.pack("<I", len(raw)) + raw
        found = set()
        self.assertEqual(
            inspect_block_file(
                io.BytesIO(wire + bytes(15)), expected, found, set(), set()
            ),
            (1, 15),
        )
        self.assertEqual(found, {ident})
        for bad in (wire[:-1], wire[:-1] + b"X", wire + bytes(8) + b"nonzero"):
            with self.assertRaises(ValueError):
                inspect_block_file(io.BytesIO(bad), expected, set(), set(), set())

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
