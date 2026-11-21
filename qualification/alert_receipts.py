"""Compare applied, repeated and outstanding signed actions against SV Node RPC."""

import argparse
import contextlib
import hashlib
import json
from pathlib import Path
import shutil
import struct
import tempfile
import urllib.request

from native_cohort import funding_transactions
from run_cohort import session_lock
from scary_dar import LabFeed, entrant, get_port, response, until
from sv_dar import confiscate
from sv_reference import IMAGE, Reference


def action_receipt(api, method, params):
    request = urllib.request.Request(
        api + "/alert-receipt",
        data=json.dumps({"method": method, "params": params, "id": 1}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=5) as reply:
        call = json.load(reply)
    assert call["error"] is None, call
    return call["result"]


def run(binary, directory):
    with (
        session_lock(),
        Reference().running(peer=True, fast_rpc=True) as full,
        contextlib.ExitStack() as life,
    ):
        full.rpc("generate", 105)
        feed = LabFeed(full, directory)
        life.callback(feed.server.server_close)
        life.callback(feed.server.shutdown)
        port, worker = get_port(), get_port()
        journal = directory / "state" / "regtest" / "alerts"

        def operator(require_directives=True):
            return entrant(
                binary,
                full,
                feed,
                directory,
                port,
                worker,
                require_directives=require_directives,
            )

        with operator() as api:
            funding_raw, (payment, _) = funding_transactions(full, 2)
            txid = full.rpc("sendrawtransaction", funding_raw)
            first = full.rpc("generate", 1)[0]
            until(
                lambda: response(api + "/status")["mining"]["tip_hash"] == first,
                "funding",
            )
            spend = full.rpc("sendrawtransaction", payment.hex())
            tip = full.rpc("generate", 1)[0]
            until(
                lambda: response(api + "/status")["mining"]["tip_hash"] == tip,
                "payment",
            )
            assert spend in full.rpc("getblock", tip)["tx"]
            start = full.rpc("getblockcount")
            stop = start + 10
            method = "addToConsensusBlacklist"
            freeze_rows = []
            for seq, indices in [(1, [0]), (2, [0, 1])]:
                funds = [
                    {
                        "txOut": {"txId": txid, "vout": i},
                        "enforceAtHeight": [{"start": start, "stop": stop}],
                        "policyExpiresWithConsensus": True,
                    }
                    for i in indices
                ]
                expected = full.rpc(method, {"funds": funds})
                envelope = b"".join(
                    bytes.fromhex(txid) + struct.pack("<QQQB", i, start, stop, 1)
                    for i in indices
                )
                assert feed.add(2, envelope) == seq
                until(
                    lambda: (
                        response(api + "/status")["mining"]["directives"][
                            "last_applied"
                        ]
                        == seq
                    ),
                    f"durable directive {seq}",
                )
                assert (
                    full.rpc("getbestblockhash")
                    == response(api + "/status")["mining"]["tip_hash"]
                    == tip
                )
                actual = action_receipt(api, method, [{"funds": funds}])
                receipt = json.loads((journal / f"{seq:010}.receipt").read_text())
                assert actual == expected == receipt["rpc_result"]
                freeze_rows.append(
                    {
                        "sequence": seq,
                        "sv": expected,
                        "scary": actual,
                        "receipt": receipt,
                    }
                )
            reason = {
                "txOut": {"txId": txid, "vout": 0},
                "reason": "already in consensus",
            }
            assert freeze_rows[-1]["sv"] == {"notProcessed": [reason]}

            # Validate the signed authorization request only. Both nodes have
            # a known third output that was never included in a freeze.
            raw = confiscate(txid, 2, bytes.fromhex("51"))
            params = [
                {
                    "confiscationTxs": [
                        {"confiscationTx": {"enforceAtHeight": start, "hex": raw}}
                    ]
                }
            ]
            method = "addToConfiscationTxidWhitelist"
            full_error = full.rpc(method, *params)
            tx = bytes.fromhex(raw)
            assert len(tx) < 253
            feed.add(4, struct.pack("<Q", start) + bytes([len(tx)]) + tx)
            failed_path = journal / "0000000003.failure"
            until(failed_path.exists, "durable per-item failure")
            error = action_receipt(api, method, params)
            failure = json.loads(failed_path.read_text())
            assert failure["rpc_result"] == error == full_error
            assert (
                error["notProcessed"][0]["reason"]
                == "confiscated TXO is not consensus frozen"
            )
            state = response(api + "/status")["mining"]["directives"]
            assert (
                state["last_applied"] == 2
                and state["last_verified"] == 3
                and not state["ready"]
            )
            assert not (journal / "0000000003.receipt").exists()
        with operator(require_directives=False) as api:
            until(
                lambda: (
                    response(api + "/status")["mining"]["directives"]["last_verified"]
                    == 3
                ),
                "recovery of the signed outstanding action",
            )
            recovered = response(api + "/status")["mining"]["directives"]
            assert recovered["last_applied"] == 2 and not recovered["ready"]
            assert action_receipt(api, method, params) == error
            assert json.loads(failed_path.read_text()) == failure
            assert not (journal / "0000000003.receipt").exists()
        return {
            "reference": IMAGE,
            "network": "regtest",
            "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            "funding_txid": txid,
            "payment_txid": spend,
            "accepted_tip": tip,
            "height": start,
            "freeze_results": freeze_rows,
            "unapplied": {
                "sv": full_error,
                "scary": error,
                "failure": failure,
                "last_verified": state["last_verified"],
                "last_applied": state["last_applied"],
                "ready": state["ready"],
                "after_restart": {
                    "last_verified": recovered["last_verified"],
                    "last_applied": recovered["last_applied"],
                    "ready": recovered["ready"],
                },
            },
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("receipt", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as d:
        binary = Path(d) / "scary-node"
        shutil.copy2(args.binary, binary)
        result = run(binary, Path(d))
    args.receipt.write_text(json.dumps(result, indent=2) + "\n")
