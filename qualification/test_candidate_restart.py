import hashlib
import struct
import unittest

from restore_teranode_archive import payment_commitments
from verify_candidate_restart import root_outpoints


class CandidateRestartTests(unittest.TestCase):
    def test_ordinary_signed_membership_survives_a_reordering(self):
        before = payment_commitments([b"one", b"two"])
        after = payment_commitments([b"two", b"one"])
        for key in ("ordinary_wire_sha256", "ordered_internal_txids_sha256"):
            self.assertNotEqual(before[key], after[key])
        for key in ("sorted_ordinary_wire_sha256", "sorted_internal_txids_sha256"):
            self.assertEqual(before[key], after[key])
        with self.assertRaisesRegex(ValueError, "duplicate candidate payment"):
            payment_commitments([b"one", b"one"])

    def test_root_must_name_a_distinct_extant_parent_output(self):
        parent = hashlib.sha256(b"original tip payment").digest()
        prefix = struct.pack("<IB", 2, 1) + parent
        first = bytearray(prefix + struct.pack("<I", 0))
        second = prefix + struct.pack("<I", 1)
        outputs = {parent: 2}
        self.assertEqual(
            root_outpoints([first, second], outputs), {(parent, 0), (parent, 1)}
        )
        with self.assertRaisesRegex(ValueError, "duplicate candidate spend"):
            root_outpoints([first, first], outputs)
        with self.assertRaisesRegex(ValueError, "absent"):
            root_outpoints([second], {parent: 1})


if __name__ == "__main__":
    unittest.main()
