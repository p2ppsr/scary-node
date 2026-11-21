"""Measure full-state SV validation, native P2P, witnessed validation and durable tip commit."""

import argparse
import hashlib
import json
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from chronicle import (
    Reference,
    ROOT,
    IMAGE,
    CTransaction,
    CTxIn,
    COutPoint,
    CTxOut,
    FromHex,
    ToHex,
    create_block,
    create_coinbase,
)
from chronicle_upgrade_tests.test_base import ChronicleHeightTestsCase
from test_framework.script import CScript, OP_CHECKSIG, SIGHASH_ALL, SIGHASH_FORKID
from scary_dar import get_port, response, until


def solve(parent, height, timestamp, txs):
    b = create_block(int(parent, 16), create_coinbase(height), timestamp)
    b.nVersion = 0x20000000
    b.vtx.extend(txs)
    b.hashMerkleRoot = b.calc_merkle_root()
    b.solve()
    return b


def wallet_coins(rpc, fanout):
    needed = 100000 * fanout + 10000
    spendable = sorted(
        (coin for coin in rpc("listunspent") if coin.get("spendable", True)),
        key=lambda coin: coin["amount"],
        reverse=True,
    )
    selected = []
    total = 0
    for coin in spendable:
        selected.append(coin)
        total += round(coin["amount"] * 1e8)
        if total >= needed:
            return selected, total
    return [], 0


def signed_funding(rpc, fanout):
    case = ChronicleHeightTestsCase()
    key = case.make_key(b"disposable native throughput fixture")
    script = CScript([key.get_pubkey(), OP_CHECKSIG])
    coins, total = wallet_coins(rpc, fanout)
    if not coins:
        raise ValueError("insufficient mature wallet funds for the signed fixture")
    funding = CTransaction()
    funding.vin = [
        CTxIn(COutPoint(int(coin["txid"], 16), coin["vout"]), b"", 0xFFFFFFFF)
        for coin in coins
    ]
    funding.vout = [CTxOut(100000, script) for _ in range(fanout)]
    change = bytes.fromhex(rpc("validateaddress", rpc("getnewaddress"))["scriptPubKey"])
    funding.vout.append(CTxOut(total - 100000 * fanout - 10000, change))
    signed = rpc("signrawtransaction", ToHex(funding))
    assert signed["complete"]
    funding = FromHex(CTransaction(), signed["hex"])
    funding.rehash()
    for o in funding.vout:
        o.scriptPubKey = CScript(o.scriptPubKey)
    return case, key, funding


def signed_bodies(
    directory, case, key, funding, fanout, rounds, parent, height, timestamp
):
    sources = [(i, funding) for i in range(fanout)]
    bodies = []
    for i in range(rounds):
        transactions = [
            case.new_transaction(key, [case.Input(utxo, SIGHASH_ALL | SIGHASH_FORKID)])
            for utxo in sources
        ]
        b = solve(parent, height + i, timestamp + i, transactions)
        path = directory / f"{i}.block"
        path.write_bytes(b.serialize())
        bodies.append((b.hash, path))
        sources = [(0, tx) for tx in transactions]
        parent = b.hash
    return bodies


def run(binary, fanout, rounds, dir):
    with Reference().running(peer=True) as ref:
        rpc = ref.rpc
        rpc("generate", 105)
        api_port = get_port()
        peer = ref.docker("port", ref.container, "18444/tcp")
        url = f"http://127.0.0.1:{api_port}/status"
        with (dir / "node.log").open("wb") as log:
            node = subprocess.Popen(
                [
                    str(binary),
                    "--network",
                    "regtest",
                    "--sv-peer",
                    peer,
                    "--data-dir",
                    str(dir / "state"),
                    "--api-bind",
                    f"127.0.0.1:{api_port}",
                    "--observe-bodies",
                ],
                stdout=log,
                stderr=log,
            )
        try:
            until(
                lambda: response(url)["peer"]["connected_peer_ids_seen"],
                "native TCP handshake",
            )
            base = rpc("getblock", rpc("getbestblockhash"))
            case, key, funding = signed_funding(rpc, fanout)
            first = solve(base["hash"], 106, base["time"] + 1, [funding])
            assert rpc("submitblock", ToHex(first)) is None
            until(
                lambda: response(url)["mining"]["tip_hash"] == first.hash,
                "witnessed funding",
            )
            bodies = signed_bodies(
                dir,
                case,
                key,
                funding,
                fanout,
                rounds,
                first.hash,
                107,
                base["time"] + 2,
            )
            parent = bodies[-1][0]
            rows = []
            began = time.perf_counter_ns()
            for i, (expected, path) in enumerate(bodies):
                start = time.perf_counter_ns()
                body = path.read_bytes()
                assert rpc("submitblock", body.hex()) is None
                reference = time.perf_counter_ns()
                view = until(
                    lambda: (
                        s
                        if (s := response(url)["mining"])["tip_hash"] == expected
                        else None
                    ),
                    "durable witnessed tip",
                )
                finished = time.perf_counter_ns()
                checks = view["validation_window"][-1]
                assert (
                    checks["script_checks_passed"] == fanout
                    and checks["transactions_determinate"] == fanout
                )
                rows.append(
                    {
                        "height": 107 + i,
                        "hash": expected,
                        "wire_bytes": len(body),
                        "transactions": fanout,
                        "known_signature_checks": checks["script_checks_passed"],
                        "reference_return_ns": reference - start,
                        "durable_observed_ns": finished - start,
                        "source": "live SV Node getdata/block",
                    }
                )
            elapsed = time.perf_counter_ns() - began
            assert rpc("getbestblockhash") == parent
            return {
                "sv_reference": IMAGE,
                "node_binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                "rows": rows,
                "total_transactions": rounds * fanout,
                "total_wall_ns": elapsed,
                "transactions_per_second": rounds * fanout * 1e9 / elapsed,
                "final_reference_tip": parent,
            }
        except Exception:
            print((dir / "node.log").read_text()[-3500:], flush=True)
            raise
        finally:
            node.send_signal(signal.SIGINT)
            node.wait(timeout=20)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("receipt", type=Path)
    p.add_argument("--rounds", type=int, default=8)
    p.add_argument("--fanout", type=int, default=4096)
    p.add_argument("--binary", type=Path, default=ROOT / "target/release/scary-node")
    a = p.parse_args()
    assert 0 < a.fanout <= 32768 and 0 < a.rounds < 900
    with tempfile.TemporaryDirectory() as d:
        result = run(a.binary, a.fanout, a.rounds, Path(d))
    a.receipt.write_text(json.dumps(result, indent=2) + "\n")
