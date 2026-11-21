"""Four privately bridged stock nodes, native gate links, and single-block calibration."""

import argparse
import concurrent.futures
import contextlib
import hashlib
import json
import math
import os
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from cohort_archive import check, sha
from matched_propagation import (
    TARGET,
    capture,
    deliver,
    dsha,
    handshake,
    receiver_roster,
)
from native_cohort import full_job, funding_transactions
from run_cohort import session_lock
from scary_dar import LabFeed, entrant, get_port, response, stratum_job, until
from sv_reference import IMAGE, Reference

CALIBRATION_ROWS = 16


def command(*args):
    return subprocess.check_output(
        ["docker", *args], text=True, stderr=subprocess.STDOUT
    ).strip()


def atomic(path, value):
    tmp = path.with_suffix(".writing")
    with tmp.open("w") as f:
        json.dump(value, f, indent=2, allow_nan=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def pinned(binary, manifest):
    m = json.loads(manifest.read_text())
    check(
        m["platform"] == "darwin_arm64" and m["executable_sha256"] == sha(binary),
        "Mac release binary differs from build manifest",
    )
    return {
        k: m[k]
        for k in (
            "source_commit",
            "source_tree",
            "source_archive_sha256",
            "executable_sha256",
            "platform",
        )
    }


def endpoint(n):
    addr, port = n.docker("port", n.container, "18444/tcp").rsplit(":", 1)
    check(addr == "127.0.0.1", "regtest peer must be published on loopback")
    return addr, int(port)


def stock_link(n, label, stack):
    sock = stack.enter_context(socket.create_connection(endpoint(n), timeout=8))
    handshake(sock, label)
    return sock


@contextlib.contextmanager
def fixture(binary, payments, swap=False):
    with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as life:
        uid = uuid.uuid4().hex[:14]
        stocks = []
        networks = []
        for i in range(4):
            net = f"scary-matched-{uid}-{i}"
            command("network", "create", net)
            life.callback(command, "network", "rm", net)
            networks.append(net)
            stocks.append(
                life.enter_context(
                    Reference().running(peer=True, network=net, fast_rpc=True)
                )
            )
        if swap:
            stocks[0], stocks[1], stocks[2], stocks[3] = (
                stocks[1],
                stocks[0],
                stocks[3],
                stocks[2],
            )
        a, b, r0, r1 = stocks

        def import_fixture(ids):
            for h in ids:
                wire = a.rpc("getblock", h, 0)
                for other in (b, r0, r1):
                    answer = other.rpc("submitblock", wire)
                    check(answer in (None, "duplicate"), "fixture history not accepted")

        import_fixture(a.rpc("generate", 105))
        feed = LabFeed(a, Path(tmp))
        life.callback(feed.server.server_close)
        life.callback(feed.server.shutdown)
        operator, worker = get_port(), get_port()
        listener = life.enter_context(socket.create_server(("127.0.0.1", 0)))
        listener.settimeout(15)
        extra = ("--sv-peer", f"127.0.0.1:{listener.getsockname()[1]}")
        url = life.enter_context(
            entrant(binary, a, feed, Path(tmp), operator, worker, extra_args=extra)
        )
        scary = life.enter_context(listener.accept()[0])
        handshake(scary, "source-0")
        roots = [stock_link(a, "source-0", life), stock_link(b, "source-1", life)]
        dest = [
            [stock_link(n, f"l{i}-r{j}", life) for j, n in enumerate((r0, r1))]
            for i in range(2)
        ]
        until(
            lambda: receiver_roster((r0, r1)), "four isolated native gate connections"
        )
        funding, offers = funding_transactions(a, payments)
        a.rpc("sendrawtransaction", funding)
        funded = a.rpc("generate", 1)
        import_fixture(funded)
        until(
            lambda: response(url + "/status")["mining"]["tip_hash"] == funded[0],
            "witnessed funding",
        )
        base = a.rpc("generate", 1)
        import_fixture(base)
        until(
            lambda: response(url + "/status")["mining"]["tip_hash"] == base[0],
            "witnessed successor",
        )
        # Every source has its own Docker bridge. Measurement receivers have no inter-node route.
        membership = [
            json.loads(
                n.docker(
                    "inspect",
                    "--format",
                    "{{json .NetworkSettings.Networks}}",
                    n.container,
                )
            )
            for n in stocks
        ]
        check(
            all(len(v) == 1 for v in membership)
            and len({next(iter(v)) for v in membership}) == 4,
            "private stock bridges must be pairwise disjoint",
        )
        yield {
            "stocks": stocks,
            "api": url,
            "operator": operator,
            "worker": worker,
            "scary": scary,
            "roots": roots,
            "dest": dest,
            "offers": offers,
            "base": base[0],
            "networks": networks,
            "private_bridges": 4,
        }


def begin_offer(f, tx, pool):
    ident = dsha(tx)[::-1].hex()
    start = time.monotonic_ns()

    def send(n):
        try:
            result = (
                response(f["api"] + "/transactions", tx)["txid"]
                if n is None
                else n.rpc("sendrawtransaction", tx.hex())
            )
            return {
                "accepted": result == ident,
                "elapsed_ns": time.monotonic_ns() - start,
            }
        except (OSError, ValueError, RuntimeError) as exc:
            return {
                "accepted": False,
                "reason": str(exc) or type(exc).__name__,
                "elapsed_ns": time.monotonic_ns() - start,
            }

    return start, [pool.submit(send, n) for n in [*f["stocks"], None]]


def offer(f, tx):
    with concurrent.futures.ThreadPoolExecutor(5) as pool:
        start, attempts = begin_offer(f, tx, pool)
        return start, [a.result() for a in attempts]


def witnessed_job(f, stack, parent, tx):
    state = response(f["api"] + "/status")["mining"]
    if (
        state["tip_hash"] != parent
        or not state["observation_window_ready"]
        or state["pending_transactions"] != 1
    ):
        return None, state.get("job_wait_reason") or ", ".join(
            state["readiness_reasons"]
        ) or "common-payment template unavailable"
    return stack.enter_context(
        stratum_job(parent, tx, f["worker"], f["operator"], "matched")
    ), None


def committed_job(job, tx):
    for n in range(4096):
        head = job.header(n)
        if int.from_bytes(dsha(head), "little") <= TARGET:
            raw = head + b"\2" + job.coinbase + tx
            return n, {
                "hash": dsha(head)[::-1].hex(),
                "body_sha256": hashlib.sha256(raw).hexdigest(),
            }
    raise RuntimeError("single-source calibration PoW exhausted")


def timed_capture(t0, *args):
    raw = capture(*args)
    return raw, time.monotonic_ns() - t0


@contextlib.contextmanager
def measurement_lock(quiescence):
    # A shared workstation can supply its external load-drain receipt as a prerequisite.
    # Wait outside the shared lock so the workload that writes that receipt can finish.
    if quiescence is not None:
        while not quiescence.is_file():
            time.sleep(2)
    with session_lock():
        digest = None
        if quiescence is not None:
            check(
                bool(json.loads(quiescence.read_text())), "quiescence receipt is empty"
            )
            digest = sha(quiescence)
        yield digest


def calibration(path, binary, manifest, quiescence=None):
    build = pinned(binary, manifest)
    check(not path.exists(), "calibration output already exists")
    r = {
        "schema": 1,
        "kind": "single-source mirrored native transport calibration",
        "build": build,
        "reference_image": IMAGE,
        "planned_sources": [
            "scary" if i % 2 == 0 else "stock-right" for i in range(CALIBRATION_ROWS)
        ],
        "rows": [],
        "complete": False,
        "clock": {
            "implementation": time.get_clock_info("monotonic").implementation,
            "resolution_seconds": time.get_clock_info("monotonic").resolution,
        },
    }
    atomic(path, r)
    with (
        measurement_lock(quiescence) as quiet_digest,
        fixture(binary, CALIBRATION_ROWS) as f,
    ):
        r["external_quiescence_receipt_sha256"] = quiet_digest
        _, b, r0, r1 = f["stocks"]
        prior = f["base"]
        r["base"] = prior
        r["private_stock_bridges"] = f["private_bridges"]
        for i, (label, tx) in enumerate(zip(r["planned_sources"], f["offers"])):
            roster = receiver_roster([r0, r1])
            _, accepted = offer(f, tx)
            check(
                all(v["accepted"] for v in accepted), "calibration payment unavailable"
            )
            ident = dsha(tx)[::-1].hex()
            with contextlib.ExitStack() as job_life:
                if label == "scary":
                    job = until(
                        lambda job_life=job_life, prior=prior, tx=tx: witnessed_job(
                            f, job_life, prior, tx
                        )[0],
                        "calibration Stratum job",
                    )
                    source = f["scary"]
                    synchronize = f["roots"][1]
                else:
                    job = until(
                        lambda prior=prior, tx=tx: full_job(
                            b, prior, tx, "calibration-right"
                        )[0],
                        "calibration stock candidate",
                    )
                    source = f["roots"][1]
                    synchronize = f["roots"][0]
                nonce, commit = committed_job(job, tx)
                t0 = time.monotonic_ns()
                with concurrent.futures.ThreadPoolExecutor(2) as pool:
                    read = pool.submit(
                        timed_capture,
                        t0,
                        source,
                        commit,
                        prior,
                        ident,
                        t0 + 8_000_000_000,
                    )
                    check(
                        job.submit_native(nonce),
                        "ordinary origin rejected calibration solution",
                    )
                    ack = time.monotonic_ns() - t0
                    body, captured = read.result()
            # A single body, one source and one possible winning hash; all four links carry identical bytes.
            due = time.monotonic_ns() + 80_000_000
            with concurrent.futures.ThreadPoolExecutor(4) as pool:
                first = i % 2
                jobs = {
                    (lane, rx): pool.submit(
                        deliver, f["dest"][lane][rx], body, t0, due, 2_000_000_000
                    )
                    for lane in (first, 1 - first)
                    for rx in ((i + lane) % 2, 1 - (i + lane) % 2)
                }
                legs = [
                    [jobs[(lane, rx)].result() for rx in range(2)] for lane in range(2)
                ]
            check(
                all("pong_ns" in end for lane in legs for end in lane),
                "missing a stock receiver calibration fence",
            )
            found = commit["hash"]
            until(
                lambda found=found: all(
                    n.rpc("getbestblockhash") == found for n in (r0, r1)
                ),
                "both stock receivers selected calibration body",
            )
            readbacks = [
                hashlib.sha256(bytes.fromhex(n.rpc("getblock", found, 0))).hexdigest()
                for n in (r0, r1)
            ]
            check(
                readbacks == [commit["body_sha256"]] * 2,
                "stock readback differs from native capture",
            )
            after = receiver_roster([r0, r1])
            check(
                after == roster,
                "receiver roster changed during calibration",
            )
            row = {
                "index": i,
                "source": label,
                "parent": prior,
                "solution": commit,
                "payment_txid": ident,
                "wire_hex": body.hex(),
                "peer_roster": roster,
                "peer_roster_after": after,
                "native_submit_ack_ns": ack,
                "native_capture_ns": captured,
                "legs": legs,
                "receiver_wire_sha256": readbacks,
                "receiver_active_hashes": [found, found],
                "left_minus_right_bounds_ns": [
                    [
                        legs[0][j]["write_begin_ns"] - legs[1][j]["pong_ns"],
                        legs[0][j]["pong_ns"] - legs[1][j]["write_begin_ns"],
                    ]
                    for j in range(2)
                ],
            }
            r["rows"].append(row)
            atomic(path, r)
            # Native synchronization uses the already captured valid body after the measured receiver readbacks.
            result = deliver(synchronize, body, t0, time.monotonic_ns(), 2_000_000_000)
            check("pong_ns" in result, "producer-side native synchronization failed")
            until(
                lambda found=found: all(
                    n.rpc("getbestblockhash") == found for n in f["stocks"]
                ),
                "all producer stocks selected calibration branch",
            )
            until(
                lambda found=found: (
                    response(f["api"] + "/status")["mining"]["tip_hash"] == found
                ),
                "entrant followed calibration branch",
            )
            prior = found
    max_i = max(
        abs(n)
        for row in r["rows"]
        for bounds in row["left_minus_right_bounds_ns"]
        for n in bounds
    )
    max_native = max(row["native_capture_ns"] for row in r["rows"])
    # Round up to whole milliseconds. Future ties are still individually certified against this bound.
    tolerance = math.ceil(max_i / 1e6 + 2) * 1_000_000
    hold = max(200_000_000, math.ceil(2 * max_native / 50_000_000) * 50_000_000)
    r["calibrated"] = {
        "max_ingress_bound_ns": max_i,
        "max_native_capture_ns": max_native,
        "proposed_tolerance_ns": tolerance,
        "proposed_hold_ns": hold,
        "proposed_signed_delay_ns": max(40_000_000, 8 * tolerance),
    }
    r["complete"] = True
    atomic(path, r)
    return r


def continuation_calibration(path, binary, manifest, quiescence=None):
    """Exercise the post-cutoff native branch transfer with one unopposed stock producer."""
    from matched_continuation import native_branch

    result = {
        "kind": "unopposed continuation transport",
        "build": pinned(binary, manifest),
        "planned_receivers": [0, 1],
        "reference_image": IMAGE,
        "rows": [],
        "complete": False,
    }
    check(not path.exists(), "continuation calibration output already exists")
    atomic(path, result)
    with measurement_lock(quiescence) as quiet_digest, fixture(binary, 2) as f:
        result["external_quiescence_receipt_sha256"] = quiet_digest
        receivers = f["stocks"][2:]
        result["base"] = parent = f["base"]
        result["private_stock_bridges"] = f["private_bridges"]
        for chosen, tx in zip(result["planned_receivers"], f["offers"]):
            _, dispositions = offer(f, tx)
            check(
                all(x["accepted"] for x in dispositions),
                "unopposed calibration payment failed",
            )
            before = receiver_roster(receivers)
            n = receivers[chosen]
            height = n.rpc("getblockcount") + 1
            ident = dsha(tx)[::-1].hex()
            until(
                lambda n=n, ident=ident: (
                    n.rpc("getminingcandidate", True)["merkleProof"] == [ident]
                ),
                "continuation payment in chosen stock template",
            )
            hashes = n.rpc("generate", 3)
            row = {
                "plan": {"successor_receiver": chosen},
                "parent": parent,
                "height": height,
            }
            native_branch(f, row)
            row.update(
                generated_hashes=hashes,
                receiver_roster=before,
                receiver_roster_after=receiver_roster(receivers),
                raw_hex=[n.rpc("getblock", h, 0) for h in hashes],
                payment_txid=dsha(tx)[::-1].hex(),
                all_stock_active_hashes=[
                    n.rpc("getbestblockhash") for n in f["stocks"]
                ],
            )
            check(
                row["all_stock_active_hashes"] == [hashes[-1]] * 4
                and row["receiver_roster_after"] == before,
                "unopposed native continuation did not reach all four isolated stocks",
            )
            result["rows"].append(row)
            atomic(path, result)
            parent = hashes[-1]
        result["complete"] = True
        atomic(path, result)
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("receipt", type=Path)
    p.add_argument("--binary", type=Path, required=True)
    p.add_argument("--build-manifest", type=Path, required=True)
    p.add_argument(
        "--continuation-only",
        action="store_true",
        help="exercise both later native continuation origins without competing work",
    )
    p.add_argument(
        "--quiescence-receipt",
        type=Path,
        help="wait for the external workstation load-drain JSON receipt before taking the shared lock",
    )
    a = p.parse_args()
    run = continuation_calibration if a.continuation_only else calibration
    result = run(a.receipt, a.binary.resolve(), a.build_manifest, a.quiescence_receipt)
    print(
        json.dumps(
            result.get("calibrated", {"completed": len(result["rows"])}), indent=2
        )
    )
