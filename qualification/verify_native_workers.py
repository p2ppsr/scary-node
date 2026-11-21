"""Replay worker headers, submitted shares, winner attribution and both receiver ancestries offline."""

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import struct
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "regtest"))
from prove import dsha, decode_block

REGTEST_TARGET = 0x7FFFFF << 232


def require(condition, message):
    if not condition:
        raise ValueError(message)


def previous_hash(job):
    raw = bytes.fromhex(job[1])
    require(len(raw) == 32, "invalid Stratum previous hash")
    return b"".join(raw[i : i + 4][::-1] for i in range(0, 32, 4))


def build_candidate(job, x1, x2, payments):
    require(len(job) == 9 and job[6] == "207fffff", "expected ordinary regtest work")
    coinbase = bytes.fromhex(job[2] + x1 + x2 + job[3])
    layer = [dsha(coinbase)] + [dsha(p) for p in payments]
    branches = []
    while len(layer) > 1:
        branches.append(layer[1].hex())
        if len(layer) % 2:
            layer.append(layer[-1])
        layer = [dsha(layer[i] + layer[i + 1]) for i in range(0, len(layer), 2)]
    require(branches == job[4], "job differs from exact ordered payment set")
    prefix = (
        struct.pack("<I", int(job[5], 16))
        + previous_hash(job)
        + layer[0]
        + struct.pack("<II", int(job[7], 16), int(job[6], 16))
    )
    return prefix, bytes([len(payments) + 1]) + coinbase + b"".join(payments)


def verify_settlement(r, winning_job, index):
    raw = bytes.fromhex(r["winning_wire"])
    decode_block(raw, r["winner"])
    local = r["local"]
    require(
        local["chain_state"] == "settled"
        and local["parent"] == r["parent"]
        and local["hash"] == r["winner"]
        and local["job"] == winning_job
        and local["height"] == r["height"],
        "local settlement attribution",
    )
    require(
        r["accepted_shares"] == r["solved_blocks"] == index + 1,
        "one accounted network share per parent",
    )
    require(
        {p["name"] for p in r["peers"]} == {"sv-1", "sv-2"},
        "distinct receiver observations",
    )
    for p in r["peers"]:
        require(
            p["winning_wire_sha256"] == hashlib.sha256(raw).hexdigest()
            and p["active_at_height"] == r["winner"],
            "peer block identity",
        )
        canon = r["winner"]
        require(len(p["successor_headers"]) == 2, "incomplete depth-two evidence")
        for h in p["successor_headers"]:
            hdr = bytes.fromhex(h)
            require(
                len(hdr) == 80
                and hdr[4:36][::-1].hex() == canon
                and hdr[72:76] == bytes.fromhex("ffff7f20")
                and int.from_bytes(dsha(hdr), "little") <= REGTEST_TARGET,
                "successor chainwork",
            )
            canon = dsha(hdr)[::-1].hex()
        require(canon == p["tip"], "receiver tip")

    require(
        r["peers"][0]["tip"] == r["peers"][1]["tip"],
        "receiver settlement disagreement",
    )


def verify(record):
    workers, rounds = record["worker_count"], record["round_count"]
    require(
        len(record["rounds"]) == rounds and rounds >= 3 and 2 <= workers <= 32,
        "session dimensions",
    )
    require(
        record["credential_required"]
        and len(bytes.fromhex(record["binary_sha256"])) == 32,
        "authenticated release identity",
    )
    seen_x1, seen_hashes = set(), set()
    summary = {
        "workers_per_parent": workers,
        "parents": rounds,
        "authorized": 0,
        "low_shares_rejected": 0,
        "network_solutions_submitted": 0,
        "accepted": 0,
        "stale_reply": 0,
        "session_closed": 0,
        "first_job_wins": 0,
        "refreshed_job_wins": 0,
        "independent_depth_two_readbacks": 0,
    }
    preceding_tip = None
    for index, r in enumerate(record["rounds"]):
        require(
            r["index"] == index
            and len(r["workers"]) == workers
            and len(r["peers"]) == 2,
            "missing worker or peer evidence",
        )
        require(not preceding_tip or preceding_tip == r["parent"], "parent continuity")
        payments = [bytes.fromhex(tx) for tx in r["payments"]]
        require(
            len(payments) == 2, "expected two independently signed ordinary payments"
        )
        winner = None
        names = set()
        for lane, w in enumerate(r["workers"]):
            old, fresh = w["first_job"], w["refreshed_job"]
            require(w["name"] not in names and w["authorized"], "worker authorization")
            names.add(w["name"])
            x1, x2 = w["extranonce1"], w["extranonce2"]
            require(
                x1 not in seen_x1
                and len(bytes.fromhex(x1)) == 4
                and len(bytes.fromhex(x2)) == w["extranonce2_bytes"] == 4,
                "extranonce allocation",
            )
            seen_x1.add(x1)
            require(
                w["share_difficulty"] == 65535 / 8388607 / 16777216,
                "regtest share difficulty",
            )
            require(
                previous_hash(old)[::-1].hex() == r["parent"]
                and old[1] == fresh[1]
                and old[8] is True
                and fresh[8] is False
                and old[0] != fresh[0],
                "same-parent job refresh",
            )
            first = build_candidate(old, x1, x2, payments[:1])
            latest = build_candidate(fresh, x1, x2, payments)
            low = w["low_share"]
            low_hash = dsha(first[0] + struct.pack("<I", low["nonce"]))
            require(
                int.from_bytes(low_hash, "little") > REGTEST_TARGET
                and low["outcome"]["error"][0] == 23,
                "low difficulty share",
            )
            summary["authorized"] += 1
            summary["low_shares_rejected"] += 1
            expected = (
                "first_job"
                if index % 3 == 0 or (index % 3 == 2 and lane % 2 == 0)
                else "refreshed_job"
            )
            require(w["job_used"] == expected, "preassigned job revision")
            prefix, body = first if expected == "first_job" else latest
            header = prefix + struct.pack("<I", w["nonce"])
            digest = dsha(header)
            require(
                digest[::-1].hex() == w["hash"]
                and w["hash"] not in seen_hashes
                and int.from_bytes(digest, "little") <= REGTEST_TARGET,
                "worker-specific solved header",
            )
            seen_hashes.add(w["hash"])
            require(0 <= w["start_ns"] <= w["finish_ns"], "worker submission time")
            summary["network_solutions_submitted"] += 1
            out = w["outcome"]
            if out.get("result") is True:
                require(
                    winner is None and out["error"] is None, "ambiguous local winner"
                )
                winner = w
                require(
                    (header + body).hex() == r["winning_wire"]
                    and w["hash"] == r["winner"],
                    "worker-to-block attribution",
                )
                summary[
                    (
                        "first_job_wins"
                        if expected == "first_job"
                        else "refreshed_job_wins"
                    )
                ] += 1
                summary["accepted"] += 1
            elif out.get("closed") is True:
                summary["session_closed"] += 1
            else:
                require(
                    out["result"] is None and out["error"][0] == 21,
                    "unexpected stale submission outcome",
                )
                summary["stale_reply"] += 1
        require(winner is not None, "missing accepted worker")
        verify_settlement(r, winner[winner["job_used"]][0], index)
        summary["independent_depth_two_readbacks"] += len(r["peers"])
        preceding_tip = r["peers"][0]["tip"]
    return summary


def verify_retained(r):
    initial = r["worker"]
    original = initial["first_job"]
    x2 = r["extranonce2"]
    payments = [bytes.fromhex(p) for p in r["payments"]]
    require(
        9 <= len(r["revisions"]) == len(payments) - 1 <= 100,
        "retention must exercise more than eight replacements",
    )
    require(
        initial["authorized"] and previous_hash(original)[::-1].hex() == r["parent"],
        "retained worker and parent",
    )
    distinct = {original[0]}
    tx_counts = {original[0]: 1}
    for i, attempt in enumerate(r["revisions"], 1):
        observed = attempt["reader"]
        job = observed["first_job"]
        require(
            observed["authorized"]
            and attempt["index"] == i
            and job[1] == original[1]
            and job[0] not in distinct
            and job[8] is True,
            "fresh revision reader",
        )
        build_candidate(job, observed["extranonce1"], x2, payments[: i + 1])
        require(
            attempt["original_share"]["error"][0] == 23, "stale or missing original job"
        )
        distinct.add(job[0])
        tx_counts[job[0]] = i + 1
    notices = r["original_notifications"]
    require(
        len(notices) >= 2 and notices[0] == original,
        "retained worker never observed a refresh",
    )
    for j in notices[1:]:
        require(
            j[8] is False and j[1] == original[1] and j[0] in distinct,
            "unexpected retained worker revision",
        )
        build_candidate(j, initial["extranonce1"], x2, payments[: tx_counts[j[0]]])
    prefix, body = build_candidate(original, initial["extranonce1"], x2, payments[:1])
    low = dsha(prefix + struct.pack("<I", r["low_nonce"]))
    header = prefix + struct.pack("<I", r["nonce"])
    digest = dsha(header)
    require(
        int.from_bytes(low, "little") > REGTEST_TARGET
        and int.from_bytes(digest, "little") <= REGTEST_TARGET,
        "retained worker probe or solution difficulty",
    )
    require(
        r["ack"]["result"] is True
        and r["ack"]["error"] is None
        and (header + body).hex() == r["winning_wire"]
        and digest[::-1].hex() == r["winner"],
        "oldest job differs from settled wire",
    )
    verify_settlement(r, original[0], 0)
    return {
        "same_parent_replacements": len(r["revisions"]),
        "original_worker_notifications": len(notices),
        "original_share_probes": len(r["revisions"]),
        "settled_hash": r["winner"],
        "peer_confirmations": len(r["peers"]),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path)
    args = parser.parse_args()
    with gzip.open(args.receipt, "rt") as f:
        record = json.load(f)
    result = (
        verify_retained(record)
        if record.get("kind") == "retained_job"
        else verify(record)
    )
    print(json.dumps(result, indent=2))
