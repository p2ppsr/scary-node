"""Compare upstream post-Chronicle vectors with two BDK bindings and a live SV Node."""

import argparse
import json
import os
import struct
import subprocess
import sys
from pathlib import Path
from sv_reference import Reference, RpcError, IMAGE

ROOT = Path(__file__).resolve().parents[1]
SV = ROOT / ".native" / "bitcoin-sv"
os.environ["SRCDIR"] = str(SV)
sys.path.insert(0, str(SV / "test" / "functional"))
from chronicle_upgrade_tests.opcodes import OpcodesTestCase
from test_framework.blocktools import create_block, create_coinbase
from test_framework.mininode import (
    CTransaction,
    CTxIn,
    COutPoint,
    CTxOut,
    FromHex,
    ToHex,
)


def binding(binary, extended, height, tip):
    reply = subprocess.check_output(
        [str(binary), extended.hex(), str(height), str(tip), str(tip + 1)], text=True
    )
    return json.loads(reply)


def block(node, tx):
    parent = node.rpc("getblock", node.rpc("getbestblockhash"))
    candidate = create_block(
        int(parent["hash"], 16),
        create_coinbase(parent["height"] + 1),
        parent["time"] + 1,
    )
    candidate.vtx.append(tx)
    candidate.hashMerkleRoot = candidate.calc_merkle_root()
    candidate.solve()
    result = node.rpc("submitblock", ToHex(candidate))
    active = candidate.hash == node.rpc("getbestblockhash")
    if active != (result is None):
        raise RuntimeError(f"ambiguous block disposition: {result}")
    return active, candidate.hash, result


def compare(node, rust, go, *, entrant=None):
    remaining = 105 - node.rpc("getblockcount")
    if remaining > 0:
        node.rpc("generate", remaining)
    vectors = [
        (str(i), bytes(v.locking_script), bytes(v.unlocking_script))
        for i, v in enumerate(OpcodesTestCase.TESTS)
        if v.label == "POST_CHRONICLE"
    ]
    if len(vectors) != 10:
        raise RuntimeError("pinned Chronicle opcode corpus changed")
    vectors += [("false-result", b"\x00", b"")]
    funding = CTransaction()
    coin = node.rpc("listunspent")[0]
    funding.vin = [
        CTxIn(COutPoint(int(coin["txid"], 16), coin["vout"]), b"", 0xFFFFFFFF)
    ]
    value = 50000
    funding.vout = [CTxOut(value, locking) for _, locking, _ in vectors]
    change = bytes.fromhex(
        node.rpc("validateaddress", node.rpc("getnewaddress"))["scriptPubKey"]
    )
    funding.vout.append(
        CTxOut(round(coin["amount"] * 100000000) - len(vectors) * value - 10000, change)
    )
    signed = node.rpc("signrawtransaction", ToHex(funding))
    if not signed["complete"]:
        raise RuntimeError("failed to sign fixture funding")
    funding = FromHex(CTransaction(), signed["hex"])
    funding.rehash()
    node.rpc("sendrawtransaction", ToHex(funding))
    funding_block = node.rpc("generate", 1)[0]
    source_height = node.rpc("getblock", funding_block)["height"]
    if entrant:
        entrant.confirm_funding(node, funding_block)
    results = []
    for index, (name, locking, unlocking) in enumerate(vectors):
        tip = node.rpc("getblockcount")
        tx = CTransaction()
        tx.vin = [CTxIn(COutPoint(funding.sha256, index), unlocking, 0xFFFFFFFF)]
        tx.vout = [CTxOut(value - 2000, change)]
        tx.rehash()
        # The extended form inserts the actual funding output after its input.
        extended = struct.pack("<i", tx.nVersion) + bytes.fromhex("0000000000ef01")
        extended += tx.vin[0].serialize() + funding.vout[index].serialize()
        extended += b"\x01" + tx.vout[0].serialize() + struct.pack("<I", tx.nLockTime)
        observations = {
            k: binding(v, extended, source_height, tip)
            for k, v in [("rust", rust), ("go", go)]
        }
        if entrant:
            observations["scary"] = entrant.check(node, tx)
        try:
            accepted = node.rpc("sendrawtransaction", ToHex(tx)) == tx.hash
        except RpcError as error:
            if "error code: -26" not in str(error):
                raise
            accepted = False
        active, block_hash, reason = (
            entrant.block(node, tx, accepted, observations) if entrant else block(node, tx)
        )
        observations["sv"] = {"policy": accepted, "consensus": active, "reason": reason}
        results.append(
            {
                "case": name,
                "txid": tx.hash,
                "raw_tx": ToHex(tx),
                "extended_hex": extended.hex(),
                "utxo_height": source_height,
                "parent_height": tip,
                "block_hash": block_hash,
                "observations": observations,
            }
        )
    return {
        "reference": IMAGE,
        "corpus": "SV Node v1.2.3 post-Chronicle opcode fixtures plus false result",
        "funding_block": funding_block,
        "funding_tx": ToHex(funding),
        "rows": results,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path)
    args = parser.parse_args()
    with Reference().running() as node:
        receipt = compare(
            node, ROOT / "target/debug/examples/chronicle", ROOT / ".native/go-oracle"
        )
    args.receipt.write_text(json.dumps(receipt, indent=2) + "\n")
    for row in receipt["rows"]:
        for context in ["policy", "consensus"]:
            outcomes = {entry[context] for entry in row["observations"].values()}
            if len(outcomes) != 1 or row["observations"]["sv"]["consensus"] != (
                row["case"] != "false-result"
            ):
                raise RuntimeError(f"{row['case']} disagrees in {context}, see receipt")
