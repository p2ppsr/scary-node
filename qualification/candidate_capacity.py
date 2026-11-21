"""Check how a small configured block budget affects a funded pool and actual Stratum work."""

import argparse
import contextlib
from hashlib import sha256
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
from unittest.mock import patch
from urllib.error import HTTPError

from native_workers import connect_worker, network_fixture
from run_cohort import session_lock
from scary_dar import response, until
from sv_reference import IMAGE
from verify_native_workers import REGTEST_TARGET, build_candidate, dsha


def run(binary, directory):
    config = {"SCARY_MAX_BLOCK_BYTES": "1024", "SCARY_MAX_PENDING_BYTES": "2048"}
    payout = "a914" + "ab" * 20 + "87"
    startup = subprocess.run(
        [
            str(binary),
            "--network",
            "regtest",
            "--sv-peer",
            "127.0.0.1:1",
            "--payout-script",
            payout,
            "--data-dir",
            str(directory / "policy"),
        ],
        capture_output=True,
        timeout=10,
    )
    if (
        startup.returncode == 0
        or b"P2SH mining payout is ineligible" not in startup.stderr
    ):
        raise AssertionError("unexpected disposition for a P2SH mining payout")
    with (
        patch.dict(os.environ, config),
        network_fixture(binary, directory, 8) as (
            api,
            port,
            password,
            refs,
            parent,
            payments,
        ),
    ):
        accepted, declined = [], []
        for raw in payments:
            try:
                reply = response(api + "/transactions", raw)
                assert reply["txid"] == dsha(raw)[::-1].hex()
                accepted.append(raw)
            except HTTPError as error:
                assert error.code == 422
                reason = json.load(error)["error"]
                assert reason == "candidate exceeds local body capacity"
                declined.append(
                    {"txid": dsha(raw)[::-1].hex(), "bytes": len(raw), "reason": reason}
                )
        assert accepted and declined and len(accepted) + len(declined) == len(payments)
        with contextlib.ExitStack() as connections:
            worker = connect_worker(connections, port, "size-check")
            initial = worker.setup(password)
            job = initial["first_job"]
            x2 = "00" * initial["extranonce2_bytes"]
            prefix, _ = build_candidate(job, initial["extranonce1"], x2, accepted)
            nonce = next(
                n
                for n in range(4096)
                if int.from_bytes(dsha(prefix + struct.pack("<I", n)), "little")
                <= REGTEST_TARGET
            )
            winner = dsha(prefix + struct.pack("<I", nonce))[::-1].hex()
            assert worker.submit(3, job, x2, nonce)["result"] is True
        for ref in refs:
            until(
                lambda: ref.rpc("getbestblockhash") == winner,
                "stock receiver adopted bounded block",
            )
        bodies = [bytes.fromhex(ref.rpc("getblock", winner, 0)) for ref in refs]
        wire = bodies[0]
        assert len(wire) <= 1024 and all(
            len(wire) + row["bytes"] > 1024 for row in declined
        )
        assert bodies[1] == wire
        accepted_ids = [dsha(raw)[::-1].hex() for raw in accepted]
        assert refs[0].rpc("getblock", winner)["tx"][1:] == accepted_ids
        successor = refs[1].rpc("generate", 1)[0]
        until(
            lambda: refs[0].rpc("getbestblockhash") == successor,
            "independent successor",
        )
        assert refs[1].rpc("getblock", successor)["previousblockhash"] == winner
        return {
            "binary_sha256": sha256(binary.read_bytes()).hexdigest(),
            "reference": IMAGE,
            "body_limit": 1024,
            "p2sh_mining_payout_refused": True,
            "parent": parent,
            "block": winner,
            "block_hex": wire.hex(),
            "wire_bytes": len(wire),
            "accepted_txids": accepted_ids,
            "declined": declined,
            "receiver_wire_sha256": [sha256(body).hexdigest() for body in bodies],
            "successor_header": refs[1].rpc("getblockheader", successor, False),
        }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("receipt", type=Path)
    p.add_argument("--binary", required=True, type=Path)
    a = p.parse_args()
    with session_lock(), tempfile.TemporaryDirectory() as d:
        receipt = run(a.binary.resolve(), Path(d))
    a.receipt.write_text(json.dumps(receipt, indent=2) + "\n")
