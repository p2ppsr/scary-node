"""Verify SV Node's confiscation lifecycle using fresh regtest wallet keys."""

import argparse
import hashlib
import json
import struct
from pathlib import Path
from sv_reference import IMAGE, Reference, RpcError


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def output(satoshis, script):
    require(len(script) < 253, "fixture script is too large")
    return struct.pack("<QB", satoshis, len(script)) + script


def confiscate(txid, index, script):
    order = hashlib.new(
        "ripemd160", hashlib.sha256(b"Scary Node regtest order").digest()
    ).digest()
    marker = b"\x00\x6a\x04cftx\x15\x01" + order
    prevout = bytes.fromhex(txid)[::-1] + struct.pack("<I", index)
    raw = b"\x01\x00\x00\x00\x01" + prevout + b"\x00\xff\xff\xff\xff\x02"
    return (raw + output(0, marker) + output(99_990_000, script) + bytes(4)).hex()


def run(node):
    rpc = node.rpc
    rpc("generate", 101)
    source_address = rpc("getnewaddress")
    recipient = rpc("getnewaddress")
    source = rpc("sendtoaddress", source_address, 1)
    source_block = rpc("generate", 1)[0]
    outputs = rpc("getrawtransaction", source, 1)["vout"]
    index = next(
        o["n"]
        for o in outputs
        if source_address in o["scriptPubKey"].get("addresses", [])
    )
    recipient_script = bytes.fromhex(rpc("validateaddress", recipient)["scriptPubKey"])
    raw = confiscate(source, index, recipient_script)
    ctx = rpc("decoderawtransaction", raw)["txid"]
    activation = rpc("getblockcount") + 1
    freeze = {
        "funds": [
            {
                "txOut": {"txId": source, "vout": index},
                "enforceAtHeight": [{"start": activation}],
                "policyExpiresWithConsensus": False,
            }
        ]
    }
    whitelist = {
        "confiscationTxs": [
            {"confiscationTx": {"enforceAtHeight": activation, "hex": raw}}
        ]
    }
    require(
        rpc("addToConsensusBlacklist", freeze)["notProcessed"] == [],
        "freeze not applied",
    )
    require(
        rpc("addToConfiscationTxidWhitelist", whitelist)["notProcessed"] == [],
        "authorization not applied",
    )
    node.restart()
    require(
        rpc("sendrawtransaction", raw) == ctx, "confiscation not admitted after restart"
    )
    ctx_block = rpc("generate", 1)[0]
    require(
        ctx in rpc("getblock", ctx_block)["tx"], "confiscation not mined at activation"
    )
    recovered = rpc("gettxout", ctx, 1, False)
    require(
        recovered["confiscation"]
        and recovered["scriptPubKey"]["hex"] == recipient_script.hex(),
        "wrong recovered output",
    )
    spend = rpc(
        "createrawtransaction",
        [{"txid": ctx, "vout": 1}],
        {rpc("getnewaddress"): 0.9998},
    )
    signed = rpc("signrawtransaction", spend)
    require(signed["complete"], "new owner signature incomplete")
    rpc("generate", 998)
    early = rpc("getblockcount") + 1
    require(early == activation + 999, "wrong maturity-minus-one height")
    try:
        rpc("sendrawtransaction", signed["hex"])
    except RpcError as exc:
        require("premature-spend-of-confiscation" in str(exc), str(exc))
    else:
        raise RuntimeError("accepted immature spend")
    parent = rpc("generate", 1)[0]
    spend_id = rpc("sendrawtransaction", signed["hex"])
    mined = rpc("generate", 1)[0]
    require(rpc("getblock", mined)["height"] == activation + 1000, "wrong spend height")
    require(spend_id in rpc("getblock", mined)["tx"], "spend not mined")
    rpc("invalidateblock", mined)
    require(
        rpc("gettxout", ctx, 1, False)["confiscation"],
        "reorg did not restore confiscated output",
    )
    rpc("invalidateblock", parent)
    require(spend_id not in rpc("getrawmempool"), "immature spend retained after reorg")
    rpc("reconsiderblock", parent)
    rpc("reconsiderblock", mined)
    require(
        rpc("getbestblockhash") == mined and rpc("gettxout", ctx, 1, False) is None,
        "spend not restored",
    )
    return {
        "reference": IMAGE,
        "scope": "SV Node RPC enforcement and wallet spend on isolated regtest",
        "source_block": source_block,
        "source": [source, index],
        "authorization_height": activation,
        "confiscation_txid": ctx,
        "confiscation_hex": raw,
        "confiscation_block": ctx_block,
        "immature_candidate_height": early,
        "mature_spend_height": activation + 1000,
        "recipient_spend_txid": spend_id,
        "recipient_spend_hex": signed["hex"],
        "settled_block": mined,
        "restored_active_tip": rpc("getbestblockhash"),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path)
    args = parser.parse_args()
    with Reference().running() as reference:
        receipt = run(reference)
    args.receipt.write_text(json.dumps(receipt, indent=2) + "\n")
