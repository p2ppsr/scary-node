import copy
import unittest

from subtree_scheduler import one


class SchedulerCriterionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = {
            "parent": "same",
            "roots": [{"ids": [f"{i}-a", f"{i}-b"]} for i in range(64)],
        }
        offers = [
            {"index": i, "at_us": 20000 * i, "accepted": i < 2} for i in range(64)
        ]
        cls.row = {
            "treatment": "parallel",
            "sample": {
                "parallel": 4,
                "capacity": 256,
                "offers": offers,
                "requests": [[1, 20500, 120500], [0, 100, 400100]],
                "drained_us": 1261000,
                "parent_before": "same",
                "parent_after": "same",
                "transaction_ids": ["0-a", "0-b", "1-a", "1-b"],
            },
        }

    def test_response_order_can_differ_from_admission_order(self):
        result = one(self.row, self.fixture)
        self.assertTrue(result["first_root_finished_after_later_root"])
        self.assertEqual(
            (
                result["accepted"],
                result["full"],
                result["peak_requests"],
                result["ordered_payments"],
            ),
            (2, 62, 2, 4),
        )

    def test_source_parent_and_order_must_reconcile(self):
        for field in ("requests", "parent_after", "transaction_ids"):
            with self.subTest(field=field):
                row = copy.deepcopy(self.row)
                s = row["sample"]
                if field == "requests":
                    s[field].pop()
                elif field == "parent_after":
                    s[field] = "other"
                else:
                    s[field].reverse()
                if field == "requests":
                    with self.assertRaises(ValueError):
                        one(row, self.fixture)
                else:
                    result = one(row, self.fixture)
                    self.assertFalse(
                        result["same_parent"]
                        if field == "parent_after"
                        else result["matches_admission_order"]
                    )
