"""Exercise bounded observer reads against archived native bytes and explicit partial responses."""

import http.client
import time
import unittest
from collections import Counter

from cohort_archive import EVIDENCE
from cohort_estimate import dsha, read
from matched_continuation import stock_readbacks
from matched_study import verify_attempts
from sv_reference import RpcError


class ReceiverObservationTests(unittest.TestCase):
    def test_retry_identical_hash_and_preserve_both_exhausted_observations(self):
        raw = bytes.fromhex(
            read(EVIDENCE / "chronicle-matched-native-continuation.json")["rows"][0][
                "raw_hex"
            ][0]
        )
        known = dsha(raw[:80])[::-1].hex()
        absent = "ef" * 32
        calls = Counter()

        def rpc(node, method, args):
            key = node, method, tuple(args)
            calls[key] += 1
            if method == "getbestblockhash" and node == 0:
                return known
            if (
                method == "getblock"
                and args == [known, 0]
                and node == 0
                and calls[key] == 2
            ):
                return raw.hex()
            if method == "getblock" and args == [absent, 0] and node == 0:
                raise RpcError('{"error":{"code":-5,"message":"Block not found"}}')
            raise http.client.IncompleteRead(b'{"result":  ')

        receipts = stock_readbacks(
            [0, 1], [known, absent], time.monotonic_ns(), rpc=rpc
        )
        self.assertEqual(receipts[0]["active_hash"], known)
        self.assertIsNone(receipts[1]["active_hash"])
        self.assertEqual(
            verify_attempts(receipts[1]["active_receipt"], 1),
            "transport_or_encoding_error",
        )
        self.assertEqual(verify_attempts(receipts[0]["bodies"][known], 2), "observed")
        self.assertEqual(verify_attempts(receipts[0]["bodies"][absent], 2), "rpc_error")
        for h in (known, absent):
            self.assertEqual(
                verify_attempts(receipts[1]["bodies"][h], 2),
                "transport_or_encoding_error",
            )
            self.assertEqual(calls[1, "getblock", (h, 0)], 2)
        self.assertEqual(calls[1, "getbestblockhash", ()], 1)
        self.assertEqual(calls[0, "getblock", (absent, 0)], 1)
        receipts[1]["bodies"][known]["attempts"].pop()
        with self.assertRaises(ValueError):
            verify_attempts(receipts[1]["bodies"][known], 2)
