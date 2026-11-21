"""Bracket a signed freeze and a network-target Stratum submission on one parent."""

import argparse
import json
import socket
import struct
import tempfile
from pathlib import Path
from scary_dar import (
    Reference,
    LabFeed,
    ROOT,
    entrant,
    get_port,
    until,
    response,
    stratum_job,
    REGTEST_TARGET,
    dsha,
)
from sv_dar import require


def run(order, binary):
    with tempfile.TemporaryDirectory() as temp, Reference().running(peer=True) as ref:
        dir = Path(temp)
        rpc = ref.rpc
        rpc("generate", 105)
        feed = LabFeed(ref, dir)
        port, stratum = get_port(), get_port()
        try:
            with entrant(binary, ref, feed, dir, port, stratum) as api:
                address = rpc("getnewaddress")
                source = rpc("sendtoaddress", address, 1)
                funding = rpc("generate", 1)[0]
                until(
                    lambda: response(api + "/status")["mining"]["tip_hash"] == funding,
                    "funding",
                )
                parent = rpc("generate", 1)[0]
                until(
                    lambda: response(api + "/status")["mining"]["tip_hash"] == parent,
                    "parent",
                )
                index = next(
                    o["n"]
                    for o in rpc("getrawtransaction", source, 1)["vout"]
                    if address in o["scriptPubKey"].get("addresses", [])
                )
                draft = rpc(
                    "createrawtransaction",
                    [{"txid": source, "vout": index}],
                    {rpc("getnewaddress"): 0.9998},
                )
                raw = bytes.fromhex(rpc("signrawtransaction", draft)["hex"])
                require(
                    response(api + "/transactions", raw)["txid"]
                    == dsha(raw)[::-1].hex(),
                    "mismatched admission",
                )
                height = rpc("getblockcount") + 1
                payload = bytes.fromhex(source) + struct.pack(
                    "<QQQB", index, height, 2**31 - 1, 0
                )

                def freeze():
                    seq = feed.add(2, payload)
                    until(
                        lambda: response(api + "/status")["mining"][
                            "durable_directive_sequence"
                        ]
                        == seq,
                        "durable freeze",
                    )
                    reply = rpc(
                        "addToConsensusBlacklist",
                        {
                            "funds": [
                                {
                                    "txOut": {"txId": source, "vout": index},
                                    "enforceAtHeight": [
                                        {"start": height, "stop": 2**31 - 1}
                                    ],
                                    "policyExpiresWithConsensus": False,
                                }
                            ]
                        },
                    )
                    require(not reply["notProcessed"], "reference freeze failed")

                with stratum_job(parent, raw, stratum, port) as job:
                    nonce = next(
                        n
                        for n in range(1024)
                        if int.from_bytes(dsha(job.header(n)), "little")
                        <= REGTEST_TARGET
                    )
                    solved = dsha(job.header(nonce))[::-1].hex()
                    if order == "directive-first":
                        freeze()
                        try:
                            job.submit_native(nonce)
                        except (
                            RuntimeError,
                            OSError,
                            ValueError,
                            AssertionError,
                        ) as rejection:
                            outcome = type(rejection).__name__
                        else:
                            raise RuntimeError("retired job accepted")
                        require(
                            rpc("getbestblockhash") == parent,
                            "retired solve appeared on the reference",
                        )
                        require(
                            response(api + "/status")["mining"]["solved_blocks"] == 0,
                            "retired solve queued",
                        )
                    else:
                        job.submit_native(nonce)
                        until(
                            lambda: rpc("getbestblockhash") == solved,
                            "reference received solution",
                        )
                        outcome = "reference adopted before freeze"
                        freeze()
                return {
                    "order": order,
                    "parent": parent,
                    "height": height,
                    "candidate": solved,
                    "outcome": outcome,
                    "reference_tip": rpc("getbestblockhash"),
                    "durable_sequence": 1,
                    "receipt": json.loads(
                        (dir / "state/regtest/alerts/0000000001.receipt").read_text()
                    ),
                    "solutions": response(api + "/status")["mining"]["solutions"],
                }
        finally:
            feed.server.shutdown()
            feed.server.server_close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("receipt", type=Path)
    p.add_argument("--binary", type=Path, default=ROOT / "target/debug/scary-node")
    args = p.parse_args()
    result = [
        run(order, args.binary) for order in ("directive-first", "solution-first")
    ]
    args.receipt.write_text(json.dumps(result, indent=2) + "\n")
