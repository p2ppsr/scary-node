"""Keep the first funded job open across many independently observed same-parent revisions."""

import argparse
import contextlib
import gzip
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import time

from native_workers import connect_worker, network_fixture, offer, settlement
from run_cohort import session_lock
from sv_reference import IMAGE
from verify_native_workers import (
    REGTEST_TARGET,
    build_candidate,
    dsha,
    require,
    verify_retained,
)


def run(binary, directory, refreshes):
    binary = binary.resolve()
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    with network_fixture(binary, directory, refreshes + 1) as (
        api,
        port,
        password,
        refs,
        parent,
        payments,
    ):
        offer(api, refs[0], payments[0])
        with contextlib.ExitStack() as connection:
            worker = connect_worker(connection, port, "retained-worker")
            first = worker.setup(password)
            job = first["first_job"]
            x2 = "00" * first["extranonce2_bytes"]
            prefix, _ = build_candidate(job, first["extranonce1"], x2, payments[:1])
            low_nonce, valid_nonce = None, None
            for n in range(4096):
                digest_work = dsha(prefix + struct.pack("<I", n))
                if int.from_bytes(digest_work, "little") <= REGTEST_TARGET:
                    valid_nonce = n
                else:
                    low_nonce = n
                if low_nonce is not None and valid_nonce is not None:
                    break
            require(
                low_nonce is not None and valid_nonce is not None, "regtest nonce pair"
            )
            revisions = []
            for offset, payment in enumerate(payments[1:], start=1):
                offer(api, refs[0], payment)
                with contextlib.ExitStack() as observer_conn:
                    observer = connect_worker(
                        observer_conn, port, f"revision-reader-{offset}"
                    )
                    observed = observer.setup(password)
                    build_candidate(
                        observed["first_job"],
                        observed["extranonce1"],
                        x2,
                        payments[: offset + 1],
                    )
                # Give the retained worker a full 150 ms Stratum refresh tick between revisions.
                time.sleep(0.2)
                probe = worker.submit(10 + offset, job, x2, low_nonce)
                require(
                    probe["error"] and probe["error"][0] == 23,
                    "old job must remain active",
                )
                revisions.append(
                    {"index": offset, "reader": observed, "original_share": probe}
                )
            ack = worker.submit(1000, job, x2, valid_nonce)
            require(ack["result"] is True, "original job solution refused")
            winner = dsha(prefix + struct.pack("<I", valid_nonce))[::-1].hex()
            notifications = list(worker.notifications)
        return {
            "schema": 1,
            "kind": "retained_job",
            "reference": IMAGE,
            "binary_sha256": digest,
            "parent": parent,
            "payments": [p.hex() for p in payments],
            "worker": first,
            "revisions": revisions,
            "original_notifications": notifications,
            "extranonce2": x2,
            "low_nonce": low_nonce,
            "nonce": valid_nonce,
            "ack": ack,
            "winner": winner,
            **settlement(api, refs, winner, 0),
        }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("receipt", type=Path)
    p.add_argument("--binary", type=Path, required=True)
    p.add_argument("--refreshes", type=int, default=16)
    args = p.parse_args()
    if not 9 <= args.refreshes <= 100:
        p.error("use 9 to 100 same-parent refreshes")
    with session_lock(), tempfile.TemporaryDirectory() as d:
        result = run(args.binary, Path(d), args.refreshes)
    verify_retained(result)
    args.receipt.write_bytes(
        gzip.compress((json.dumps(result, indent=2) + "\n").encode(), mtime=0)
    )
