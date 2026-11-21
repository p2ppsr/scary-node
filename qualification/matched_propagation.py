"""Method for delay-controlled, two-receiver regtest races; no registered study or samples."""

import argparse
import concurrent.futures
import hashlib
import json
import secrets
import struct
import subprocess
import threading
import time
from pathlib import Path

from cohort_archive import check, contrast
from cohort_estimate import decode_block, dsha, interval, ratio_influences, txid
from prove import read_varint

MAGIC = bytes.fromhex("dab5bffa")
TARGET = 0x7FFFFF << 232


def frame(command, body):
    return (
        MAGIC
        + command.encode().ljust(12, b"\0")
        + struct.pack("<I", len(body))
        + dsha(body)[:4]
        + body
    )


def receive(sock, deadline):
    def exact(n):
        parts = bytearray()
        while len(parts) < n:
            remaining = (deadline - time.monotonic_ns()) / 1e9
            if remaining <= 0:
                raise TimeoutError("native frame deadline")
            sock.settimeout(remaining)
            incoming = sock.recv(n - len(parts))
            if not incoming:
                raise EOFError("native peer disconnected")
            parts.extend(incoming)
        return bytes(parts)

    header = exact(24)
    check(header[:4] == MAGIC, "requires a regtest peer")
    size = int.from_bytes(header[16:20], "little")
    check(size <= 1 << 20, "unexpected frame in two-transaction regtest fixture")
    body = exact(size)
    check(dsha(body)[:4] == header[20:24], "invalid native frame checksum")
    return header[4:16].rstrip(b"\0").decode(), body


def write(sock, data, deadline):
    remaining = (deadline - time.monotonic_ns()) / 1e9
    if remaining <= 0:
        raise TimeoutError("native write deadline")
    sock.settimeout(remaining)
    sock.sendall(data)


def gate_agent(label):
    check(
        label in {"source-0", "source-1", "l0-r0", "l1-r0", "l0-r1", "l1-r1"},
        "unknown isolated gate link",
    )
    return "/scary-regtest-propagation-" + label + "/"


def handshake(sock, label, timeout_seconds=10):
    """Establish a regular regtest peer on a caller-supplied isolated TCP connection."""
    deadline = time.monotonic_ns() + int(1e9 * timeout_seconds)
    agent = gate_agent(label).encode("ascii")
    version = (
        struct.pack("<iQq", 70016, 0, int(time.time()))
        + bytes(52)
        + secrets.token_bytes(8)
        + bytes([len(agent)])
        + agent
        + struct.pack("<i?", 0, True)
    )
    write(sock, frame("version", version), deadline)
    saw_version = False
    while True:
        name, payload = receive(sock, deadline)
        if name == "version":
            check(not saw_version and len(payload) >= 80, "invalid version")
            saw_version = True
            write(sock, frame("verack", b""), deadline)
        elif name == "verack":
            check(saw_version and not payload, "invalid verack")
            return


def receiver_roster(receivers):
    """Read both actual reference peer lists; omit local network addresses from the receipt."""
    check(
        len(receivers) == 2 and receivers[0] is not receivers[1],
        "need two independent stock receivers",
    )

    def inspect(item):
        idx, n = item
        check(
            n.rpc("getblockchaininfo")["chain"] == "regtest",
            "receiver network is not regtest",
        )
        peers = n.rpc("getpeerinfo")
        result = sorted(
            ({"connection": p["id"], "agent": p["subver"]} for p in peers),
            key=lambda x: x["agent"],
        )
        check(
            [p["agent"] for p in result]
            == [gate_agent(f"l{lane}-r{idx}") for lane in range(2)],
            "receiver has an unexpected or missing native gate peer",
        )
        return result

    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        return list(pool.map(inspect, enumerate(receivers)))


def capture(sock, commitment, parent, payment_txid, deadline):
    """Only bytes actually supplied by the designated native publication path qualify."""
    ident = bytes.fromhex(commitment["hash"])[::-1]
    inventory = b"\1" + struct.pack("<I", 2) + ident
    while True:
        cmd, payload = receive(sock, deadline)
        if cmd == "ping" and len(payload) == 8:
            write(sock, frame("pong", payload), deadline)
        elif cmd in ("inv", "headers"):
            # A normal peer may announce a block before serving it by hash.
            # The only inventory requested here is the worker's exact discovery.
            check(bool(payload), "empty native announcement")
            n, pos = read_varint(payload, 0)
            width = 36 if cmd == "inv" else 81
            check(
                n <= (50000 if cmd == "inv" else 2000)
                and len(payload) == pos + n * width,
                "invalid native announcement length",
            )
            items = (payload[j : j + width] for j in range(pos, len(payload), width))
            matched = (
                any(item == inventory[1:] for item in items)
                if cmd == "inv"
                else any(
                    item[-1:] == b"\0" and dsha(item[:80]) == ident for item in items
                )
            )
            if matched:
                write(sock, frame("getdata", inventory), deadline)
        elif cmd == "block" and dsha(payload[:80]) == ident:
            check(
                hashlib.sha256(payload).hexdigest() == commitment["body_sha256"],
                "native body differs from worker commitment",
            )
            _, txs, _ = decode_block(payload, commitment["hash"])
            check(
                len(txs) == 2
                and txid(txs[1]) == payment_txid
                and payload[4:36][::-1].hex() == parent,
                "native work differs from the planned opportunity",
            )
            check(
                payload[72:76] == bytes.fromhex("ffff7f20")
                and int.from_bytes(ident, "little") <= TARGET,
                "invalid regtest work",
            )
            return payload


def deliver(sock, body, origin, due, timeout_ns):
    # Prepare the complete packet before timing its application write. A unique
    # pong bounds arrival of every earlier byte on this particular TCP stream.
    nonce = secrets.token_bytes(8)
    packet = (
        frame("inv", b"\1" + struct.pack("<I", 2) + dsha(body[:80]))
        + frame("block", body)
        + frame("ping", nonce)
    )
    wait = (due - time.monotonic_ns()) / 1e9
    if wait > 0:
        time.sleep(wait)
    start = time.monotonic_ns()
    result = {
        "planned_release_ns": due - origin,
        "write_begin_ns": start - origin,
        "ping_nonce": nonce.hex(),
    }
    deadline = start + timeout_ns
    try:
        write(sock, packet, deadline)
        result["write_end_ns"] = time.monotonic_ns() - origin
        while True:
            cmd, payload = receive(sock, deadline)
            if cmd == "pong" and payload == nonce:
                result.update(
                    pong_ns=time.monotonic_ns() - origin, pong_nonce=payload.hex()
                )
                break
            if cmd == "ping" and len(payload) == 8:
                write(sock, frame("pong", payload), deadline)
    except (OSError, EOFError, ValueError) as exc:
        result["error"] = str(exc)
    return result


def align(row):
    tolerance = row["tolerance_ns"]
    expected = row["offsets_ns"][0] - row["offsets_ns"][1]
    check(
        tolerance >= 0 and all(v >= 0 for v in row["offsets_ns"]),
        "negative timing allowance",
    )
    out = []
    for receiver in range(2):
        legs = [lane.get("legs", [{}, {}])[receiver] for lane in row["lanes"]]
        if any(lane["discovery"] is None for lane in row["lanes"]):
            out.append({"status": "single_or_no_discovery"})
            continue
        if any("pong_ns" not in leg for leg in legs):
            out.append({"status": "missing_native_body_or_fence"})
            continue
        for leg, lane in zip(legs, row["lanes"]):
            check(
                leg["ping_nonce"] == leg["pong_nonce"]
                and len(bytes.fromhex(leg["pong_nonce"])) == 8
                and lane["native_capture_ns"]
                <= leg["write_begin_ns"]
                <= leg["write_end_ns"]
                <= leg["pong_ns"]
                and leg["planned_release_ns"] <= leg["write_begin_ns"],
                "invalid ingress fence",
            )
        left, right = legs
        bounds = [
            left["write_begin_ns"] - right["pong_ns"],
            left["pong_ns"] - right["write_begin_ns"],
        ]
        on_time = all(
            lane["native_capture_ns"] <= row["hold_ns"] for lane in row["lanes"]
        )
        certified = on_time and all(abs(x - expected) <= tolerance for x in bounds)
        out.append(
            {
                "status": "certified" if certified else "timing_not_certified",
                "native_bodies_on_time": on_time,
                "left_minus_right_ingress_bounds_ns": bounds,
                "scheduled_difference_ns": expected,
            }
        )
    return out


def sample_jobs(plan, offer_monotonic_ns, providers):
    """Request each normal job once at the registered point after the common offer.

    Each bounded provider returns (job, reason). The caller keeps successful
    worker sessions alive until native submission finishes.
    """
    check(
        len(providers) == 2
        and plan["job_response_window_ns"] > 0
        and plan["offer_delay_ms"] >= 0,
        "invalid job sampling plan",
    )
    due = offer_monotonic_ns + plan["offer_delay_ms"] * 1_000_000

    def call(provider):
        wait = (due - time.monotonic_ns()) / 1e9
        if wait > 0:
            time.sleep(wait)
        begin = time.monotonic_ns()
        try:
            job, reason = provider()
        except (
            OSError,
            RuntimeError,
            ValueError,
            AssertionError,
            subprocess.SubprocessError,
        ) as exc:
            job, reason = None, str(exc) or type(exc).__name__
        end = time.monotonic_ns()
        if end > due + plan["job_response_window_ns"]:
            job, reason = None, "job response missed the fixed readiness window"
        check(job is not None or reason, "missing readiness disposition")
        return job, {
            "reason": reason,
            "request_ns_after_offer": begin - offer_monotonic_ns,
            "response_ns_after_offer": end - offer_monotonic_ns,
        }

    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        first = plan["first_lane"]
        check(first in (0, 1), "invalid physical dispatch order")
        ordered = [pool.submit(call, providers[lane]) for lane in (first, 1 - first)]
        result = [f.result() for f in ordered]
        return result if first == 0 else result[::-1]


def opportunity(plan, samples, sources, destinations, *, parent, height, payment_txid):
    """Measure one preplanned opportunity on two input and four receiver-native connections.

    jobs carry normal subscribed Stratum/full RPC submissions. sources are the
    two independently handshaken producer sockets. destinations[lane][receiver]
    are four handshaken, dedicated sockets into the two full-state receivers.
    The caller isolates these paths and durably records the plan before sampling readiness.
    """
    jobs = [j for j, _ in samples]
    reasons = [result["reason"] for _, result in samples]
    timing = plan["timing"]
    hold_ns, offsets_ns, tolerance_ns = (
        timing["hold_ns"],
        timing["offsets_ns"],
        timing["tolerance_ns"],
    )
    capture_window_ns, pong_timeout_ns = (
        timing["capture_window_ns"],
        timing["pong_timeout_ns"],
    )
    check(
        plan["arm"] in ("witnessed/full", "full/full")
        and plan["successor_receiver"] in (0, 1),
        "unexpected planned race",
    )
    check(
        all(j is not None or reason for j, reason in zip(jobs, reasons)),
        "missing unready disposition",
    )
    check(
        len(jobs)
        == len(sources)
        == len(destinations)
        == len(reasons)
        == len(offsets_ns)
        == 2
        and all(len(s) == 2 for s in destinations),
        "expected two native lanes to two receivers",
    )
    check(
        len({s.fileno() for s in [*sources, *destinations[0], *destinations[1]]}) == 6,
        "native sockets must be independent",
    )
    check(
        hold_ns >= 0
        and min(offsets_ns) >= 0
        and tolerance_ns >= 0
        and capture_window_ns > hold_ns + max(offsets_ns)
        and pong_timeout_ns > 0,
        "invalid gate timing budget",
    )
    lanes = [
        {
            "ready": j is not None,
            "readiness_reason": sample["reason"],
            "readiness_sample": sample,
            "hashes": 0,
            "discovery": None,
        }
        for j, sample in samples
    ]
    for tick in range(64):
        for i, j in enumerate(jobs):
            if j is None:
                continue
            nonce = plan["nonce"] + 2 * tick + (i ^ int(plan["swap_nonce"]))
            h = j.header(nonce)
            lanes[i]["hashes"] += 1
            if int.from_bytes(dsha(h), "little") <= TARGET:
                # The worker's bytes produce a commitment only; delivery always reads the source peer.
                expected = h + b"\2" + j.coinbase + j.payment
                check(
                    h[4:36][::-1].hex() == parent and txid(j.payment) == payment_txid,
                    "wrong worker job",
                )
                lanes[i]["discovery"] = {
                    "hash": dsha(h)[::-1].hex(),
                    "nonce": nonce,
                    "body_sha256": hashlib.sha256(expected).hexdigest(),
                }
        if any(l["discovery"] for l in lanes) or not any(jobs):
            break
    row = {
        "plan": plan,
        "parent": parent,
        "height": height,
        "payment_txid": payment_txid,
        "hold_ns": hold_ns,
        "offsets_ns": list(offsets_ns),
        "tolerance_ns": tolerance_ns,
        "capture_window_ns": capture_window_ns,
        "pong_timeout_ns": pong_timeout_ns,
        "lanes": lanes,
    }
    ready = threading.Event()
    origin = 0

    def submit(idx):
        ready.wait()
        start = time.monotonic_ns() - origin
        try:
            disp = {"ack": jobs[idx].submit_native(lanes[idx]["discovery"]["nonce"])}
        except (
            OSError,
            RuntimeError,
            ValueError,
            AssertionError,
            subprocess.SubprocessError,
        ) as exc:
            disp = {"error": str(exc)}
        return dict(disp, start_ns=start, finish_ns=time.monotonic_ns() - origin)

    def relay(idx, pool):
        ready.wait()
        lane = lanes[idx]
        try:
            body = capture(
                sources[idx],
                lane["discovery"],
                parent,
                payment_txid,
                origin + capture_window_ns,
            )
        except (OSError, EOFError, ValueError) as exc:
            return {"capture_error": str(exc)}
        captured = time.monotonic_ns() - origin
        due = origin + hold_ns + offsets_ns[idx]
        first = plan["first_receiver"][idx]
        check(first in (0, 1), "invalid receiver dispatch order")
        legs = {
            r: pool.submit(
                deliver, destinations[idx][r], body, origin, due, pong_timeout_ns
            )
            for r in (first, 1 - first)
        }
        return {
            "native_capture_ns": captured,
            "native_wire_hex": body.hex(),
            "legs": [legs[r].result() for r in range(2)],
        }

    with (
        concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool,
        concurrent.futures.ThreadPoolExecutor(max_workers=4) as legs,
    ):
        in_flight = [
            (i, pool.submit(submit, i), pool.submit(relay, i, legs))
            for i in (plan["first_lane"], 1 - plan["first_lane"])
            if lanes[i]["discovery"]
        ]
        origin = time.monotonic_ns()
        ready.set()
        for idx, sent, relayed in in_flight:
            lanes[idx]["native_submit"] = sent.result()
            lanes[idx].update(relayed.result())
    # Hold continuation to the same observation horizon on every scheduled
    # parent, including one where both issuers were unavailable.
    horizon = capture_window_ns + pong_timeout_ns
    remaining = (origin + horizon - time.monotonic_ns()) / 1e9
    if remaining > 0:
        time.sleep(remaining)
    row["discovery_tick_monotonic_ns"] = origin
    row["gate_complete_ns"] = time.monotonic_ns() - origin
    row["ingress_alignment"] = align(row)
    return row


def verify(row):
    timing = row["plan"]["timing"]
    check(
        dict(timing)
        == {
            k: row[k]
            for k in (
                "hold_ns",
                "offsets_ns",
                "tolerance_ns",
                "capture_window_ns",
                "pong_timeout_ns",
            )
        },
        "measured timing differs from the assigned plan",
    )
    check(len(row["lanes"]) == len(timing["offsets_ns"]) == 2, "missing scheduled lane")
    check(
        row["gate_complete_ns"] >= row["capture_window_ns"] + row["pong_timeout_ns"],
        "continuation opened before the common horizon",
    )
    nonces = set()
    attempts = []
    for i, lane in enumerate(row["lanes"]):
        check(
            type(lane["ready"]) is bool
            and (1 <= lane["hashes"] <= 64 if lane["ready"] else lane["hashes"] == 0),
            "lost readiness exposure",
        )
        sample = lane["readiness_sample"]
        scheduled = row["plan"]["offer_delay_ms"] * 1_000_000
        due = scheduled + row["plan"]["job_response_window_ns"]
        check(
            scheduled
            <= sample["request_ns_after_offer"]
            <= sample["response_ns_after_offer"]
            and sample["reason"] == lane["readiness_reason"],
            "missing job request timing",
        )
        check(
            sample["response_ns_after_offer"] <= due
            if lane["ready"]
            else bool(lane["readiness_reason"]),
            "unavailable job counted as ready or missing reason",
        )
        if lane["ready"]:
            attempts.append(lane["hashes"])
        sol = lane["discovery"]
        if sol is None:
            check(
                "native_submit" not in lane and "native_wire_hex" not in lane,
                "publication without a discovery",
            )
            continue
        sub = lane["native_submit"]
        check(
            lane["ready"]
            and ("ack" in sub) != ("error" in sub)
            and 0 <= sub["start_ns"] <= sub["finish_ns"],
            "missing ordinary native publication attempt",
        )
        expected_nonce = (
            row["plan"]["nonce"]
            + 2 * (lane["hashes"] - 1)
            + (i ^ int(row["plan"]["swap_nonce"]))
        )
        check(sol["nonce"] == expected_nonce, "wrong hash lane")
        if "native_wire_hex" not in lane:
            check(
                "capture_error" in lane and "legs" not in lane,
                "missing native capture disposition",
            )
            continue
        raw = bytes.fromhex(lane["native_wire_hex"])
        _, txs, _ = decode_block(raw, sol["hash"])
        check(
            len(txs) == 2
            and txid(txs[1]) == row["payment_txid"]
            and raw[4:36][::-1].hex() == row["parent"]
            and raw[72:76] == bytes.fromhex("ffff7f20")
            and int.from_bytes(dsha(raw[:80]), "little") <= TARGET
            and int.from_bytes(raw[76:80], "little") == expected_nonce
            and hashlib.sha256(raw).hexdigest() == sol["body_sha256"],
            "delivered bytes disagree with the assigned job",
        )
        check(len(lane["legs"]) == 2, "missing measurement receiver")
        for leg in lane["legs"]:
            check(
                leg["planned_release_ns"] == row["hold_ns"] + row["offsets_ns"][i],
                "release schedule changed",
            )
            nonce = bytes.fromhex(leg["ping_nonce"])
            check(len(nonce) == 8 and nonce not in nonces, "reused receiver fence")
            nonces.add(nonce)
            check(
                lane["native_capture_ns"] <= leg["write_begin_ns"]
                and leg["planned_release_ns"] <= leg["write_begin_ns"],
                "release predates bytes or target",
            )
            if "pong_ns" in leg:
                check(
                    leg["pong_nonce"] == leg["ping_nonce"]
                    and leg["write_begin_ns"] <= leg["write_end_ns"] <= leg["pong_ns"],
                    "invalid native pong receipt",
                )
            check(
                ("pong_ns" in leg) != ("error" in leg),
                "missing receive fence disposition",
            )
    check(
        len(set(attempts)) <= 1 and align(row) == row["ingress_alignment"],
        "attempts or alignment disagree",
    )
    if "settlement" in row:
        settlement = row["settlement"]
        check(
            settlement["generation_started_ns"] >= row["gate_complete_ns"],
            "continuation precedes gate closure",
        )
        h = settlement["depth_two_ancestry"]
        tip = settlement["endpoint_tips"]
        check(
            len(h) == len(tip) == 2 and h[0] == h[1] and tip[0] == tip[1],
            "independent receiver settlement disagrees",
        )
        ancestry = settlement["selected_header_hex"]
        check(len(ancestry) == 3, "expected winning header and two successors")
        previous = row["parent"]
        for pos, encoded in enumerate(ancestry):
            header = bytes.fromhex(encoded)
            check(
                len(header) == 80
                and header[4:36][::-1].hex() == previous
                and header[72:76] == bytes.fromhex("ffff7f20")
                and int.from_bytes(dsha(header), "little") <= TARGET,
                "invalid settlement headers",
            )
            previous = dsha(header)[::-1].hex()
            if pos == 0:
                check(h[0] == previous, "wrong winning header")
        check(previous == tip[0], "wrong settlement tip")
    return row["ingress_alignment"]


def settle(row, receivers, connect_after_continuation, timeout_seconds=60):
    """Call with the two isolated stock nodes after both deliveries and the common cutoff."""
    check(
        len(receivers) == 2 and receivers[0] is not receivers[1],
        "two independent receiving nodes required",
    )
    row["receiver_peers_after_gate"] = receiver_roster(receivers)
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        before = list(pool.map(lambda n: n.rpc("getblockchaininfo"), receivers))
    check(
        all(
            b["chain"] == "regtest"
            and b["blocks"] in (row["height"] - 1, row["height"])
            for b in before
        ),
        "unexpected receiver height or network",
    )
    chosen = row["plan"]["successor_receiver"]
    # All arms get the same preselected continuation rule, including a race
    # with no discovery or a native publication that timed out.
    stock = receivers[chosen]
    generation_ns = time.monotonic_ns() - row["discovery_tick_monotonic_ns"]
    row["continuation_attempt"] = {
        "chosen_receiver": chosen,
        "generation_started_ns": generation_ns,
        "precontinuation_tips": [b["bestblockhash"] for b in before],
    }
    generated = stock.rpc("generate", row["height"] + 2 - before[chosen]["blocks"])
    row["continuation_attempt"]["generated"] = generated
    connect_after_continuation()
    deadline = time.monotonic() + timeout_seconds
    while True:
        tips = [n.rpc("getbestblockhash") for n in receivers]
        if tips == [generated[-1], generated[-1]]:
            break
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "both native receivers have not selected the continuation; preserve partial row"
            )
        time.sleep(0.04)
    row["settlement"] = {
        "generation_started_ns": generation_ns,
        "precontinuation_tips": [b["bestblockhash"] for b in before],
        "depth_two_ancestry": [n.rpc("getblockhash", row["height"]) for n in receivers],
        "endpoint_tips": tips,
        "selected_header_hex": [
            stock.rpc("getblockheader", stock.rpc("getblockhash", h), False)
            for h in range(row["height"], row["height"] + 3)
        ],
    }
    verify(row)


def close_session(plan, rows, receivers):
    check(
        len(plan) == len(rows) and len(receivers) == 2, "incomplete registered session"
    )
    for row in rows:
        row["session_final_ancestry"] = [
            n.rpc("getblockhash", row["height"]) for n in receivers
        ]
    return check_session(plan, rows)


def check_session(plan, rows):
    from collections import Counter

    check(len(plan) == len(rows) and len(plan) > 0, "missing scheduled parent")
    totals = {}
    for i, (planned, row) in enumerate(zip(plan, rows)):
        check(
            planned == row["plan"] and planned["index"] == i,
            "scheduled exposure changed",
        )
        if i:
            check(
                row["parent"] == rows[i - 1]["settlement"]["endpoint_tips"][0]
                and row["height"] == rows[i - 1]["height"] + 3,
                "missing parent transition",
            )
        before_peers, after_peers = (
            row["receiver_peers_before_offer"],
            row["receiver_peers_after_gate"],
        )
        check(
            before_peers == after_peers and len(before_peers) == 2,
            "receiver connections changed during the propagation race",
        )
        for idx, ps in enumerate(before_peers):
            check(
                [p["agent"] for p in ps]
                == [gate_agent(f"l{lane}-r{idx}") for lane in range(2)]
                and len({p["connection"] for p in ps}) == 2,
                "unisolated receiver roster",
            )
        verify(row)
        winners = row["settlement"]["depth_two_ancestry"]
        final = row["session_final_ancestry"]
        check(
            len(final) == 2 and final[0] == final[1],
            "final ancestry differs between stores",
        )
        label = f"{planned['arm']}:{row['offsets_ns'][0] - row['offsets_ns'][1]}ns"
        arm = totals.setdefault(
            label,
            {"parents": 0, "alignment": Counter(), "lanes": [Counter(), Counter()]},
        )
        arm["parents"] += 1
        # Two receivers observe the same parent. They are never counted as independent races.
        status = {x["status"] for x in row["ingress_alignment"]}
        arm["alignment"]["+".join(sorted(status))] += 1
        for lane, c in zip(row["lanes"], arm["lanes"]):
            sol = lane["discovery"]
            c.update(scheduled=1, ready=int(lane["ready"]), hashes=lane["hashes"])
            if sol:
                won = sol["hash"] == winners[0]
                c.update(
                    discovered=1,
                    native_submit_errors=int("error" in lane["native_submit"]),
                    captured_after_common_cutoff=int(
                        "native_capture_ns" in lane
                        and lane["native_capture_ns"] > row["hold_ns"]
                    ),
                    native_captured=int("native_wire_hex" in lane),
                    settled=int(won),
                    stale=int(not won),
                    later_detached=int(won and final[0] != sol["hash"]),
                )
    return totals


def paired_delay_effect(closed_sessions, registered_ids, arm, delay_ns):
    """Analyze the fixed future cohort by whole sessions, including all assigned parents."""
    check(
        len(registered_ids) >= 2
        and len(set(registered_ids)) == len(registered_ids)
        and set(registered_ids) == set(closed_sessions),
        "incomplete or unregistered cohort",
    )
    check(
        arm in ("witnessed/full", "full/full") and delay_ns > 0,
        "invalid treatment contrast",
    )
    groups = [f"{arm}:{d}ns" for d in (-delay_ns, 0, delay_ns)]
    counts = []
    for ident in registered_ids:
        session = closed_sessions[ident]
        check(
            all(k in session and session[k]["parents"] > 0 for k in groups),
            "session missing registered treatment",
        )
        counts.append({k: session[k]["lanes"] for k in groups})
    results = {
        k: {
            "conditional_stale": contrast(counts, k, "stale", "discovered"),
            "per_scheduled": {
                v: contrast(counts, k, v, "scheduled")
                for v in ("ready", "discovered", "settled", "stale")
            },
        }
        for k in groups
    }
    minus, _, plus = groups
    if any(
        results[k]["conditional_stale"]["difference"] is None for k in (plus, minus)
    ):
        effect = {
            "difference": None,
            "confidence_interval_95": None,
            "reason": "zero discovery denominator",
        }
    else:
        _, _, i_plus = ratio_influences(counts, plus, "stale", "discovered")
        _, _, i_minus = ratio_influences(counts, minus, "stale", "discovered")
        delta = (
            results[plus]["conditional_stale"]["difference"]
            - results[minus]["conditional_stale"]["difference"]
        )
        effect = {
            "difference": delta,
            **interval(delta, [a - b for a, b in zip(i_plus, i_minus)], bound=2),
        }
    return {
        "sessions": len(counts),
        "by_assigned_delay": results,
        "left_delayed_minus_right_delayed_stale_difference": effect,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Verify a complete delay-controlled regtest session."
    )
    parser.add_argument("receipt", type=Path)
    parser.add_argument(
        "--plan",
        type=Path,
        required=True,
        help="separately retained session plan created before observing readiness",
    )
    args = parser.parse_args()
    ledger = json.loads(args.receipt.read_text())
    plan = json.loads(args.plan.read_text())
    check(plan == ledger["plan"], "ledger differs from the supplied session plan")
    print(json.dumps(check_session(plan, ledger["rows"]), indent=2))
