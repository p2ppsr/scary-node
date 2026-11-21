"""Replay previously published blocks on local socket pairs; no new study or race sample."""

import concurrent.futures
import contextlib
import copy
import socket
import struct
import time
import unittest
from collections import Counter

from cohort_archive import EVIDENCE
from cohort_estimate import decode_block, dsha, read
from matched_propagation import (
    align,
    check_session,
    close_session,
    frame,
    gate_agent,
    opportunity,
    paired_delay_effect,
    receive,
    receiver_roster,
    sample_jobs,
    settle,
    verify,
)


class ArchivedJob:
    def __init__(self, raw, source, emit):
        self.raw = raw
        self.source = source
        self.emit = emit
        _, (self.coinbase, self.payment), _ = decode_block(
            raw, dsha(raw[:80])[::-1].hex()
        )

    def header(self, nonce):
        return self.raw[:76] + struct.pack("<I", nonce)

    def submit_native(self, nonce):
        assert self.header(nonce) == self.raw[:80]
        if self.emit:
            inv = b"\1" + struct.pack("<I", 2) + dsha(self.raw[:80])
            self.source.sendall(frame("inv", inv))
            assert receive(self.source, time.monotonic_ns() + 2_000_000_000) == (
                "getdata",
                inv,
            )
            self.source.sendall(frame("block", self.raw))
        else:
            self.source.shutdown(socket.SHUT_RDWR)
        return True


class PropagationMethodTests(unittest.TestCase):
    def replay(self, missing=False, unready=False):
        receipt = read(EVIDENCE / "chronicle-relay-comparison/immediate/62001.json.gz")
        idx = next(
            i
            for i, row in enumerate(receipt["rows"])
            if all(p["discovery"] for p in row["producers"])
        )
        original = receipt["rows"][idx]
        plan = {
            **receipt["planned"][idx],
            "job_response_window_ns": 500_000_000,
            "first_lane": 1,
            "first_receiver": [1, 0],
            "timing": {
                "hold_ns": 70_000_000,
                "offsets_ns": [0, 12_000_000],
                "tolerance_ns": 500_000_000,
                "capture_window_ns": 180_000_000,
                "pong_timeout_ns": 140_000_000,
            },
        }
        blocks = [
            bytes.fromhex(p["discovery"]["wire_hex"]) for p in original["producers"]
        ]

        def receiver(s, expected):
            end = time.monotonic_ns() + 2_000_000_000
            kind, inv = receive(s, end)
            self.assertEqual(
                (kind, inv), ("inv", b"\1" + struct.pack("<I", 2) + dsha(expected[:80]))
            )
            self.assertEqual(receive(s, end), ("block", expected))
            name, nonce = receive(s, end)
            self.assertEqual(name, "ping")
            s.sendall(frame("pong", nonce))

        with (
            contextlib.ExitStack() as stack,
            concurrent.futures.ThreadPoolExecutor(4) as pool,
        ):

            def pair():
                a, b = socket.socketpair()
                return stack.enter_context(a), stack.enter_context(b)

            inputs = [pair(), pair()]
            destinations = [[pair(), pair()], [pair(), pair()]]
            jobs = [
                ArchivedJob(b, p[1], not (missing and i == 0))
                for i, (b, p) in enumerate(zip(blocks, inputs))
            ]
            if unready:
                jobs[0] = None
            waiting = [
                pool.submit(receiver, p[1], blocks[i])
                for i, lane in enumerate(destinations)
                for p in lane
                if i == 1 or not (missing or unready)
            ]
            row = opportunity(
                plan,
                sample_jobs(
                    plan,
                    time.monotonic_ns() - plan["offer_delay_ms"] * 1_000_000,
                    [
                        lambda: (
                            jobs[0],
                            "published fixture lacks a job" if unready else None,
                        ),
                        lambda: (jobs[1], None),
                    ],
                ),
                [p[0] for p in inputs],
                [[p[0] for p in lane] for lane in destinations],
                parent=original["parent"],
                height=original["height"],
                payment_txid=original["payment_txid"],
            )
            for f in waiting:
                f.result()
        verify(row)
        return row

    def test_native_bytes_fan_out_to_both_receivers(self):
        row = self.replay()
        self.assertEqual([len(p["legs"]) for p in row["lanes"]], [2, 2])
        self.assertTrue(all(p["native_submit"]["ack"] for p in row["lanes"]))
        self.assertEqual(
            len({l["pong_nonce"] for p in row["lanes"] for l in p["legs"]}), 4
        )
        changed = copy.deepcopy(row)
        changed["lanes"][1]["legs"][1]["pong_ns"] = 0
        with self.assertRaisesRegex(ValueError, "pong receipt"):
            verify(changed)

    def test_missing_publication_and_unready_lane_remain(self):
        row = self.replay(missing=True)
        self.assertTrue(row["lanes"][0]["discovery"])
        self.assertIn("capture_error", row["lanes"][0])
        self.assertEqual(
            [a["status"] for a in row["ingress_alignment"]],
            ["missing_native_body_or_fence"] * 2,
        )
        row = self.replay(unready=True)
        self.assertEqual(row["lanes"][0]["hashes"], 0)
        self.assertFalse(row["lanes"][0]["ready"])
        self.assertEqual(
            [a["status"] for a in row["ingress_alignment"]],
            ["single_or_no_discovery"] * 2,
        )

    def test_certification_uses_worst_case_skew_and_common_staging_cutoff(self):
        def lane(start, finish, available):
            leg = {
                "native_capture_ns": available,
                "write_begin_ns": start,
                "write_end_ns": start + 1,
                "pong_ns": finish,
                "ping_nonce": "01" * 8,
                "pong_nonce": "01" * 8,
                "planned_release_ns": start,
            }
            return {
                "discovery": {"hash": "test"},
                "native_capture_ns": available,
                "legs": [leg.copy(), leg.copy()],
            }

        row = {
            "tolerance_ns": 2,
            "hold_ns": 20,
            "offsets_ns": [0, 10],
            "lanes": [lane(20, 21, 12), lane(30, 31, 15)],
        }
        self.assertEqual(align(row)[0]["left_minus_right_ingress_bounds_ns"], [-11, -9])
        self.assertEqual(align(row)[0]["status"], "certified")
        row["lanes"][1]["native_capture_ns"] = 25
        self.assertEqual(align(row)[0]["status"], "timing_not_certified")
        row["lanes"][1]["native_capture_ns"] = 12
        row["lanes"][0]["legs"][0]["pong_ns"] = 30
        self.assertEqual(align(row)[0]["status"], "timing_not_certified")

    def test_delay_effect_keeps_both_roles_and_treatments_in_each_session(self):
        def session():
            out = {}
            for d, stale in [(-10, (2, 8)), (0, (5, 5)), (10, (7, 3))]:
                out[f"full/full:{d}ns"] = {
                    "parents": 12,
                    "lanes": [
                        Counter(
                            scheduled=12,
                            ready=11,
                            hashes=12,
                            discovered=10,
                            settled=10 - x,
                            stale=x,
                        )
                        for x in stale
                    ],
                }
            return out

        samples = {k: session() for k in ("fixture-a", "fixture-b", "fixture-c")}
        with self.assertRaisesRegex(ValueError, "incomplete"):
            paired_delay_effect(samples, ["fixture-a", "fixture-b"], "full/full", 10)
        result = paired_delay_effect(samples, list(samples), "full/full", 10)
        self.assertAlmostEqual(
            result["left_delayed_minus_right_delayed_stale_difference"]["difference"], 1
        )
        self.assertEqual(
            result["by_assigned_delay"]["full/full:0ns"]["per_scheduled"]["ready"][
                "rates"
            ],
            [11 / 12, 11 / 12],
        )

    def test_receiver_roster_requires_only_the_four_registered_native_legs(self):
        class Receiver:
            def __init__(self, idx):
                self.peers = [
                    {"id": 9 - i, "subver": gate_agent(f"l{i}-r{idx}")}
                    for i in range(2)
                ]

            def rpc(self, method):
                return self.peers if method == "getpeerinfo" else {"chain": "regtest"}

        receivers = [Receiver(0), Receiver(1)]
        self.assertEqual(receiver_roster(receivers)[1][0]["agent"], gate_agent("l0-r1"))
        receivers[0].peers.append({"id": 10, "subver": "/unplanned-stock-peer/"})
        with self.assertRaisesRegex(ValueError, "unexpected or missing"):
            receiver_roster(receivers)

    def test_two_unready_lanes_survive_settlement_and_the_session_audit(self):
        # Replay the existing four-header prefix; generation advances an index only.
        evidence = read(EVIDENCE / "chronicle-retained-delivery.json")
        ids = evidence["ordered_block_ids"][:4]
        hdrs = evidence["ordered_header_hex"][:4]

        class ReplayedStock:
            def __init__(self, idx):
                self.idx, self.offset = idx, 0

            def rpc(self, cmd, *args):
                if cmd == "getblockchaininfo":
                    return {
                        "chain": "regtest",
                        "blocks": 107 + self.offset,
                        "bestblockhash": ids[self.offset],
                    }
                if cmd == "generate":
                    self.offset += args[0]
                    return ids[1 : self.offset + 1]
                if cmd == "getbestblockhash":
                    return ids[self.offset]
                if cmd == "getblockhash":
                    return ids[args[0] - 107]
                if cmd == "getblockheader":
                    return hdrs[ids.index(args[0])]
                if cmd == "getpeerinfo":
                    return [
                        {"id": i + 1, "subver": gate_agent(f"l{i}-r{self.idx}")}
                        for i in range(2)
                    ]
                raise AssertionError(cmd)

        stocks = [ReplayedStock(0), ReplayedStock(1)]
        plan = {
            "index": 0,
            "arm": "witnessed/full",
            "nonce": 0,
            "swap_nonce": False,
            "first_lane": 0,
            "first_receiver": [0, 1],
            "offer_delay_ms": 0,
            "successor_receiver": 1,
            "job_response_window_ns": 500_000_000,
            "timing": {
                "hold_ns": 0,
                "offsets_ns": [0, 0],
                "tolerance_ns": 1_000_000,
                "capture_window_ns": 2_000_000,
                "pong_timeout_ns": 2_000_000,
            },
        }
        roster = receiver_roster(stocks)
        sampled = sample_jobs(
            plan, time.monotonic_ns(), [lambda: (None, "unready replay")] * 2
        )
        with contextlib.ExitStack() as stack:
            conns = []
            for _ in range(6):
                a, b = socket.socketpair()
                conns.append(stack.enter_context(a))
                stack.enter_context(b)
            row = opportunity(
                plan,
                sampled,
                conns[:2],
                [conns[2:4], conns[4:6]],
                parent=ids[0],
                height=108,
                payment_txid="0" * 64,
            )
        row["receiver_peers_before_offer"] = roster
        settle(row, stocks, lambda: setattr(stocks[0], "offset", 3))
        got = close_session([plan], [row], stocks)["witnessed/full:0ns"]
        self.assertEqual(
            [(c["scheduled"], c["ready"], c["stale"]) for c in got["lanes"]],
            [(1, 0, 0)] * 2,
        )
        with self.assertRaisesRegex(ValueError, "missing scheduled"):
            check_session([plan], [])


if __name__ == "__main__":
    unittest.main()
