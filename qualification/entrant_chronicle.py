"""Run the pinned Chronicle corpus through a real witnessed node, Stratum and a stock SV receiver."""

import argparse
import contextlib
import gzip
import hashlib
import importlib
import json
from pathlib import Path
import tempfile
from urllib.error import HTTPError

from chronicle import ROOT, IMAGE, Reference, ToHex, block, compare
from chronicle_signatures import FAMILIES, evaluate
from run_cohort import session_lock
from scary_dar import LabFeed, entrant, get_port, mine_with_worker, response, until


def file_hash(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


class WitnessedChecks:
    def __init__(self, api, port, stratum):
        self.api = api
        self.port = port
        self.stratum = stratum
        self.funding = {}
        self.funding_ids = set()

    def selected(self, parent):
        def ready():
            m = response(self.api + "/status")["mining"]
            return (
                m if m["tip_hash"] == parent and m["observation_window_ready"] else None
            )

        return until(ready, "observed reference parent and contiguous current state")

    def confirm_funding(self, node, funding):
        # Every case compares scripts using an output for which the entrant has
        # seen a full descendant, as required by its witnessed mining policy.
        self.selected(funding)
        self.selected(node.rpc("generate", 1)[0])

    def check(self, node, tx):
        parent = node.rpc("getbestblockhash")
        m = self.selected(parent)
        for coin in tx.vin:
            txid = format(coin.prevout.hash, "064x")
            if txid not in self.funding_ids:
                h = node.rpc("getrawtransaction", txid, True)["blockhash"]
                if h not in self.funding:
                    self.funding[h] = {
                        "height": node.rpc("getblockheader", h)["height"],
                        "body_hex": node.rpc("getblock", h, 0),
                    }
                self.funding_ids.add(txid)
        raw = bytes.fromhex(ToHex(tx))
        result = response(self.api + "/check-transaction", raw)
        why = None
        try:
            policy = response(self.api + "/transactions", raw)["txid"] == tx.hash
        except HTTPError as err:
            if err.code != 422:
                raise
            policy = False
            why = json.loads(err.read())["error"]
        return {
            "policy": policy,
            "policy_reason": why,
            "consensus": result["outcome"] == "determinate",
            "check": result,
            "parent": parent,
            "parent_height": m["height"],
            "coverage_from": m["coverage_start"],
            "applied_sequence": m["durable_directive_sequence"],
        }

    def block(self, node, tx, accepted, observations):
        entry = observations["scary"]
        if accepted and all(
            r["policy"] and r["consensus"] for r in observations.values()
        ):
            h = mine_with_worker(
                self.api, self.port, self.stratum, node, bytes.fromhex(ToHex(tx))
            )
            active, why = True, None
            entry["origin"] = "scary-stratum"
        else:
            active, h, why = block(node, tx)
            entry["origin"] = "sv-reference"
        entry["stock_active"] = node.rpc("getbestblockhash")
        if active:
            self.selected(h)
            entry["active_body_hex"] = node.rpc("getblock", h, 0)
            obs = response(self.api + "/status")["mining"]["validation_window"]
            entry["block_checks"] = next(o for o in obs if o["hash"] == h)
        return active, h, why


def run(binary, rust, go):
    with (
        session_lock(),
        tempfile.TemporaryDirectory() as td,
        Reference().running(peer=True) as node,
        contextlib.ExitStack() as life,
    ):
        node.rpc("generate", 150)
        p = Path(td)
        feed = LabFeed(node, p)
        life.callback(feed.server.server_close)
        life.callback(feed.server.shutdown)
        port, worker = get_port(), get_port()
        api = life.enter_context(entrant(binary, node, feed, p, port, worker))
        checks = WitnessedChecks(api, port, worker)
        for _ in range(3):
            tip = node.rpc("generate", 1)[0]
            until(
                lambda: response(api + "/status")["mining"]["tip_hash"] == tip,
                "entry body from SV",
            )
        checks.selected(tip)
        opcodes = compare(node, rust, go, entrant=checks)
        signatures = []
        for module, name in FAMILIES.items():
            cls = getattr(
                importlib.import_module("chronicle_upgrade_tests." + module), name
            )
            signatures.extend(
                evaluate(node, module, cls, entrant=checks, rust=rust, go=go)
            )
        successor = node.rpc("generate", 1)[0]
        checks.selected(successor)
        state = response(api + "/status")
        digest = file_hash(binary)
        if state["release"]["executable_sha256"] != digest:
            raise ValueError("running node differs from requested release")
        return {
            "schema": 1,
            "reference": IMAGE,
            "node_sha256": digest,
            "rust_oracle_sha256": file_hash(rust),
            "go_oracle_sha256": file_hash(go),
            "interpreter": state["mining"]["interpreter"],
            "funding_blocks": checks.funding,
            "opcodes": opcodes,
            "signatures": signatures,
            "final_successor": successor,
            "final_body_hex": node.rpc("getblock", successor, 0),
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument(
        "--rust-oracle", type=Path, default=ROOT / "target/debug/examples/chronicle"
    )
    parser.add_argument("--go-oracle", type=Path, default=ROOT / ".native/go-oracle")
    a = parser.parse_args()
    record = run(a.binary, a.rust_oracle, a.go_oracle)
    tmp = a.receipt.with_name(a.receipt.name + ".next")
    tmp.write_bytes(
        gzip.compress(json.dumps(record, separators=(",", ":")).encode(), mtime=0)
    )
    tmp.replace(a.receipt)
    from verify_entrant_chronicle import verify

    print(json.dumps(verify(record), indent=2))
