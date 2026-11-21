"""Verify that the registered schedules and cold/endorsed start states remain distinct."""

from collections import Counter
import hashlib
import unittest

from sustained_profiles import (
    ENDORSED_HASH,
    ENDORSED_SHA,
    ENDORSEMENT,
    FUNDING,
    PROJECTS,
    REGISTRATIONS,
    batch_order,
    cohort_by_batch,
)
from verify_sustained_native import check_header, decode_block


class WitnessedWorkloadTests(unittest.TestCase):
    def test_schedule_preserves_inputs_and_native_lanes(self):
        order = list(batch_order("reordered"))
        self.assertEqual(sorted(order), list(range(10_800)))
        self.assertEqual(order[:1024], list(range(1024)))
        self.assertEqual(order[10752:], list(range(10752, 10800)))
        positions = {batch: pos for pos, batch in enumerate(order)}
        self.assertTrue(all(pos % 4 == batch % 4 for batch, pos in positions.items()))
        self.assertTrue(
            all(positions[n] > positions[n - 128] for n in range(128, 10800))
        )
        self.assertEqual(
            (positions[1152] - positions[1024], positions[1536] - positions[1408]),
            (32, 416),
        )
        cohorts = cohort_by_batch("reordered")
        self.assertEqual(Counter(cohorts), dict.fromkeys(range(6), 1800))
        self.assertEqual(cohorts[1792], positions[1792] // 1800)
        self.assertTrue(any(cohorts[i] != i // 1800 for i in positions))

    def test_endorsed_wire_parent_merkle_and_registered_builds(self):
        self.assertEqual(hashlib.sha256(ENDORSEMENT).hexdigest(), ENDORSED_SHA)
        check_header(ENDORSEMENT[:80], ENDORSED_HASH, FUNDING)
        _, txs, _ = decode_block(ENDORSEMENT, ENDORSED_HASH)
        self.assertEqual(len(txs), 1)
        self.assertEqual(len(ENDORSEMENT), 145)
        self.assertEqual(list(batch_order("cold")), list(batch_order("endorsed")))
        self.assertNotEqual(REGISTRATIONS["cold"], REGISTRATIONS["endorsed"])
        self.assertEqual(REGISTRATIONS["endorsed"], REGISTRATIONS["reordered"])
        self.assertEqual(len(set(PROJECTS.values())), len(PROJECTS))


if __name__ == "__main__":
    unittest.main()
