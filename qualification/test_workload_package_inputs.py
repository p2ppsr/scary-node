import unittest

from workload_package_inputs import participant_counts


def row(height, payments):
    return {"height": height, "payments": payments, "hash": hex(height), "owner": "ref"}


class PackageInputTests(unittest.TestCase):
    def test_separate_rails_and_descendants_advance_by_height_and_generation(self):
        blocks = [(row(11, 1), [0]), (row(12, 4), [1, 2, 3, 5]), (row(14, 2), [4, 6])]
        first, mixed, later = participant_counts(blocks, 2, 10, 8)
        self.assertEqual(first["external_ancestor_age"], {1: 1})
        self.assertEqual(mixed["direct_parent_age"], {0: 2, 1: 1, 2: 1})
        self.assertEqual(mixed["external_ancestor_age"], {1: 1, 2: 3})
        self.assertEqual(later["external_ancestor_age"], {2: 2})

    def test_missing_generation_cannot_supply_same_block_eligibility(self):
        with self.assertRaises(ValueError):
            participant_counts([(row(11, 1), [2])], 2, 10, 8)


if __name__ == "__main__":
    unittest.main()
