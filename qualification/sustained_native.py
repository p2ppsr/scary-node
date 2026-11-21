"""Run the prospectively registered 30-minute peer-fed mixed regtest and retain all opportunities."""

import argparse
import concurrent.futures
import contextlib
import gzip
import hashlib
import json
import mmap
import multiprocessing
from pathlib import Path
import socket
import struct
import threading
import time

from mixed_storage_trial import NODES, PRODUCTION_REPLICATION, rpc, status, sv, wait
from native_workload import FromHex, ToHex, CTransaction, solve
from native_workers import Worker
from prove import PORT_SHIFT, decode_block, dsha, request, txid
from run_cohort import session_lock
from sustained_profiles import (
    CHANGED_RELEASE,
    FROZEN_RELEASE,
    REGISTRATIONS,
    WITNESSED_ARMS,
    cohort_by_batch,
    profile,
)
from sustained_corpus import BATCH, COUNT
from sustained_wallet import produce, wait_until, Wallet
from verify_native_workers import previous_hash

RELEASE = CHANGED_RELEASE if profile() == "cold" else FROZEN_RELEASE
DURATION = 1800
INTERVAL = 6
ARM = profile()
REGISTRATION = REGISTRATIONS.get(ARM)


def append(log, row):
    log.write(json.dumps(row, separators=(",", ":")) + "\n")
    log.flush()


def readlines(path):
    return (
        [json.loads(s) for s in path.read_text().splitlines()] if path.exists() else []
    )


def prepare_funding(corpus):
    manifest = json.loads((corpus / "manifest.json").read_text())
    base = manifest["predecessor_hash"]
    saved = corpus / "funding.block"
    if not saved.exists():
        if sv("getbestblockhash") != base:
            raise ValueError("funding source tip differs")
        transactions = []
        for path in sorted(corpus.glob("funding-*.bin")):
            with path.open("rb") as f:
                while n := f.read(4):
                    size = struct.unpack("<I", n)[0]
                    transactions.append(FromHex(CTransaction(), f.read(size).hex()))
        if len(transactions) != 16384:
            raise ValueError("incomplete ordinary funding splits")
        header = sv("getblock", base)
        block = solve(base, header["height"] + 1, header["time"] + 1, transactions)
        saved.write_bytes(bytes.fromhex(ToHex(block)))
    wire = saved.read_bytes()
    h = txid(wire[:80])
    if sv("getbestblockhash") == base:
        if sv("submitblock", wire.hex()) is not None:
            raise ValueError("SV refused funding setup")

    def observed():
        mining = status()["mining"]
        row = next((r for r in mining["validation_window"] if r["hash"] == h), {})
        return (
            sv("getbestblockhash") == h
            and all(rpc(n, "getblockchaininfo")["bestblockhash"] == h for n in NODES)
            and mining["tip_hash"] == h
            and row.get("script_checks_passed") == 16384
        )

    wait("funding splits validated by all stores", observed, seconds=180)
    return {
        "height": manifest["predecessor_height"] + 1,
        "hash": h,
        "wire_sha256": hashlib.sha256(wire).hexdigest(),
        "created_payment_rails": 65536,
    }


def stratum(parent):
    with (
        socket.create_connection(("127.0.0.1", 19433 + PORT_SHIFT), timeout=4) as conn,
        conn.makefile("rwb") as stream,
    ):
        conn.settimeout(4)
        worker = Worker(stream, "sustained-native")
        subscribed = worker.setup("")
        j = subscribed["first_job"]
        if previous_hash(j)[::-1].hex() != parent:
            raise ValueError("worker received a different parent")
        if j[6] != "207fffff":
            raise ValueError("unexpected regtest work target")
        x2 = "00" * subscribed["extranonce2_bytes"]
        root = dsha(bytes.fromhex(j[2] + subscribed["extranonce1"] + x2 + j[3]))
        for branch in j[4]:
            root = dsha(root + bytes.fromhex(branch))
        prefix = (
            bytes.fromhex(j[5])[::-1]
            + previous_hash(j)
            + root
            + bytes.fromhex(j[7])[::-1]
            + bytes.fromhex(j[6])[::-1]
        )
        nonce = next(
            i
            for i in range(4096)
            if int.from_bytes(dsha(prefix + struct.pack("<I", i)), "little")
            <= 0x7FFFFF << 232
        )
        hdr = prefix + struct.pack("<I", nonce)
        result = worker.submit(4, j, x2, nonce)
        return txid(hdr), hdr.hex(), result


def mine(kind, phase, index, due, start):
    r = {
        "miner": kind,
        "phase": phase,
        "index": index,
        "scheduled_ns": due,
        "started_ns": time.monotonic_ns() - start,
        "parent": None,
        "ready": False,
    }
    try:
        if kind == "scary":
            m = status()["mining"]
            r["parent"] = m["tip_hash"]
            r["receiver_ready"] = bool(
                m["observation_window_ready"]
                and m["connected_data_peers"]
                and not m["storage_failure"]
            )
            r["job_wait_reason"] = m.get("job_wait_reason")
            r["ready"] = r["receiver_ready"] and not r["job_wait_reason"]
            r["pending_before"] = m["pending_transactions"]
            if not r["ready"]:
                r["outcome"] = "unready"
            else:
                h, header, result = stratum(r["parent"])
                r["outcome"] = "solved" if result["result"] is True else "rejected"
                r["solution_header"] = header
                if r["outcome"] == "solved":
                    r["hash"] = h
                else:
                    r["reason"] = str(result["error"])
        else:
            m = sv("getblockchaininfo")
            r["parent"] = m["bestblockhash"]
            r["ready"] = m["chain"] == "regtest"
            if r["ready"]:
                r["hash"] = sv("generate", 1)[0]
                r["outcome"] = "solved"
            else:
                r["outcome"] = "unready"
    except Exception as e:
        r["outcome"] = "error"
        r["reason"] = str(e)[:256]
    r["finished_ns"] = time.monotonic_ns() - start
    return r


class Live:
    def __init__(self, base):
        self.lock = threading.RLock()
        self.last = {}
        self.observations = {}
        self.first = {}
        self.base = base
        self.last_audited = base["height"]
        self.confirmed = 0
        self.last_payment = base["height"]
        self.audit_error = None

    def sample(self, start):
        elapsed = lambda: time.monotonic_ns() - start
        row = {"elapsed_ns": elapsed()}
        for name, fn in [
            ("sv", lambda: sv("getblockchaininfo")),
            ("tn1", lambda: rpc(NODES[0], "getblockchaininfo")),
            ("tn2", lambda: rpc(NODES[1], "getblockchaininfo")),
            ("scary", status),
        ]:
            try:
                got = fn()
                if name == "scary":
                    m = got["mining"]
                    st = {
                        "height": m["height"],
                        "hash": m["tip_hash"],
                        "ready": m["observation_window_ready"],
                        "work_ready": not m.get("job_wait_reason")
                        and m["observation_window_ready"],
                        "job_wait_reason": m.get("job_wait_reason"),
                        "pending": m["pending_transactions"],
                        "connected_peers": m["connected_data_peers"],
                        "peer_intake": m["peer_transaction_intake"],
                        "storage_failed": bool(m["storage_failure"]),
                    }
                    with self.lock:
                        for obs in m["validation_window"]:
                            self.observations[obs["hash"]] = (
                                obs["height"],
                                obs["script_checks_passed"],
                            )
                else:
                    st = {"height": got["blocks"], "hash": got["bestblockhash"]}
                st["read_ns"] = elapsed()
                with self.lock:
                    self.first.setdefault((name, st["hash"]), st["read_ns"])
            except Exception as e:
                st = {"error": type(e).__name__}
            row[name] = st
        with self.lock:
            self.last = row
        return row


def sample_loop(live, directory, start, stop):
    with (directory / "samples.jsonl").open("x", buffering=1) as log:
        n = 0
        while not stop.is_set():
            wait_until(start + n * 1_000_000_000, stop)
            if stop.is_set():
                break
            append(log, live.sample(start))
            n = max(n + 1, (time.monotonic_ns() - start) // 1_000_000_000 + 1)


def read_block(node, h):
    for attempt in range(7):
        try:
            return request(f"http://127.0.0.1:{node[1]}/api/v1/block_legacy/{h}?wire=1")
        except (OSError, RuntimeError):
            if attempt == 6:
                raise
            time.sleep(min(2.0, 0.1 * 2**attempt))


def audit_loop(ids, seen, directory, live, start, stop, abort):
    blocks = directory / "blocks"
    blocks.mkdir()
    active = []
    base = live.base
    source_cohorts = cohort_by_batch(ARM)
    next_height = base["height"] + 1
    try:
        with (
            (directory / "body-events.jsonl").open("x", buffering=1) as log,
            concurrent.futures.ThreadPoolExecutor(3) as fetch,
        ):
            while not stop.is_set():
                with live.lock:
                    snapshot = live.last.copy()
                common = min(
                    (
                        snapshot.get(k, {}).get("height", 0) or 0
                        for k in ("sv", "tn1", "tn2", "scary")
                    ),
                    default=0,
                )
                if next_height > common:
                    stop.wait(0.15)
                    continue
                h = sv("getblockhash", next_height)
                if any(rpc(n, "getblockhash", next_height) != h for n in NODES):
                    stop.wait(0.2)
                    continue
                with live.lock:
                    observed = live.observations.get(h)
                if not observed:
                    stop.wait(0.15)
                    continue
                reads = [fetch.submit(read_block, n, h) for n in NODES]
                sv_body = bytes.fromhex(sv("getblock", h, 0))
                _, txs, _ = decode_block(sv_body, h)
                wire = hashlib.sha256(sv_body).hexdigest()
                hashes = {
                    "sv": wire,
                    **{
                        name: hashlib.sha256(f.result()).hexdigest()
                        for name, f in zip(("tn1", "tn2"), reads)
                    },
                }
                if set(hashes.values()) != {wire}:
                    raise ValueError("native wire disagreement")
                parent = sv_body[4:36][::-1].hex()
                want = active[-1]["hash"] if active else base["hash"]
                if parent != want:
                    if not active:
                        raise ValueError("reorg crosses initial funding boundary")
                    old = active.pop()
                    for (ordinal,) in struct.iter_unpack(
                        "<I", (blocks / (old["hash"] + ".index")).read_bytes()
                    ):
                        seen[ordinal] = 0
                    with live.lock:
                        live.confirmed -= old["scheduled_payments"]
                        live.last_audited -= 1
                        live.last_payment = next(
                            (
                                r["height"]
                                for r in reversed(active)
                                if r["scheduled_payments"]
                            ),
                            base["height"],
                        )
                    append(log, {"detach": old["hash"], "height": old["height"]})
                    next_height -= 1
                    continue
                counts = [0] * 6
                unexpected = duplicates = 0
                indices = bytearray()
                for tx in txs[1:]:
                    ordinal = ids.get(dsha(tx))
                    if ordinal is None:
                        unexpected += 1
                    elif seen[ordinal]:
                        duplicates += 1
                    else:
                        seen[ordinal] = 1
                        counts[source_cohorts[ordinal // BATCH]] += 1
                        indices.extend(struct.pack("<I", ordinal))
                (blocks / (h + ".block")).write_bytes(sv_body)
                (blocks / (h + ".index")).write_bytes(indices)
                with live.lock:
                    first = {
                        k: live.first.get((k, h)) for k in ("sv", "tn1", "tn2", "scary")
                    }
                r = {
                    "height": next_height,
                    "hash": h,
                    "header_hex": sv_body[:80].hex(),
                    "wire_sha256": wire,
                    "wire_bytes": len(sv_body),
                    "independent_wire_sha256": hashes,
                    "ordinary_transactions": len(txs) - 1,
                    "scheduled_payments": sum(counts),
                    "foreign_payments": unexpected,
                    "duplicate_scheduled": duplicates,
                    "confirmed_by_source_cohort": counts,
                    "entrant_script_checks": observed[1],
                    "entrant_hash": h,
                    "first_observed_ns": first,
                    "readback_ns": time.monotonic_ns() - start,
                }
                append(log, r)
                active.append(r)
                with live.lock:
                    live.last_audited = next_height
                    live.confirmed += sum(counts)
                    if sum(counts):
                        live.last_payment = next_height
                next_height += 1
    except Exception as e:
        with live.lock:
            live.audit_error = type(e).__name__ + ": " + str(e)[:280]
        abort.set()


def rebroadcast(corpus, directory, seen, start, stop):
    offsets = json.loads((corpus / "offsets.json").read_text())
    with (
        (directory / "retries.jsonl").open("x", buffering=1) as log,
        (corpus / "frames.bin").open("rb") as src,
        contextlib.closing(Wallet(4)) as wallet,
    ):
        pace = time.monotonic_ns()
        for i, (lo, hi) in enumerate(zip(offsets, offsets[1:])):
            if stop.is_set():
                break
            bits = seen[i * BATCH : (i + 1) * BATCH]
            if all(bits):
                continue
            src.seek(lo)
            raw = src.read(hi - lo)
            pos = 0
            selected = bytearray()
            count = 0
            for b in bits:
                end = pos + 24 + struct.unpack_from("<I", raw, pos + 16)[0]
                if not b:
                    selected.extend(raw[pos:end])
                    count += 1
                pos = end
            wait_until(pace, stop)
            if stop.is_set():
                break
            begin = time.monotonic_ns() - start
            try:
                wallet.send(selected)
                result = "sent"
            except OSError:
                result = "error"
            append(
                log,
                {
                    "phase": "rebroadcast",
                    "batch": i,
                    "count": count,
                    "sender": 4,
                    "scheduled_ns": pace - start,
                    "started_ns": begin,
                    "finished_ns": time.monotonic_ns() - start,
                    "outcome": result,
                },
            )
            pace = max(pace, time.monotonic_ns()) + count * 1_000_000_000 // 3072


def load_ids(corpus, source):
    ids = {}
    with (
        (corpus / "ids.bin").open("rb") as f,
        mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data,
    ):
        if (
            len(data) != COUNT * 32
            or hashlib.sha256(data).hexdigest() != source["offered_id_sha256"]
        ):
            raise ValueError("signed ID artifact differs")
        for i in range(COUNT):
            ids[data[32 * i : 32 * i + 32]] = i
    if len(ids) != COUNT:
        raise ValueError("source contains duplicate signed payment")
    return ids


def run(corpus, directory, *, setup=None, resume_genesis=False):
    from sustained_resources import sample_resources

    src = json.loads((corpus / "manifest.json").read_text())
    directory.mkdir(parents=True, exist_ok=True)
    start_ns = 0
    with session_lock():
        if not resume_genesis and any(directory.iterdir()):
            raise ValueError("timed workload requires an empty output directory")
        if resume_genesis and (
            not setup or {item.name for item in directory.iterdir()} != {"setup.json"}
        ):
            raise ValueError("cannot repeat or resume a timed sustained workload")
        base = setup(directory) if setup else prepare_funding(corpus)
        if status()["release"]["executable_sha256"] != RELEASE:
            raise ValueError("running Linux image differs")
        ids = load_ids(corpus, src)
        seen = bytearray(COUNT)
        live = Live(base)
        ctx = multiprocessing.get_context("spawn")
        start_value = ctx.Value("Q", 0)
        peer_ready = ctx.Event()
        wallet_stop = ctx.Event()
        wallet_args = (corpus, directory, start_value, peer_ready, wallet_stop)
        if PRODUCTION_REPLICATION:
            from sustained_inventory import produce_inventory, request_reannouncement
            from sustained_stock import capture

            retry_channel = ctx.Queue(maxsize=32)
            wallet = ctx.Process(
                target=produce_inventory, args=(*wallet_args, retry_channel)
            )
        else:
            wallet = ctx.Process(target=produce, args=wallet_args)
        wallet.start()
        if not peer_ready.wait(55) or not wallet.is_alive():
            raise ValueError("wallets failed preflight")
        stop = threading.Event()
        resource_stop = threading.Event()
        abort = threading.Event()
        threads = []

        def launch(fn, *args):
            t = threading.Thread(target=fn, args=args, daemon=True)
            t.start()
            threads.append(t)

        start_ns = time.monotonic_ns() + 1_000_000_000
        start_value.value = start_ns
        launch(sample_loop, live, directory, start_ns, stop)
        launch(
            sample_resources,
            directory / "resources.jsonl",
            start_ns,
            stop,
            resource_stop,
        )
        launch(audit_loop, ids, seen, directory, live, start_ns, stop, abort)
        start_wall = int(time.time_ns() + (start_ns - time.monotonic_ns()))
        (directory / "attempt.json").write_text(
            json.dumps(
                {
                    "registration": REGISTRATION,
                    **({"arm": ARM} if ARM in WITNESSED_ARMS else {}),
                    "start_wall_ns": start_wall,
                    "baseline": base,
                    "release_sha256": RELEASE,
                    "source": src,
                }
            )
            + "\n"
        )
        if PRODUCTION_REPLICATION:
            launch(capture, directory, sv, stop)
        settle_slots = 0
        try:
            with (
                (directory / "slots.jsonl").open("x", buffering=1) as log,
                concurrent.futures.ThreadPoolExecutor(1) as worker,
            ):
                pending = None
                retry_started = False
                next_slot = 0
                while True:
                    now = time.monotonic_ns() - start_ns
                    if (
                        resource_stop.is_set()
                        or abort.is_set()
                        or now >= (DURATION + 300) * 1_000_000_000
                    ):
                        break
                    if pending and pending.done():
                        completed = pending.result()
                        append(log, completed)
                        if (
                            completed["phase"] == "settle"
                            and completed["outcome"] == "solved"
                        ):
                            settle_slots += 1
                        pending = None
                    with live.lock:
                        done = live.confirmed == COUNT
                        settled = done and live.last_audited >= live.last_payment + 2
                    if settled and next_slot >= 300:
                        break
                    if (
                        now >= (DURATION + 18) * 1_000_000_000
                        and not done
                        and not retry_started
                    ):
                        if PRODUCTION_REPLICATION:
                            launch(
                                request_reannouncement,
                                seen,
                                start_ns,
                                stop,
                                retry_channel,
                            )
                        else:
                            launch(rebroadcast, corpus, directory, seen, start_ns, stop)
                        retry_started = True
                    due = (next_slot + 1) * INTERVAL * 1_000_000_000
                    if now < due:
                        time.sleep(min(0.1, (due - now) / 1e9))
                        continue
                    phase = (
                        "offer" if next_slot < 300 else ("settle" if done else "drain")
                    )
                    kind = "sv" if phase == "settle" or next_slot % 4 == 3 else "scary"
                    if pending or (phase == "settle" and settle_slots >= 2):
                        append(
                            log,
                            {
                                "index": next_slot,
                                "phase": phase,
                                "miner": kind,
                                "scheduled_ns": due,
                                "started_ns": now,
                                "finished_ns": now,
                                "parent": None,
                                "ready": False,
                                "outcome": "missed",
                            },
                        )
                    else:
                        pending = worker.submit(
                            mine, kind, phase, next_slot, due, start_ns
                        )
                    next_slot += 1
                if pending:
                    append(log, pending.result())
        finally:
            elapsed = time.monotonic_ns() - start_ns
            wallet_stop.set()
            stop.set()
            wallet.join(15)
            if wallet.is_alive():
                wallet.terminate()
                wallet.join(8)
            for t in threads:
                t.join(12)
        producer = readlines(directory / "producer.jsonl")
        slots = sorted(readlines(directory / "slots.jsonl"), key=lambda r: r["index"])
        blocks = []
        detached = []
        audit_events = readlines(directory / "body-events.jsonl")
        for row in audit_events:
            if "detach" in row:
                if blocks[-1]["hash"] != row["detach"]:
                    raise ValueError("recorded detach mismatch")
                detached.append(blocks.pop())
            else:
                blocks.append(row)
        final = blocks[-1] if blocks else base
        complete = {
            "all_accounted": live.confirmed == COUNT,
            "unique_confirmed": live.confirmed,
            "missing": COUNT - live.confirmed,
            "duplicate_confirmations": sum(x["duplicate_scheduled"] for x in blocks),
            "last_payment_height": live.last_payment,
            "final_height": final["height"],
            "final_hash": final["hash"],
            "resource_stop": resource_stop.is_set(),
            "primary_complete": len([x for x in slots if x["phase"] == "offer"]) == 300
            and len(producer) == 10800,
            "audit_failure": live.audit_error,
            "two_successors": live.confirmed == COUNT
            and final["height"] >= live.last_payment + 2,
        }
        result = {
            "schema": 1,
            "registration": REGISTRATION,
            **({"arm": ARM} if ARM in WITNESSED_ARMS else {}),
            "network": "regtest",
            "release_sha256": RELEASE,
            "source": src,
            "baseline": base,
            "start_wall_ns": start_wall,
            "duration_ns": elapsed,
            "completion": complete,
            "producer": producer + readlines(directory / "retries.jsonl"),
            "slots": slots,
            "blocks": blocks,
            "samples": readlines(directory / "samples.jsonl"),
            "resources": readlines(directory / "resources.jsonl"),
            "wallets": json.loads((directory / "wallet-outcomes.json").read_text())
            if (directory / "wallet-outcomes.json").exists()
            else [],
        }
        if ARM in WITNESSED_ARMS:
            result["detached_observations"] = detached
            result["branch_events"] = [
                {"detach": row["detach"], "height": row["height"]}
                if "detach" in row
                else {"attach": row["hash"], "height": row["height"]}
                for row in audit_events
            ]
        if PRODUCTION_REPLICATION:
            result["source_protocol"] = "inv-getdata-tx"
            result["inventory_progress"] = readlines(
                directory / "inventory-progress.jsonl"
            )
            final_inventory = directory / "inventory-final.json"
            result["inventory_final"] = (
                json.loads(final_inventory.read_text())
                if final_inventory.exists()
                else None
            )
            result["stock"] = readlines(directory / "stock.jsonl")
            result["setup"] = json.loads((directory / "setup.json").read_text())
            retry_channel.cancel_join_thread()
            retry_channel.close()
        tmp = directory / "receipt.next"
        tmp.write_bytes(
            gzip.compress(json.dumps(result, separators=(",", ":")).encode(), mtime=0)
        )
        tmp.replace(directory / "receipt.json.gz")
        if ARM in WITNESSED_ARMS:
            from quiesce_sustained import quiesce

            quiesce(directory)
        return complete


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("corpus", type=Path)
    p.add_argument("directory", type=Path)
    args = p.parse_args()
    print(json.dumps(run(args.corpus, args.directory), indent=2))
