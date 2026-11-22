import unittest

from shadow_subtree_tail import COUNT, summary


class BoundedTailTest(unittest.TestCase):
    def test_same_poll_has_no_internal_arrival_order_timing(self):
        r = {
            "batches": [[0, 20000, [0, 1]], [20000, 40000, [1] * (COUNT - 2)]],
            "elapsed_us": 40000,
            "cpu_us": 200,
            "peak_rss_bytes": 1024,
            "bytes_read": 10,
        }
        result = summary(r)
        self.assertEqual(result["median_nonfull_to_next_interval_us"], [0, 20000])
        self.assertEqual(result["queue_full"], COUNT - 1)
        r["batches"][1][0] = 19000
        with self.assertRaisesRegex(ValueError, "overlapping"):
            summary(r)
