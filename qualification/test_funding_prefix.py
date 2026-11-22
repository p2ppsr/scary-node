import copy
import json
import unittest
from pathlib import Path
from unittest.mock import call, patch
from urllib.error import HTTPError

from teranode_funding_prefix import stock_body, verify


class AssetPacingTest(unittest.TestCase):
    def test_follows_stock_retry_after_and_propagates_other_failures(self):
        for code in (429, 404):
            with self.subTest(code=code):
                error = RuntimeError("asset response")
                error.__cause__ = HTTPError(
                    "regtest", code, "status", {"Retry-After": "3"}, None
                )
                self.addCleanup(error.__cause__.close)
                with (
                    patch(
                        "teranode_funding_prefix.request", side_effect=[error, b"body"]
                    ),
                    patch("teranode_funding_prefix.time.sleep") as pause,
                ):
                    if code == 429:
                        self.assertEqual(stock_body("regtest"), b"body")
                        self.assertEqual(pause.call_args_list, [call(0.125), call(3)])
                    else:
                        with self.assertRaisesRegex(RuntimeError, "asset response"):
                            stock_body("regtest")
                        pause.assert_called_once_with(0.125)


class StockFundingPrefixTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        evidence = Path(__file__).resolve().parents[1] / "docs/evidence"
        cls.source = evidence / "chronicle-waiting-shutdown-dar-history.json.gz"
        receipt = evidence / "chronicle-waiting-shutdown-teranode-prefix.json"
        cls.result = json.loads(receipt.read_bytes())

    def test_original_signed_freeze_and_every_intermediate_body(self):
        observed = verify(self.source, self.result)
        self.assertEqual(observed["asset_readbacks"], 214)
        self.assertEqual(observed["common_height"], 107)
        self.assertEqual(observed["frozen_teranodes"], 2)

    def test_each_stock_observation_is_required(self):
        for name in ("tn1", "tn2"):
            for field, reason in (
                ("asset_readbacks", "chain readbacks"),
                ("selected_parent", "chain readbacks"),
                ("before_freeze", "funded output states"),
                ("after_freeze", "funded output states"),
                ("processed_records", "stored signed prefix"),
            ):
                with self.subTest(node=name, field=field):
                    result = copy.deepcopy(self.result)
                    peer = result["teranodes"][name]
                    if field == "asset_readbacks":
                        peer[field][53]["wire_sha256"] = "00" * 32
                    elif field == "selected_parent":
                        peer[field] = "00" * 32
                    elif field == "processed_records":
                        peer[field][0]["processed"] = False
                    else:
                        peer[field]["status"] = 3
                    with self.assertRaisesRegex(ValueError, reason):
                        verify(self.source, result)

    def test_funding_outpoint_interval_and_signed_receipt_must_match(self):
        for field, reason in (
            ("txid", "funding boundary"),
            ("freeze_interval", "different freeze interval"),
            ("signed_record", "stored signed prefix"),
        ):
            with self.subTest(field=field):
                result = copy.deepcopy(self.result)
                if field == "txid":
                    result["funding_boundary"]["txid"] = "00" * 32
                elif field == "freeze_interval":
                    result["sv"]["after_freeze"]["funds"][0]["enforceAtHeight"][0][
                        "start"
                    ] += 1
                else:
                    result["teranodes"]["tn1"]["processed_records"][0]["raw"] = ""
                with self.assertRaisesRegex(ValueError, reason):
                    verify(self.source, result)
