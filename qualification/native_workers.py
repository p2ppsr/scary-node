"""Exercise concurrent authenticated Stratum clients and settle their exact work on two SV receivers."""

import argparse
import contextlib
import gzip
import hashlib
import json
from pathlib import Path
import secrets
import socket
import struct
import sys
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import time
import uuid

from native_cohort import funding_transactions, native_link
from run_cohort import session_lock
from scary_dar import LabFeed, entrant, get_port, response, until
from sv_reference import IMAGE, Reference
from verify_native_workers import REGTEST_TARGET, build_candidate, verify
from prove import dsha


def require(condition, message):
    if not condition:
        raise ValueError(message)


class Worker:
    def __init__(self, stream, name):
        self.stream = stream
        self.name = name
        self.notifications = []

    def send(self, seq, method, params):
        self.stream.write(
            json.dumps({"id": seq, "method": method, "params": params}).encode() + b"\n"
        )
        self.stream.flush()

    def read(self):
        line = self.stream.readline(8193)
        if not line:
            raise EOFError("Stratum session closed")
        require(
            len(line) <= 8192 and line.endswith(b"\n"), "incomplete Stratum message"
        )
        message = json.loads(line)
        if message.get("method") == "mining.notify":
            self.notifications.append(message["params"])
        return message

    def setup(self, password):
        self.send(1, "mining.subscribe", ["scary-regtest-worker"])
        self.send(2, "mining.authorize", [self.name, password])
        subscription, difficulty, job, authorized = None, None, None, False
        while not (subscription and difficulty and job and authorized):
            msg = self.read()
            require(msg.get("error") is None, "Stratum initialization refused")
            if msg.get("id") == 1:
                subscription = msg["result"]
            elif msg.get("id") == 2:
                authorized = msg["result"] is True
            elif msg.get("method") == "mining.set_difficulty":
                difficulty = msg["params"][0]
            elif msg.get("method") == "mining.notify":
                job = msg["params"]
        return {
            "name": self.name,
            "extranonce1": subscription[1],
            "extranonce2_bytes": subscription[2],
            "authorized": authorized,
            "share_difficulty": difficulty,
            "first_job": job,
        }

    def refresh(self):
        while True:
            msg = self.read()
            if msg.get("method") == "mining.notify":
                return msg["params"]

    def submit(self, seq, job, x2, nonce):
        self.send(seq, "mining.submit", [self.name, job[0], x2, job[7], f"{nonce:08x}"])
        while True:
            msg = self.read()
            if msg.get("id") == seq:
                return {"result": msg["result"], "error": msg["error"]}


def disposition(worker, row, order, payments, barrier, clock):
    job = row[order]
    selected = payments if order == "refreshed_job" else payments[:1]
    x2 = "00" * row["extranonce2_bytes"]
    prefix, _ = build_candidate(job, row["extranonce1"], x2, selected)
    nonce = next(
        n
        for n in range(4096)
        if int.from_bytes(dsha(prefix + struct.pack("<I", n)), "little")
        <= REGTEST_TARGET
    )
    block_hash = dsha(prefix + struct.pack("<I", nonce))[::-1].hex()
    barrier.wait(timeout=10)
    start = time.monotonic_ns() - clock
    try:
        outcome = worker.submit(4, job, x2, nonce)
    except (EOFError, BrokenPipeError, ConnectionResetError):
        outcome = {"closed": True}
    return {
        "job_used": order,
        "nonce": nonce,
        "extranonce2": x2,
        "hash": block_hash,
        "start_ns": start,
        "finish_ns": time.monotonic_ns() - clock,
        "outcome": outcome,
    }


def offer(api, reference, payment):
    expected = dsha(payment)[::-1].hex()
    require(
        reference.rpc("sendrawtransaction", payment.hex()) == expected,
        "full-state payment admission",
    )
    require(
        response(api + "/transactions", payment)["txid"] == expected,
        "witnessed payment admission",
    )


def connect_worker(resources, port, name):
    sock = resources.enter_context(
        socket.create_connection(("127.0.0.1", port), timeout=15)
    )
    return Worker(resources.enter_context(sock.makefile("rwb")), name)


def round_on_parent(api, port, password, refs, parent, payments, index, count):
    require(
        response(api + "/status")["mining"]["pending_transactions"] == 0,
        "previous round still has outstanding payments",
    )
    offer(api, refs[0], payments[0])
    with contextlib.ExitStack() as sockets, ThreadPoolExecutor(count) as pool:
        workers = []
        for i in range(count):
            workers.append(connect_worker(sockets, port, f"worker-{i + 1:02}"))
        rows = list(pool.map(lambda w: w.setup(password), workers))

        def check_low(args):
            w, row = args
            job = row["first_job"]
            x2 = "00" * row["extranonce2_bytes"]
            prefix, _ = build_candidate(job, row["extranonce1"], x2, payments[:1])
            n = next(
                x
                for x in range(4096)
                if int.from_bytes(dsha(prefix + struct.pack("<I", x)), "little")
                > REGTEST_TARGET
            )
            reply = w.submit(3, job, x2, n)
            require(reply["error"][0] == 23, "low-difficulty disposition")
            row["low_share"] = {"nonce": n, "outcome": reply}

        list(pool.map(check_low, zip(workers, rows)))
        offer(api, refs[0], payments[1])
        for row, fresh in zip(rows, pool.map(lambda w: w.refresh(), workers)):
            row["refreshed_job"] = fresh
        barrier = Barrier(count)
        clock = time.monotonic_ns()

        def contend(args):
            lane, w, row = args
            order = (
                "first_job"
                if index % 3 == 0 or (index % 3 == 2 and lane % 2 == 0)
                else "refreshed_job"
            )
            row.update(disposition(w, row, order, payments, barrier, clock))

        list(pool.map(contend, zip(range(count), workers, rows)))
    winners = [r for r in rows if r["outcome"].get("result") is True]
    require(len(winners) == 1, "exactly one same-parent solution must be acknowledged")
    winner = winners[0]["hash"]
    return {
        "index": index,
        "parent": parent,
        "payments": [p.hex() for p in payments],
        "workers": rows,
        "winner": winner,
        **settlement(
            api,
            refs,
            winner,
            index,
            payments[1:] if winners[0]["job_used"] == "first_job" else [],
        ),
    }


def settlement(api, refs, winner, index, outstanding=()):
    until(
        lambda: all(n.rpc("getbestblockhash") == winner for n in refs),
        "both SV receivers adopted exact worker hash",
    )
    height = refs[0].rpc("getblockcount")
    bodies = [n.rpc("getblock", winner, 0) for n in refs]
    require(bodies[0] == bodies[1], "independent receiver bodies differ")
    continuation = refs[(index + 1) % 2]
    remaining_ids = {dsha(p)[::-1].hex() for p in outstanding}
    if remaining_ids:
        for p in outstanding:
            pid = dsha(p)[::-1].hex()
            if pid not in continuation.rpc("getrawmempool"):
                require(
                    continuation.rpc("sendrawtransaction", p.hex()) == pid,
                    "carryover admission on continuation peer",
                )
        until(
            lambda: remaining_ids.issubset(
                {
                    t["hash"]
                    for t in continuation.rpc("getblocktemplate")["transactions"]
                }
            ),
            "continuation miner committed the remaining payments",
        )
    successors = continuation.rpc("generate", 2)
    confirmed = {tx for h in successors for tx in continuation.rpc("getblock", h)["tx"]}
    require(remaining_ids <= confirmed, "unspent payment missing from successor bodies")
    tip = successors[-1]
    until(
        lambda: all(n.rpc("getbestblockhash") == tip for n in refs),
        "two peer continuations",
    )

    def settled():
        view = response(api + "/status")["mining"]
        if view["tip_hash"] == tip and any(
            s["hash"] == winner and s["chain_state"] == "settled"
            for s in view["solutions"]
        ):
            return view

    status = until(settled, "witnessed solution settled")
    record = next(s for s in status["solutions"] if s["hash"] == winner)
    return {
        "height": height,
        "winning_wire": bodies[0],
        "peers": [
            {
                "name": f"sv-{i+1}",
                "active_at_height": n.rpc("getblockhash", height),
                "winning_wire_sha256": hashlib.sha256(bytes.fromhex(b)).hexdigest(),
                "tip": n.rpc("getbestblockhash"),
                "successor_headers": [
                    n.rpc("getblockheader", h, False) for h in successors
                ],
            }
            for i, (n, b) in enumerate(zip(refs, bodies))
        ],
        "local": {
            k: record[k]
            for k in ("hash", "parent", "height", "job", "chain_state", "transitions")
        },
        "accepted_shares": status["accepted_shares"],
        "solved_blocks": status["solved_blocks"],
    }


@contextlib.contextmanager
def network_fixture(binary, directory, payment_count):
    network = "scary-native-workers-" + uuid.uuid4().hex
    with contextlib.ExitStack() as life:
        subprocess.run(
            ["docker", "network", "create", network],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        life.callback(
            subprocess.run,
            ["docker", "network", "rm", network],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        refs = [
            life.enter_context(
                Reference().running(peer=True, fast_rpc=True, network=network)
            )
            for _ in range(2)
        ]
        native_link(refs[0], refs[1])
        base = refs[0].rpc("generate", 105)[-1]
        until(
            lambda: refs[1].rpc("getbestblockhash") == base, "common reference history"
        )
        feed = LabFeed(refs[0], directory)
        life.callback(feed.server.shutdown)
        password = secrets.token_hex(24)
        credential = directory / "worker-password"
        credential.touch(mode=0o600)
        credential.write_text(password)
        port = get_port()
        peer2 = refs[1].docker("port", refs[1].container, "18444/tcp")
        api = life.enter_context(
            entrant(
                binary,
                refs[0],
                feed,
                directory,
                get_port(),
                port,
                extra_args=[
                    "--worker-password-file",
                    str(credential),
                    "--sv-peer",
                    peer2,
                ],
            )
        )
        funding, offers = funding_transactions(refs[0], payment_count)
        refs[0].rpc("sendrawtransaction", funding)
        h = refs[0].rpc("generate", 1)[0]
        until(
            lambda: response(api + "/status")["mining"]["tip_hash"] == h,
            "witnessed funding",
        )
        parent = refs[0].rpc("generate", 1)[0]
        until(
            lambda: response(api + "/status")["mining"]["tip_hash"] == parent
            and refs[1].rpc("getbestblockhash") == parent,
            "common eligible parent",
        )
        yield api, port, password, refs, parent, offers


def run(binary, directory, count, rounds):
    binary = binary.resolve()
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    with network_fixture(binary, directory, 2 * rounds) as (
        api,
        port,
        password,
        refs,
        parent,
        offers,
    ):
        records = []
        for index in range(rounds):
            record = round_on_parent(
                api,
                port,
                password,
                refs,
                parent,
                offers[2 * index : 2 * index + 2],
                index,
                count,
            )
            records.append(record)
            print(
                f"Verified parent {index + 1}/{rounds}, {count} workers, winner {record['winner']}",
                file=sys.stderr,
                flush=True,
            )
            parent = record["peers"][0]["tip"]
        return {
            "schema": 1,
            "binary_sha256": digest,
            "reference": IMAGE,
            "worker_count": count,
            "round_count": rounds,
            "credential_required": True,
            "rounds": records,
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument(
        "--workers", type=int, default=32, choices=range(2, 33), metavar="2..32"
    )
    parser.add_argument("--rounds", type=int, default=12)
    args = parser.parse_args()
    if args.rounds < 3:
        parser.error("use at least three rounds to test all job assignments")
    with session_lock(), tempfile.TemporaryDirectory() as directory:
        result = run(args.binary, Path(directory), args.workers, args.rounds)
    verify(result)
    args.receipt.write_bytes(
        gzip.compress((json.dumps(result, indent=2) + "\n").encode(), mtime=0)
    )
