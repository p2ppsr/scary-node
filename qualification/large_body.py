"""Verify a disk-backed large wire body, its known signature and checkpoint restore on native SV P2P."""

import argparse
import hashlib
import json
import tempfile
from pathlib import Path
from capacity_recovery import observer
from native_workload import solve
from chronicle import ROOT, Reference, IMAGE, CTransaction, CTxOut, FromHex, ToHex
from test_framework.script import CScript, OP_FALSE, OP_RETURN
from scary_dar import get_port, response, until


def run(binary, path, size, previous=None):
    with Reference().running(peer=True) as n:
        n.rpc("generate", 105)
        peer = n.docker("port", n.container, "18444/tcp")
        p = get_port()
        url = f"http://127.0.0.1:{p}/status"
        with observer(binary, peer, path, p, 10000):
            until(lambda: response(url)["peer"]["connected_peer_ids_seen"], "handshake")
            address = n.rpc("getnewaddress")
            fund_id = n.rpc("sendtoaddress", address, 1)
            fund_block = n.rpc("generate", 1)[0]
            until(
                lambda: response(url)["mining"]["tip_hash"] == fund_block,
                "witnessed funding",
            )
            fund = n.rpc("getrawtransaction", fund_id, 1)
            vout = next(x["n"] for x in fund["vout"] if x["value"] == 1)
            stub = n.rpc(
                "createrawtransaction",
                [{"txid": fund_id, "vout": vout}],
                {address: 0.99},
            )
            child = FromHex(CTransaction(), stub)
            child.vout.append(CTxOut(0, CScript([OP_FALSE, OP_RETURN, bytes(size)])))
            signed = n.rpc("signrawtransaction", ToHex(child))
            assert signed["complete"]
            child = FromHex(CTransaction(), signed["hex"])
            parent = n.rpc("getblock", fund_block)
            b = solve(parent["hash"], 107, parent["time"] + 1, [child])
            raw = b.serialize()
            assert (
                n.rpc("submitblock", raw.hex()) is None
                and n.rpc("getbestblockhash") == b.hash
            )
            until(
                lambda: response(url)["mining"]["tip_hash"] == b.hash,
                "mapped large body",
            )
            view = response(url)["mining"]
            r = view["validation_window"][-1]
            assert r["transactions_determinate"] == 1 and r["script_checks_passed"] == 1
            assert view["mapped_body_bytes"] == len(raw) and view["mapped_bodies"] == 1
        with observer(binary, peer, path, p, 10000):
            until(
                lambda: response(url)["mining"]["tip_hash"] == b.hash, "restored body"
            )
            restored = response(url)["mining"]
            assert restored["mapped_bodies"] == 1 and restored[
                "mapped_body_bytes"
            ] == len(raw)
            next_block = n.rpc("generate", 1)[0]
            until(
                lambda: response(url)["mining"]["tip_hash"] == next_block,
                "post-restore reference successor",
            )
            result = {
                "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                "reference": IMAGE,
                "wire_bytes": len(raw),
                "body_sha256": hashlib.sha256(raw).hexdigest(),
                "header_hex": raw[:80].hex(),
                "hash": b.hash,
                "known_signature_checks": r["script_checks_passed"],
                "mapped_bytes": view["mapped_body_bytes"],
                "restored_tip": restored["tip_hash"],
                "restored_mapped_bytes": restored["mapped_body_bytes"],
                "successor": next_block,
                "reference_tip": n.rpc("getbestblockhash"),
            }

        if previous:
            with observer(previous, peer, path, p, 10000):
                until(
                    lambda: response(url)["peer"]["connected_peer_ids_seen"]
                    and response(url)["mining"]["tip_hash"] == next_block,
                    "older executable restored the newer boundary",
                )
                rollback_tip = n.rpc("generate", 1)[0]
                until(
                    lambda: response(url)["mining"]["tip_hash"] == rollback_tip,
                    "older executable resumed coverage",
                )
                result["rollback"] = {
                    "binary_sha256": hashlib.sha256(previous.read_bytes()).hexdigest(),
                    "restored_tip": next_block,
                    "next_active_tip": rollback_tip,
                    "reference_tip": n.rpc("getbestblockhash"),
                }
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path)
    parser.add_argument(
        "--binary", type=Path, default=ROOT / "target/release/scary-node"
    )
    parser.add_argument("--payload-bytes", type=int, default=12 << 20)
    parser.add_argument("--rollback-binary", type=Path)
    a = parser.parse_args()
    assert (1 << 20) <= a.payload_bytes < 19_000_000
    with tempfile.TemporaryDirectory() as td:
        r = run(a.binary, Path(td), a.payload_bytes, a.rollback_binary)
    a.receipt.write_text(json.dumps(r, indent=2) + "\n")
