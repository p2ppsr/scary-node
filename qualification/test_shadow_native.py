"""Keep the unsigned candidate, its successor, and proven native funding distinct."""

import copy
import gzip
import json
import struct
import unittest
from pathlib import Path
from unittest.mock import patch

from shadow import digest, merkle
from shadow_first_candidate import assess, first_decision
from shadow_native import inputs, txid, verify_fees, verify_funding
from shadow_native_warmup import verify

EVIDENCE = Path(__file__).resolve().parents[1] / "docs" / "evidence"


def fixture_header(raw, height):
    # The 146 synthetic descendants are inexpensive test headers. Merkle proofs
    # for the two actual mainnet transactions still undergo the normal PoW checks.
    return {
        "height": height,
        "hash": txid(raw),
        "previous": raw[4:36][::-1].hex(),
        "bits": int.from_bytes(raw[72:76], "little"),
    }


def fixture():
    row = json.loads(
        gzip.decompress(
            (EVIDENCE / "chronicle-shadow-native-fee-warmup.json.gz").read_bytes()
        )
    )
    funding, spending = row["proofs"]
    fhead = bytes.fromhex(funding["header"])
    block = txid(fhead)
    tx = spending["transactions"][row["spend"]]["tx"]
    points, _ = inputs(bytes.fromhex(tx))
    src = verify_funding(funding, block)
    previous = {}
    for t, n in points:
        value, script = src[t][n]
        previous[f"{t}:{n}"] = {
            "height": funding["height"],
            "block": block,
            "value": value,
            "script": script.hex(),
            "coinbase": funding["transactions"][t]["index"] == 0,
            "confiscation": False,
        }
    (fee,) = verify_fees([tx], previous)
    bits = 0x1D00FFFF
    stamp = int.from_bytes(fhead[68:72], "little")
    headers = [fhead]
    for i in range(1, 147):
        headers.append(
            struct.pack("<i", 4)
            + digest(headers[-1])
            + bytes(32)
            + struct.pack("<III", stamp + i * 600, bits, 0)
        )
    parent = txid(headers[-1])
    n = funding["height"] + 147
    now = stamp + 147 * 600
    payout = ((50 * 10**8) >> (n // 210000)) + fee
    coinbase = (
        b"\x01\0\0\0\x01"
        + bytes(32)
        + bytes.fromhex("ffffffff0403")
        + n.to_bytes(3, "little")
        + bytes.fromhex("ffffffff01")
        + struct.pack("<Q", payout)
        + bytes.fromhex("015100000000")
    )
    root = merkle([digest(coinbase), digest(bytes.fromhex(tx))])
    header = (
        struct.pack("<i", 4)
        + digest(headers[-1])
        + root
        + struct.pack("<III", now, bits, 0)
    )
    successor = header[:36] + bytes(32) + header[68:]
    return {
        "schema": 1,
        "decision": {
            "ready": True,
            "coverage": 147,
            "pending": 1,
            "observed_at": now,
            "height": n - 1,
            "hash": parent,
            "header_hex": headers[-1].hex(),
            "expected_next_bits": bits,
            "unsigned_candidate": {
                "height": n,
                "coinbase": coinbase.hex(),
                "transactions": [row["spend"]],
                "header_hex": header.hex(),
                "bits": bits,
            },
            "directives": {
                "ready": True,
                "connected": True,
                "genesis_verified": True,
                "last_applied": 1,
                "last_verified": 1,
                "reported_sequence": 1,
                "last_peer_sync": {"sequence": 1, "unix_s": now - 10},
            },
        },
        "clock": {
            "at": now,
            "hash": parent,
            "height": n - 1,
            "clock_step": 0,
            "clock_step_limit": 300,
            "peer_clock": {
                "connected_network_groups": 5,
                "minimum_groups": 3,
                "disagreeing_samples": False,
                "offset_from_wall_seconds": 0,
                "hold_threshold_seconds": 300,
            },
        },
        "raw_transactions": [tx],
        "admitted_fees": [fee],
        "witnessed": previous,
        "funding_proofs": {block: funding},
        "headers": [h.hex() for h in headers],
        "successor": {
            "observed_at": now,
            "height": n,
            "hash": txid(successor),
            "header_hex": successor.hex(),
        },
    }


class NativeQualificationTests(unittest.TestCase):
    def test_confirmed_native_warmup_and_tampered_merkle(self):
        raw = json.loads(
            gzip.decompress(
                (EVIDENCE / "chronicle-shadow-native-fee-warmup.json.gz").read_bytes()
            )
        )
        self.assertEqual(verify(raw)["fee_satoshis"], 64)
        tx = raw["proofs"][0]["transactions"]
        key = next(iter(tx))
        tx[key]["branch"][0] = "00" * 32
        with self.assertRaisesRegex(ValueError, "Merkle"):
            verify(raw)

    @patch("shadow_first_candidate.checked_header", side_effect=fixture_header)
    def test_complete_inputs_require_funding_clock_and_immediate_successor(self, _):
        r = fixture()
        self.assertTrue(assess(r)["complete"])
        for field, val in (
            ("funding_proofs", {}),
            ("clock", None),
            ("successor", None),
        ):
            with self.subTest(field=field):
                self.assertFalse(assess(dict(r, **{field: val}))["complete"])
        r["clock"]["peer_clock"]["connected_network_groups"] = 2
        self.assertFalse(assess(r)["prospective_peer_clock_confirmed"])
        r["successor"]["hash"] = "00" * 32
        with self.assertRaisesRegex(ValueError, "next observed header"):
            assess(r)

    @patch("shadow_first_candidate.checked_header", side_effect=fixture_header)
    def test_native_amount_script_fees_and_source_branch_are_bound(self, _):
        r = fixture()
        for field, value in (
            ("script", "00"),
            ("coinbase", True),
            ("height", r["decision"]["height"]),
        ):
            wrong = copy.deepcopy(r)
            next(iter(wrong["witnessed"].values()))[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                assess(wrong)
        with self.assertRaisesRegex(ValueError, "per-payment fees"):
            assess(dict(r, admitted_fees=[1]))
        with self.assertRaisesRegex(ValueError, "duplicate candidate transaction"):
            verify_fees(r["raw_transactions"] * 2, r["witnessed"])
        with self.assertRaisesRegex(ValueError, "unused external"):
            verify_fees(r["raw_transactions"], {**r["witnessed"], "unused": {}})
        mutated = copy.deepcopy(r)
        terminal = bytearray.fromhex(mutated["headers"][-2])
        terminal[4:36] = bytes(32)
        mutated["headers"][-2] = terminal.hex()
        with self.assertRaisesRegex(ValueError, "gap"):
            assess(mutated)

    @patch("shadow_first_candidate.checked_header", side_effect=fixture_header)
    def test_two_hour_limit_uses_corroborated_clock_and_timed_successor(self, _):
        r = fixture()
        candidate = r["decision"]["unsigned_candidate"]
        wire = bytearray.fromhex(candidate["header_hex"])
        wire[68:72] = struct.pack("<I", r["decision"]["observed_at"] + 7190)
        candidate["header_hex"] = wire.hex()
        r["clock"]["peer_clock"]["offset_from_wall_seconds"] = -20
        with self.assertRaisesRegex(ValueError, "corroborated future limit"):
            assess(r)
        r["clock"]["peer_clock"]["offset_from_wall_seconds"] = 0
        self.assertTrue(assess(r)["complete"])
        r["successor"]["observed_at"] -= 7201
        with self.assertRaisesRegex(ValueError, "successor time"):
            assess(r)

    def test_selection_preserves_the_original_even_after_a_gap(self):
        rows = [
            {"unsigned_candidate": {"transactions": []}},
            {"unsigned_candidate": {"transactions": ["a"]}},
            {"unsigned_candidate": None},
            {"unsigned_candidate": {"transactions": ["b"]}},
        ]
        self.assertEqual(first_decision(rows), 1)


if __name__ == "__main__":
    unittest.main()
