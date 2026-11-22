import unittest

from workload_block_fees import external_input_age


class ExternalInputAgeTests(unittest.TestCase):
    def test_same_block_chains_inherit_their_own_first_external_input(self):
        roots = {}
        heights = [(2, 19), (7, 17), (7, 20), (2, 20), (7, 20)]
        counts = [external_input_age(20, prior, rail, roots) for rail, prior in heights]
        self.assertEqual(counts, [1, 3, 3, 1, 3])

    def test_child_must_follow_its_same_block_parent(self):
        with self.assertRaises(ValueError):
            external_input_age(20, 20, 7, {})


if __name__ == "__main__":
    unittest.main()
