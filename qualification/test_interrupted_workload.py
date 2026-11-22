import contextlib
import copy
import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from interrupted_workload import restore_stock_mempool, verify
from recover_sustained_chain import confirmed_boundary


class InterruptedWorkloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with gzip.open(
            Path(__file__).resolve().parents[1]
            / "docs/evidence/chronicle-sustained-endorsed-interruption.json.gz",
            "rt",
        ) as f:
            cls.record = json.load(f)

    def test_unobserved_slots_and_complete_window_misses(self):
        result = verify(self.record)
        self.assertEqual(result["recorded_opportunities"], 246)
        self.assertEqual(result["opportunities_without_outcomes"], 54)
        self.assertEqual(
            [w["slots_recorded"] for w in result["observed_windows"]],
            [50, 50, 50, 50, 46, 0],
        )
        self.assertTrue(result["per_window_criterion_already_missed"])
        tail = result["observed_empty_pool_suffix"]
        self.assertEqual(tail["empty_status_replies"], 1149)
        self.assertEqual(tail["unavailable_status_replies"], 0)
        self.assertEqual(tail["minimum_connected_peers"], 3)
        self.assertEqual(tail["last_populated_status"]["pending"], 19168)
        self.assertEqual(tail["first_empty_status"]["height"], 494)
        self.assertEqual(
            tail["intake_change_since_last_populated"]["sv"]["admitted"], 0
        )
        self.assertEqual(
            confirmed_boundary(self.record)["final_hash"],
            self.record["blocks"][-1]["hash"],
        )

    def test_archive_reoffers_only_missing_original_payments(self):
        from prove import dsha

        wires = [b"first fixture payment", b"second fixture payment"]
        ids = [dsha(tx) for tx in wires]
        payload = b"".join(sorted(ids))
        pool = {ids[0]}
        calls = []

        def rpc(method, *args):
            if method == "getrawmempool":
                return [h[::-1].hex() for h in pool]
            self.assertEqual(method, "sendrawtransactions")
            [entries] = args
            raw = [bytes.fromhex(tx["hex"]) for tx in entries]
            calls.extend(raw)
            pool.update(dsha(tx) for tx in raw)
            return {}

        stock = Mock()
        stock.rpc.side_effect = rpc
        summary = {
            "final_stock_mempool_after_interruption": 2,
            "advertised_transactions": 2,
            "sorted_internal_mempool_txids_sha256": hashlib.sha256(payload).hexdigest(),
        }
        frames = bytes(24) + wires[0] + bytes(24) + wires[1]
        offsets = [0, 24 + len(wires[0]), len(frames)]
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("interrupted_workload.verify", return_value=summary),
        ):
            path = Path(tmp)
            private = path / "pending.gz"
            private.write_bytes(gzip.compress(payload))
            (path / "ids.bin").write_bytes(b"".join(ids))
            with patch(
                "sustained_block_archive.signed_frames",
                return_value=contextlib.nullcontext((frames, offsets)),
            ):
                result = restore_stock_mempool(
                    stock, {"source": {}}, path, private, allow_retained=True
                )
            self.assertEqual(
                (
                    result["retained_initial_count"],
                    result["reoffered"],
                    result["restored_count"],
                ),
                (1, 1, 2),
            )
            self.assertEqual(calls, [wires[1]])
            pool.add(b"x" * 32)
            with self.assertRaisesRegex(ValueError, "absent from the original pool"):
                restore_stock_mempool(
                    stock, {"source": {}}, path, private, allow_retained=True
                )

    def test_changed_original_line(self):
        bad = copy.deepcopy(self.record)
        bad["slots"][0]["ready"] = False
        with self.assertRaisesRegex(ValueError, "original source log"):
            verify(bad)

    def test_late_membership_must_agree(self):
        bad = copy.deepcopy(self.record)
        bad["after_interruption"]["queue_readbacks"][1]["stock"]["pending"] -= 1
        with self.assertRaisesRegex(ValueError, "mempool readbacks"):
            verify(bad)
