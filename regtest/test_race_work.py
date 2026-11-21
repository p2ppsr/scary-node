"""Offline work/accounting tests using a saved, accepted two-transaction regtest block."""
import json
import struct
import unittest
from pathlib import Path
from unittest.mock import Mock

from prove import decode_block, txid
from race_work import REGTEST_BITS, RpcObservation, checked_job, race_pair


class PairedWorkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = Path(__file__).resolve().parents[1] / "docs/evidence/regtest-integration.json"
        record = json.loads(fixture.read_text())
        raw = bytes.fromhex(record["candidate_block_hex"])
        _, cls.txs, _ = decode_block(raw, record["solved_hash"])
        cls.version = struct.unpack_from("<I", raw, 0)[0]
        cls.time = struct.unpack_from("<I", raw, 68)[0]
        cls.parent = record["parent"]

    def jobs(self):
        return [checked_job(name, "fixture", name, self.parent, self.version, self.time,
                            REGTEST_BITS, *self.txs, Mock(return_value=True))
                for name in ("left", "right")]

    def test_first_joint_hit_submits_both_committed_bodies_after_all_proposals(self):
        calls = []
        def proposal(raw):
            decode_block(raw, txid(raw[:80]))
            calls.append(("proposal", raw[:80]))
        def relay(raw):
            self.assertEqual(sum(kind == "proposal" for kind, _ in calls), 2)
            calls.append(("relay", raw[:80]))
        jobs = self.jobs()
        record = race_pair(*jobs, 0, relay, preflight=proposal, swap_lanes=True, swap_dispatch=True)
        self.assertEqual(record["attempts_per_arm"], 2)
        self.assertEqual(record["dispatch_order"], ["right", "left"])
        self.assertEqual([a["last_nonce"] for a in record["arms"]], [3, 2])
        self.assertEqual(len(calls), 4)
        for job, arm in zip(jobs, record["arms"]):
            self.assertTrue(arm["solved"])
            self.assertTrue(arm["relay"]["ok"])
            self.assertGreaterEqual(arm["relay"]["started_ns"],
                                    max(a["proposal"]["finished_ns"] for a in record["arms"]))
            raw = bytes.fromhex(arm["block_hex"])
            self.assertEqual(decode_block(raw, arm["hash"])[1], self.txs)
            job.submit_native(arm["last_nonce"])
            job._submit.assert_called_once_with(arm["last_nonce"])

    def test_single_first_hit_never_submits_unsolved_peer_or_relabels_censor(self):
        relay, propose = Mock(return_value=None), Mock(return_value=None)
        rec = race_pair(*self.jobs(), 1, relay, preflight=propose)
        self.assertEqual(rec["attempts_per_arm"], 1)
        self.assertEqual([a["solved"] for a in rec["arms"]], [False, True])
        self.assertIsNone(rec["arms"][0]["header_hex"])
        relay.assert_called_once()
        propose.assert_called_once()
        inactive = race_pair(*self.jobs(), 0, relay, preflight=propose, max_ticks=1)
        self.assertEqual(inactive["attempts_per_arm"], 1)
        self.assertEqual(inactive["dispatch_order"], [])
        self.assertFalse(any(a["solved"] for a in inactive["arms"]))
        relay.assert_called_once()

    def test_proxy_clock_and_independent_dispatch_order(self):
        result = RpcObservation(None, None, "1795200000.124", "0.003")
        record = race_pair(*self.jobs(), 2, lambda raw: result, preflight=lambda raw: result,
                           swap_dispatch=True, swap_lanes=False)
        self.assertEqual(record["dispatch_order"], ["right", "left"])
        self.assertEqual([a["last_nonce"] for a in record["arms"]], [2, 3])
        for arm in record["arms"]:
            self.assertEqual(arm["relay"]["ingress_ms"], 1795200000121)
        rpc_error = RpcObservation(None, {"code": -1, "message": "lab error"}, "1795200000.124", "0.003")
        bad = race_pair(*self.jobs(), 2, Mock(), preflight=lambda raw: rpc_error)
        self.assertFalse(bad["arms"][0]["proposal"]["ok"])
        self.assertEqual(bad["arms"][0]["proposal"]["error"]["code"], -1)
        self.assertIsNone(bad["arms"][0]["relay"])

    def test_rejected_or_unavailable_preflight_leaves_complete_pair_unpublished(self):
        relay = Mock()
        for proposal in (Mock(side_effect=[None, "inconclusive"]),
                         Mock(side_effect=[None, RuntimeError("offline")])):
            rec = race_pair(*self.jobs(), 2, relay, preflight=proposal)
            self.assertEqual(rec["attempts_per_arm"], 1)
            self.assertTrue(all(a["solved"] and a["proposal"] and a["relay"] is None for a in rec["arms"]))
        relay.assert_not_called()


if __name__ == "__main__":
    unittest.main()
