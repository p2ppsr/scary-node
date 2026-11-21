"""Compare witnessed transaction and coinbase finality at the regtest BIP113 boundary."""

import argparse
import contextlib
import hashlib
import json
from pathlib import Path
import shutil
import tempfile

from chronicle import CTransaction, FromHex, ToHex, create_block, create_coinbase
from scary_dar import LabFeed, entrant, get_port, response, until
from sv_reference import IMAGE, Reference


def signed_payment(node, source, index, lock_time):
    raw = node.rpc(
        "createrawtransaction",
        [{"txid": source, "vout": index, "sequence": 0xFFFFFFFE}],
        {node.rpc("getnewaddress"): 0.9999},
        lock_time,
    )
    signed = node.rpc("signrawtransaction", raw)
    if not signed["complete"]:
        raise RuntimeError("wallet did not finish signing the payment")
    tx = FromHex(CTransaction(), signed["hex"])
    assert tx.nLockTime == lock_time and tx.vin[0].nSequence == 0xFFFFFFFE
    return tx


def run(binary, directory):
    with (
        Reference().running(peer=True, fast_rpc=True) as full,
        contextlib.ExitStack() as life,
    ):
        full.rpc("generate", 562)
        feed = LabFeed(full, directory)
        life.callback(feed.server.server_close)
        life.callback(feed.server.shutdown)
        api = life.enter_context(
            entrant(binary, full, feed, directory, get_port(), get_port())
        )

        addresses = [full.rpc("getnewaddress") for _ in range(2)]
        funding = full.rpc("sendmany", "", {address: 1 for address in addresses})
        outputs = full.rpc("getrawtransaction", funding, 1)["vout"]
        indices = [
            next(
                o["n"]
                for o in outputs
                if address in o["scriptPubKey"].get("addresses", [])
            )
            for address in addresses
        ]
        history = []
        rows = []
        for height in range(563, 578):
            parent = full.rpc("getblockheader", full.rpc("getbestblockhash"))
            assert parent["height"] + 1 == height
            payment = None
            if height == 563:
                tip = full.rpc("generate", 1)[0]
            else:
                # Advance the block clock at a measured pace so the block time
                # and the preceding eleven-header median differ at the boundary.
                block_time = parent["time"] + 8
                cutoff = block_time if height < 576 else parent["mediantime"]
                coinbase = create_coinbase(height)
                if height == 577:
                    # The stock regtest BIP34 height is 100,000,000. This
                    # coinbase identifies its height solely through the parent.
                    coinbase.vin[0].scriptSig = b"\x6a\x00"
                coinbase.nLockTime = cutoff - 1
                coinbase.vin[0].nSequence = 0xFFFFFFFE
                coinbase.rehash()
                block = create_block(int(parent["hash"], 16), coinbase, block_time)
                block.nVersion = 1
                if height in (575, 576):
                    lock = cutoff - 1
                    if height == 575:
                        assert lock >= parent["mediantime"]
                    payment = signed_payment(full, funding, indices[height - 575], lock)
                    block.vtx.append(payment)
                block.hashMerkleRoot = block.calc_merkle_root()
                block.solve()
                disposition = full.rpc("submitblock", ToHex(block))
                assert disposition is None, disposition
                tip = block.hash
            assert full.rpc("getbestblockhash") == tip
            until(
                lambda: response(api + "/status")["mining"]["tip_hash"] == tip,
                f"height {height}",
            )
            if 564 <= height <= 574:
                history.append(full.rpc("getblockheader", tip, False))
            if height >= 575:
                observation = response(api + "/status")["mining"]["validation_window"][
                    -1
                ]
                assert observation["height"] == height and observation["hash"] == tip
                assert observation["coinbase_finality_checked"]
                if payment:
                    assert observation["transactions_determinate"] == 1
                    assert (
                        observation["known_inputs"]
                        == observation["script_checks_passed"]
                        == 1
                    )
                    assert observation["unknown_inputs"] == 0
                rows.append(
                    {
                        "height": height,
                        "parent_hash": parent["hash"],
                        "sv_active_hash": tip,
                        "block_hex": ToHex(block),
                        "scary_active_hash": observation["hash"],
                        "header_version": block.nVersion,
                        "parent_median_time": parent["mediantime"],
                        "header_time": block_time,
                        "coinbase_lock_time": coinbase.nLockTime,
                        "coinbase_sequence": coinbase.vin[0].nSequence,
                        "payment_txid": payment.hash if payment else None,
                        "payment_lock_time": payment.nLockTime if payment else None,
                        "observation": observation,
                    }
                )
    return {
        "reference": IMAGE,
        "network": "regtest",
        "chronicle_height": 2,
        "csv_height": 576,
        "funding_height": 563,
        "funding_txid": funding,
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "headers_before_cases": history,
        "rows": rows,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("receipt", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as d:
        binary = Path(d) / "scary-node"
        shutil.copy2(args.binary, binary)
        data = run(binary, Path(d))
    args.receipt.write_text(json.dumps(data, indent=2) + "\n")
