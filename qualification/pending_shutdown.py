"""Compare eligible native wallet work across controlled process stops and a change of stock peer."""

import argparse
import hashlib
import json
import signal
import tempfile
from pathlib import Path

from native_cohort import dsha, funding_transactions, stratum_job
from peer_loss import nonce
from run_cohort import session_lock
from scary_dar import LabFeed, entrant, get_port, response, until
from sv_reference import IMAGE, Reference
from sv_transaction_intake import worker_without_job


def scenario(binary, sig, baseline, directory):
    with (
        Reference().running(peer=True, fast_rpc=True) as sender,
        Reference().running(peer=True, fast_rpc=True) as fresh,
    ):
        sender.rpc("generate", 105)
        feed = LabFeed(sender, directory)
        api_port, worker = get_port(), get_port()
        release = hashlib.sha256(binary.read_bytes()).hexdigest()
        exit_receipt = {}
        try:
            with entrant(
                binary,
                sender,
                feed,
                directory,
                api_port,
                worker,
                stop_signal=sig,
                exit_receipt=exit_receipt,
            ) as api:

                def mining():
                    return response(api + "/status")["mining"]

                require_release = response(api + "/status")["release"][
                    "executable_sha256"
                ]
                assert require_release == release
                funding, (payment,) = funding_transactions(sender, 1)
                sender.rpc("sendrawtransaction", funding)
                fund = sender.rpc("generate", 1)[0]
                until(lambda: mining()["tip_hash"] == fund, "creation witnessed")
                parent = sender.rpc("generate", 1)[0]
                until(
                    lambda: (
                        mining()["tip_hash"] == parent
                        and mining()["observation_window_ready"]
                    ),
                    "funding endorsed",
                )
                for height in range(1, 108):
                    block = sender.rpc(
                        "getblock", sender.rpc("getblockhash", height), 0
                    )
                    assert fresh.rpc("submitblock", block) is None
                assert (
                    fresh.rpc("getbestblockhash") == parent
                    and fresh.rpc("getrawmempool") == []
                )
                ident = sender.rpc("sendrawtransaction", payment.hex())
                assert ident == dsha(payment)[::-1].hex()
                until(
                    lambda: mining()["pending_transactions"] == 1,
                    "native signed payment admitted",
                )
                before = mining()
                assert (
                    before["peer_transaction_intake"]["sv"]["admitted"] == 1
                    and before["job_preconditions_met"]
                )
            with entrant(binary, fresh, feed, directory, api_port, worker) as api:
                until(
                    lambda: (
                        mining()["tip_hash"] == parent
                        and mining()["observation_window_ready"]
                    ),
                    "restored parent reconfirmed",
                )
                restored = mining()
                record = {
                    "release": release,
                    "reference": IMAGE,
                    "exit": exit_receipt,
                    "height": before["height"],
                    "parent": parent,
                    "funding_hex": funding,
                    "funding_block_hex": sender.rpc("getblock", fund, 0),
                    "funding_block_header": sender.rpc("getblockheader", fund, False),
                    "parent_header": sender.rpc("getblockheader", parent, False),
                    "payment_hex": payment.hex(),
                    "payment_id": ident,
                    "before_pending": before["pending_transactions"],
                    "restored_pending": restored["pending_transactions"],
                    "sender_native_admissions": before["peer_transaction_intake"]["sv"][
                        "admitted"
                    ],
                }
                assert fresh.rpc("getrawmempool") == []
                (peer,) = fresh.rpc("getpeerinfo")
                record["new_peer_tx_bytes"] = peer["bytessent_per_msg"].get("tx", 0)
                assert record["new_peer_tx_bytes"] == 0
                if baseline:
                    assert record["restored_pending"] == 0
                    record["worker"] = worker_without_job(worker)
                    return record
                assert (
                    exit_receipt["exit_code"] == 0
                    and record["restored_pending"] == 1
                    and restored["job_preconditions_met"]
                )
                with stratum_job(
                    parent, payment, worker, api_port, "restored-pending"
                ) as job:
                    solution = nonce(job)
                    solved = dsha(job.header(solution))[::-1].hex()
                    assert job.submit_native(solution)
                until(
                    lambda: fresh.rpc("getbestblockhash") == solved,
                    "independent receiver selected restored wallet payment",
                )
                record["solved_block"] = fresh.rpc("getblock", solved, 0)
                assert fresh.rpc("getblock", solved)["tx"][1:] == [ident]
                successor = fresh.rpc("generate", 1)[0]
                until(
                    lambda: mining()["tip_hash"] == successor,
                    "stock extended durable pending work",
                )
                record["stock_successor_header"] = fresh.rpc(
                    "getblockheader", successor, False
                )
                return record
        finally:
            feed.server.shutdown()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("binary", type=Path)
    p.add_argument("output", type=Path)
    p.add_argument("--baseline", action="store_true")
    args = p.parse_args()
    if args.output.exists():
        p.error("choose a fresh output")
    rows = []
    # Each signal runs with a separate chain, two independent full nodes and one offer.
    for sig in (signal.SIGINT, signal.SIGTERM):
        with session_lock(), tempfile.TemporaryDirectory() as tmp:
            rows.append(scenario(args.binary.resolve(), sig, args.baseline, Path(tmp)))
    with args.output.open("x") as f:
        json.dump(rows, f, indent=2)
        f.write("\n")
