import unittest

from workload_waiting_offers import COUNTERS, PERIOD, ROUTES, measure


def observed(ns, n):
    return {
        "scary": {
            "read_ns": ns,
            "height": 484 + n,
            "peer_intake": {route: dict.fromkeys(COUNTERS, n) for route in ROUTES},
        }
    }


class WaitingOfferTests(unittest.TestCase):
    def test_sampled_bounds_and_drain_keep_the_measured_deltas(self):
        result = measure(
            [
                observed(8, 0),
                observed(PERIOD, 1),
                observed(PERIOD + 1, 2),
                observed(6 * PERIOD + 2, 3),
            ]
        )
        first, second, third, *_, drain = result["intervals"]
        self.assertEqual(first["last_entrant_read_ns"], PERIOD)
        self.assertEqual(second["first_entrant_read_ns"], PERIOD)
        self.assertEqual(first["changes"]["sv"]["awaiting_successor"], 1)
        self.assertEqual(second["changes"]["sv"]["awaiting_successor"], 1)
        self.assertEqual(third["changes"], {})
        self.assertEqual(drain["changes"]["teranode_subtree"]["admitted"], 1)

    def test_reset_is_unavailable_and_missing_features_are_not_counted(self):
        early = measure([observed(PERIOD + 1, 0), observed(2 * PERIOD + 3, 1)])[
            "intervals"
        ][0]
        self.assertIsNone(early["last_entrant_read_ns"])
        self.assertEqual(early["endpoint_heights"], [])
        rows = [observed(1, 3), observed(2, 2)]
        self.assertIsNone(
            measure(rows)["intervals"][0]["changes"]["sv"]["awaiting_successor"]
        )
        self.assertEqual(
            measure([{"scary": {"peer_intake": {"sv": {"admitted": 5}}}}])[
                "observations"
            ],
            0,
        )


if __name__ == "__main__":
    unittest.main()
