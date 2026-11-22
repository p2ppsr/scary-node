import copy
import unittest

from checkpoint_profile import PHASES, profile_summary


def observations():
    rows = [{"event": "warmup"}]
    for pair in range(24):
        writers = ["existing", "buffered_hash"]
        if pair % 2:
            writers.reverse()
        for position, writer in enumerate(writers):
            phases = dict.fromkeys(PHASES, 2 if writer == "existing" else 1)
            rows.append(
                {
                    "event": "sample",
                    "pair": pair,
                    "position": position,
                    "save": {
                        "writer": writer,
                        "total_ns": sum(phases.values()),
                        **phases,
                    },
                }
            )
    return rows


class CheckpointProfileTests(unittest.TestCase):
    def test_pair_order_and_exact_phase_totals(self):
        full = observations()
        result = profile_summary(full)
        self.assertEqual(result["buffered_faster_pairs"], 24)
        self.assertEqual(result["b_over_a"]["median"], 0.5)
        self.assertEqual(
            result["by_order"]["buffered_then_existing"]["buffered_faster_pairs"], 12
        )
        bad = copy.deepcopy(full)
        bad[3], bad[4] = bad[4], bad[3]
        with self.assertRaisesRegex(ValueError, "order"):
            profile_summary(bad)
        with self.assertRaisesRegex(ValueError, "incomplete"):
            profile_summary(full[:-1])
        del full[1]["save"]["open_ns"]
        with self.assertRaisesRegex(ValueError, "phase"):
            profile_summary(full)
