"""Mine a signed wallet payment delivered only through the independent SV peer's mempool relay."""

import argparse
import contextlib
import hashlib
import json
import socket
import tempfile
from pathlib import Path

from native_cohort import dsha, funding_transactions, stratum_job
from peer_loss import nonce
from run_cohort import session_lock
from scary_dar import ROOT, LabFeed, empty_successor, entrant, get_port, response, until
from sv_reference import IMAGE, Reference
from test_framework.mininode import CBlock, CTransaction, FromHex


def worker_without_job(port):
    """Observe an authorized worker across multiple server refreshes."""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        for i, (method, params) in enumerate(
            [
                ("mining.subscribe", ["early-witness-regtest"]),
                ("mining.authorize", ["race-worker", "regtest"]),
            ],
            start=1,
        ):
            sock.sendall(
                (
                    json.dumps({"id": i, "method": method, "params": params}) + "\n"
                ).encode()
            )
        messages = []
        with sock.makefile("rb") as stream:
            while not any(
                m.get("id") == 2 and m.get("result") is True for m in messages
            ):
                line = stream.readline(8193)
                assert line.endswith(b"\n")
                messages.append(json.loads(line))
            sock.settimeout(0.8)
            try:
                while True:
                    line = stream.readline(8193)
                    assert line.endswith(b"\n")
                    messages.append(json.loads(line))
            except TimeoutError:
                pass
        assert any(m.get("id") == 1 and m.get("result") for m in messages)
        assert not any(
            m.get("error") or m.get("method") == "mining.notify" for m in messages
        )
        return {
            "authorized": any(
                m.get("id") == 2 and m.get("result") is True for m in messages
            ),
            "notifications": sum(m.get("method") == "mining.notify" for m in messages),
            "observed_methods": [m["method"] for m in messages if m.get("method")],
            "quiet_interval_ms": 800,
        }


def run(binary, directory, early=None):
    with (
        Reference().running(peer=True, fast_rpc=True) as full,
        contextlib.ExitStack() as life,
    ):
        full.rpc("generate", 105)
        feed = LabFeed(full, directory)
        life.callback(feed.server.shutdown)
        port, worker = get_port(), get_port()
        api = life.enter_context(entrant(binary, full, feed, directory, port, worker))

        def mining():
            return response(api + "/status")["mining"]

        binary_sha256 = hashlib.sha256(binary.read_bytes()).hexdigest()
        assert (
            response(api + "/status")["release"]["executable_sha256"] == binary_sha256
        )
        funding, (payment,) = funding_transactions(full, 1)
        funding_id = full.rpc("sendrawtransaction", funding)
        first = full.rpc("generate", 1)[0]
        until(lambda: mining()["tip_hash"] == first, "funding block witnessed")

        def stock_tx_bytes():
            (peer,) = full.rpc("getpeerinfo")
            return peer["bytessent_per_msg"].get("tx", 0)

        def snapshot():
            m = mining()
            return {
                k: m[k]
                for k in (
                    "tip_hash",
                    "height",
                    "observation_window_ready",
                    "job_preconditions_met",
                    "pending_transactions",
                    "peer_transaction_intake",
                    "waiting_for_successor",
                    "waiting_for_successor_bytes",
                )
                if k in m
            }

        early_record = {}
        if early:
            start = snapshot()
            assert start["tip_hash"] == first and start["observation_window_ready"]
            parent = None
            stock_before = stock_tx_bytes()
        else:
            parent = full.rpc("generate", 1)[0]
            until(
                lambda: (
                    mining()["tip_hash"] == parent
                    and mining()["observation_window_ready"]
                ),
                "ready parent",
            )
            start = snapshot()
        assert start["pending_transactions"] == 0
        before = start["peer_transaction_intake"]
        payment_id = full.rpc("sendrawtransaction", payment.hex())
        assert payment_id == dsha(payment)[::-1].hex() and payment_id in full.rpc(
            "getrawmempool"
        )
        if early:
            until(
                lambda: mining()["peer_transaction_intake"]["sv"] != before["sv"],
                "early payment handled by the native peer",
            )
            until(
                lambda: stock_tx_bytes() == stock_before + 24 + len(payment),
                "one stock native transaction frame",
            )
            stock_after = stock_tx_bytes()
            assert stock_after == stock_before + 24 + len(payment)
            seen = snapshot()
            assert (
                seen["tip_hash"] == first
                and seen["pending_transactions"] == 0
                and not seen["job_preconditions_met"]
            )
            silence = worker_without_job(worker)
            parent = empty_successor(full)
            until(
                lambda: (
                    mining()["tip_hash"] == parent
                    and mining()["observation_window_ready"]
                ),
                "empty successor witnessed",
            )
            parent_wire = full.rpc("getblock", parent, 0)
            mempool = full.rpc("getrawmempool")
            assert mempool == [payment_id]
            early_record = {
                "before_offer": start,
                "after_first_offer": seen,
                "before_successor_worker": silence,
                "stock_tx_bytes_before": stock_before,
                "stock_tx_bytes_after_offer": stock_after,
                "stock_mempool_after_successor": mempool,
                "empty_successor_wire": parent_wire,
            }
            if early == "baseline":
                early_record["after_successor_worker"] = worker_without_job(worker)
                early_record["after_successor"] = snapshot()
                early_record["stock_tx_bytes_after_successor"] = stock_tx_bytes()
                assert early_record["after_successor"]["pending_transactions"] == 0
                assert not early_record["after_successor"]["job_preconditions_met"]
                assert (
                    early_record["stock_tx_bytes_after_successor"]
                    == early_record["stock_tx_bytes_after_offer"]
                )
                return {
                    "network": "regtest",
                    "reference": IMAGE,
                    "binary_sha256": binary_sha256,
                    "early_offer": early_record,
                    "result": "withheld",
                    "funding_txid": funding_id,
                    "funding_hex": funding,
                    "witnessed_funding_block": first,
                    "creating_block_wire": full.rpc("getblock", first, 0),
                    "parent": parent,
                    "parent_header": parent_wire[:160],
                    "payment_hex": payment.hex(),
                    "payment_txid": payment_id,
                }
        until(
            lambda: mining()["pending_transactions"] == 1,
            "ordinary payment eligible from original SV peer offer",
        )
        after = mining()["peer_transaction_intake"]
        if early:
            assert seen["waiting_for_successor"] == 1 and seen[
                "waiting_for_successor_bytes"
            ] == len(payment)
            assert (
                seen["peer_transaction_intake"]["sv"]["awaiting_successor"]
                == before["sv"]["awaiting_successor"] + 1
            )
            assert (
                after["sv"]["promoted_after_successor"]
                == before["sv"]["promoted_after_successor"] + 1
            )
            assert after["sv"]["admitted"] == before["sv"]["admitted"] + 1
            early_record["after_successor"] = snapshot()
            assert early_record["after_successor"]["waiting_for_successor"] == 0
            early_record["stock_tx_bytes_after_successor"] = stock_tx_bytes()
            assert (
                early_record["stock_tx_bytes_after_successor"]
                == early_record["stock_tx_bytes_after_offer"]
            )
        else:
            assert after["sv"]["admitted"] == before["sv"]["admitted"] + 1
        assert after["teranode_subtree"] == before["teranode_subtree"]
        with stratum_job(parent, payment, worker, port, "sv-relayed-payment") as job:
            solution = nonce(job)
            header = job.header(solution)
            solved_hash = dsha(header)[::-1].hex()
            assert job.submit_native(solution)
        until(
            lambda: full.rpc("getbestblockhash") == solved_hash,
            "SV selected Stratum block",
        )
        if early:
            early_record["stock_tx_bytes_after_selection"] = stock_tx_bytes()
            assert (
                early_record["stock_tx_bytes_after_selection"]
                == early_record["stock_tx_bytes_after_offer"]
            )
        result = full.rpc("getblock", solved_hash)
        assert result["previousblockhash"] == parent and result["tx"][1:] == [
            payment_id
        ]
        wire = full.rpc("getblock", solved_hash, 0)
        block = FromHex(CBlock(), wire)
        assert block.vtx[1].serialize() == payment
        source = FromHex(CTransaction(), funding)
        spent = block.vtx[1].vin[0].prevout
        assert spent.hash == int(funding_id, 16)
        fee = source.vout[spent.n].nValue - sum(out.nValue for out in block.vtx[1].vout)
        subsidy = (50 * 100_000_000) >> (result["height"] // 150)
        assert fee > 0 and len(block.vtx[0].vout) == 1
        payout = block.vtx[0].vout[0]
        assert bytes(payout.scriptPubKey) == b"\x51" and payout.nValue == subsidy + fee
        successor = full.rpc("generate", 1)[0]
        until(
            lambda: mining()["tip_hash"] == successor,
            "entrant observed independent successor",
        )
        assert full.rpc("getblock", successor)["previousblockhash"] == solved_hash
        return {
            **({"early_offer": early_record, "result": "selected"} if early else {}),
            "network": "regtest",
            "reference": IMAGE,
            "binary_sha256": binary_sha256,
            "source": "Stock SV Node received sendrawtransaction and delivered the payment over the entrant’s only SV connection.",
            "peer_intake_before": before,
            "peer_intake_after": after,
            "funding_txid": funding_id,
            "funding_hex": funding,
            "witnessed_funding_block": first,
            "creating_block_wire": full.rpc("getblock", first, 0),
            "parent_header": full.rpc("getblockheader", parent, False),
            "parent": parent,
            "payment_hex": payment.hex(),
            "payment_txid": payment_id,
            "fee_satoshis": fee,
            "subsidy_satoshis": subsidy,
            "payout_satoshis": payout.nValue,
            "block_hash": solved_hash,
            "solved_block": wire,
            "header_hex": header.hex(),
            "block_transaction_ids": result["tx"],
            "sv_successor": successor,
            "sv_successor_header": full.rpc("getblockheader", successor, False),
        }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("receipt", type=Path)
    p.add_argument("--binary", type=Path, default=ROOT / "target/release/scary-node")
    p.add_argument(
        "--early",
        choices=["baseline", "reconsidered"],
        help="offer the payment before its parent gains a successor",
    )
    args = p.parse_args()
    with session_lock(), tempfile.TemporaryDirectory() as directory:
        receipt = run(args.binary, Path(directory), args.early)
    args.receipt.write_text(json.dumps(receipt, indent=2) + "\n")
