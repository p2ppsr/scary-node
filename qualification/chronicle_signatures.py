"""Run unmodified post-Chronicle signature case generators on actual funded outputs."""

import argparse
import importlib
import json
import struct
from pathlib import Path
from chronicle import (
    binding,
    funding_coin,
    block,
    Reference,
    RpcError,
    IMAGE,
    ROOT,
    CTransaction,
    CTxIn,
    COutPoint,
    CTxOut,
    FromHex,
    ToHex,
)
from test_framework.height_based_test_framework import TxCollection
from test_framework.script import CScript, OP_CHECKSIG
from test_framework.mininode import ser_compact_size

FAMILIES = {
    "clean_stack": "CleanStackTestCase",
    "low-s": "LowSRemovalTestCase",
    "sighash_chronicle": "ForkIDTestCase",
    "sig_push_only": "SigPushOnlyTestCase",
}


def evaluate(node, family, klass, *, entrant=None, rust=None, go=None):
    case = klass()
    size = case._NUMBER_OF_UTXOS_PER_HEIGHT
    coin, change_value = funding_coin(node, size * 200000 + 10000)
    parent = CTransaction()
    parent.vin = [
        CTxIn(COutPoint(int(coin["txid"], 16), coin["vout"]), b"", 0xFFFFFFFF)
    ]
    script = CScript([case._UTXO_KEY.get_pubkey(), OP_CHECKSIG])
    parent.vout = [CTxOut(200000, script) for _ in range(size)]
    change = bytes.fromhex(
        node.rpc("validateaddress", node.rpc("getnewaddress"))["scriptPubKey"]
    )
    parent.vout.append(CTxOut(change_value, change))
    signed = node.rpc("signrawtransaction", ToHex(parent))
    assert signed["complete"]
    parent = FromHex(CTransaction(), signed["hex"])
    parent.rehash()
    for out in parent.vout:
        out.scriptPubKey = CScript(out.scriptPubKey)
    node.rpc("sendrawtransaction", ToHex(parent))
    funding_block = node.rpc("generate", 1)[0]
    source = node.rpc("getblock", funding_block)["height"]
    if entrant:
        entrant.confirm_funding(node, funding_block)
    case.utxos["POST_CHRONICLE"] = ([(i, parent) for i in range(size)], None)
    vectors = TxCollection(source + 1, "POST_CHRONICLE")
    case.get_transactions_for_test(vectors, None)
    rows = []
    for expected, txs in [
        (True, vectors.block_valid_txs),
        (False, [t for t, _ in vectors.block_invalid_txs]),
    ]:
        for tx in txs:
            tx.rehash()
            ef = (
                struct.pack("<i", tx.nVersion)
                + bytes.fromhex("0000000000ef")
                + ser_compact_size(len(tx.vin))
            )
            for inp in tx.vin:
                assert inp.prevout.hash == parent.sha256
                ef += inp.serialize() + parent.vout[inp.prevout.n].serialize()
            ef += (
                ser_compact_size(len(tx.vout))
                + b"".join(o.serialize() for o in tx.vout)
                + struct.pack("<I", tx.nLockTime)
            )
            tip = node.rpc("getblockcount")
            contexts = ",".join(str(source) for _ in tx.vin)
            outcomes = {
                name: binding(binary, ef, contexts, tip)
                for name, binary in [
                    ("rust", rust or ROOT / "target/debug/examples/chronicle"),
                    ("go", go or ROOT / ".native/go-oracle"),
                ]
            }
            if entrant:
                outcomes["scary"] = entrant.check(node, tx)
            why = None
            try:
                accepted = node.rpc("sendrawtransaction", ToHex(tx)) == tx.hash
            except RpcError as error:
                why = str(error)
                if "-26" not in why:
                    raise
                accepted = False
            active, block_id, reason = (
                entrant.block(node, tx, accepted, outcomes) if entrant else block(node, tx)
            )
            outcomes["sv"] = {
                "policy": accepted,
                "consensus": active,
                "block_reason": reason,
                "policy_reason": why,
            }
            rows.append(
                {
                    "family": family,
                    "index": len(rows),
                    "inputs": len(tx.vin),
                    "txid": tx.hash,
                    "raw": ToHex(tx),
                    "extended": ef.hex(),
                    "source_height": source,
                    "tip": tip,
                    "block": block_id,
                    "expected_consensus": expected,
                    "observations": outcomes,
                }
            )
    return rows


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("receipt", type=Path)
    args = p.parse_args()
    result = {"reference": IMAGE, "rows": []}
    with Reference().running() as node:
        node.rpc("generate", 150)
        for family, name in FAMILIES.items():
            cls = getattr(
                importlib.import_module("chronicle_upgrade_tests." + family), name
            )
            result["rows"].extend(evaluate(node, family, cls))
            args.receipt.write_text(json.dumps(result, indent=2) + "\n")
    disagreements = [
        (r["family"], r["index"], mode)
        for r in result["rows"]
        for mode in ("policy", "consensus")
        if len({o[mode] for o in r["observations"].values()}) != 1
    ]
    if disagreements:
        raise SystemExit(f"Differential disagreements {disagreements}")

    if any(row["expected_consensus"] != row["observations"]["sv"]["consensus"] for row in result["rows"]):
        raise SystemExit("SV Node differs from the pinned functional expectation")
