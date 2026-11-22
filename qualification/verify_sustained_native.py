"""Recompute the registered duration, primary opportunities and stock readbacks, optionally from raw wires."""

import argparse
from array import array
from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path
import struct
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "regtest"))
from prove import decode_block, dsha
from verify_receipt import check_header, require
from sustained_resources import DOCKER_MIN, HOST_MIN, SERVICES
from sustained_profiles import (
    FROZEN_RELEASE,
    REORDERED_ARMS,
    pinned_build,
    ENDORSED_HASH,
    ENDORSED_SHA,
    ENDORSEMENT,
    PROJECTS,
    REGISTRATIONS,
    WITNESSED_ARMS,
    REORDERED_IDS,
    batch_order,
    cohort_by_batch,
)

BATCH_SIZE = 512
BATCHES = 10_800
PAYMENTS = BATCH_SIZE * BATCHES
SLOTS = 300
COHORTS = 6
NS = 10**9
REFERENCE_RELEASE = FROZEN_RELEASE
MAGIC = bytes.fromhex("dab5bffa")
TX_COMMAND = b"tx".ljust(12, b"\x00")
REGISTRATION = "11e60074fbdde9587e1652d4de272a24bd4ac68b"
REPLICATION = "3ec961ef81881b83d4cc1ecc646250a2d8d200f9"
ORDERED_SOURCE = "243170edb3df9bea289c32207732d988f6e4b92f67b17634a72e709ae8b735a2"
FUNDING = "3fb3a456448fd6d91801a35c04112ffab3f30dc0a6a14e426effff4f91660ba9"
PROJECT = "scary-node-sustained-production"
COUNTERS = (
    "requests",
    "served",
    "unknown_requests",
    "unique_requested",
    "unique_served",
    "served_wire_bytes",
)


def sha256_hex(text):
    return (
        isinstance(text, str) and len(text) == 64 and bytes.fromhex(text).hex() == text
    )


def resources(rows):
    require(
        rows and all(a["elapsed_ns"] < b["elapsed_ns"] for a, b in zip(rows, rows[1:])),
        "resource timestamps are incomplete or out of order",
    )
    host_free, docker_free, counters = [], [], {}
    for row in rows:
        if "sampler_error" in row:
            continue
        host_free.append(row["host_filesystem_free_bytes"])
        if row["docker_filesystem_free_bytes"] is not None:
            docker_free.append(row["docker_filesystem_free_bytes"])
        for service, sample in row["services"].items():
            require(service in SERVICES, "unregistered resource service")
            if "error" in sample:
                continue
            require(
                sample["read_ns"] <= row["elapsed_ns"] + NS,
                "resource read occurs outside sampled interval",
            )
            stat = counters.setdefault(
                service, {"first": sample, "last": sample, "peak": 0, "count": 0}
            )
            if sample["read_ns"] > stat["last"]["read_ns"] or not stat["count"]:
                require(
                    sample["cpu_usage_usec"] >= stat["last"]["cpu_usage_usec"],
                    "container CPU counter reset",
                )
                stat.update(
                    last=sample,
                    peak=max(stat["peak"], sample["memory_bytes"]),
                    count=stat["count"] + 1,
                )
        if row["host_filesystem_free_bytes"] < HOST_MIN or (
            row["docker_filesystem_free_bytes"] is not None
            and row["docker_filesystem_free_bytes"] < DOCKER_MIN
        ):
            require(
                row["stop_reason"], "registered reserve crossed without recorded stop"
            )
    return {
        "samples": len(rows),
        "lowest_host_free_bytes": min(host_free, default=None),
        "lowest_docker_free_bytes": min(docker_free, default=None),
        "services": {
            service: {
                "distinct_samples": v["count"],
                "peak_sampled_memory_bytes": v["peak"],
                "cpu_seconds_across_observed_interval": (
                    v["last"]["cpu_usage_usec"] - v["first"]["cpu_usage_usec"]
                )
                / 1e6,
            }
            for service, v in counters.items()
        },
    }


def distribution(values):
    if not values:
        return {"count": 0}
    ordered = sorted(values)
    return {
        "count": len(values),
        "minimum": ordered[0],
        "median": (ordered[(len(values) - 1) // 2] + ordered[len(values) // 2]) / 2,
        "p95_nearest_rank": ordered[(95 * len(values) + 99) // 100 - 1],
        "maximum": ordered[-1],
    }


def intake_delta(samples):
    endpoints = [
        s["scary"]["peer_intake"]
        for s in samples
        if "peer_intake" in s.get("scary", {})
    ]
    if len(endpoints) < 2:
        return {}
    return {
        route: {
            field: last - endpoints[0][route][field] for field, last in final.items()
        }
        for route, final in endpoints[-1].items()
    }


def inventory_evidence(progress, final):
    last = dict.fromkeys(COUNTERS, 0)
    timestamp = None
    for r in progress + ([final] if final else []):
        if "elapsed_ns" in r:
            require(
                timestamp is None or timestamp <= r["elapsed_ns"],
                "inventory clock moved backwards",
            )
            timestamp = r["elapsed_ns"]
        require(
            all(type(r[k]) is int and r[k] >= last[k] for k in COUNTERS),
            "inventory counter regressed",
        )
        require(
            0
            <= r["unique_served"]
            <= r["unique_requested"]
            <= min(PAYMENTS, r["requests"])
            and r["unique_served"] <= r["served"] <= r["requests"],
            "inconsistent inventory counts",
        )
        last = r
    if final:
        require(
            all(sha256_hex(final[k + "_bits_sha256"]) for k in ("requested", "served")),
            "missing inventory membership commitment",
        )
    return {
        "progress_samples": len(progress),
        "durable_final": bool(final),
        **{k: last[k] for k in COUNTERS},
    }


def inventory_membership(directory, final, confirmed):
    memberships = {}
    for name in ("requested", "served"):
        bits = (directory / (name + ".bits")).read_bytes()
        require(
            len(bits) == len(confirmed) and bits.count(0) + bits.count(1) == len(bits),
            "invalid local wallet membership bitmap",
        )
        require(
            hashlib.sha256(bits).hexdigest() == final[name + "_bits_sha256"]
            and bits.count(1) == final["unique_" + name],
            "local wallet membership differs from final digest or count",
        )
        memberships[name] = bits
    require(
        all(
            s <= r
            for s, r in zip(
                memberships["served"], memberships["requested"], strict=True
            )
        ),
        "served transaction was never requested",
    )
    return {
        "confirmed_and_wallet_served": sum(
            bool(c and s) for c, s in zip(confirmed, memberships["served"], strict=True)
        ),
        "confirmed_without_recorded_wallet_serve": sum(
            bool(c and not s)
            for c, s in zip(confirmed, memberships["served"], strict=True)
        ),
    }


def stock_activity(rows, start_wall):
    fields = (
        ("getmempoolinfo", "size"),
        ("getmempoolinfo", "bytes"),
        ("getblockchainactivity", "transactions"),
        ("getblockchainactivity", "blocks"),
        ("getorphaninfo", "size"),
    )
    for r in rows:
        require(
            r["wall_ns"] <= r["finished_wall_ns"]
            and r["wall_ns"] - start_wall == r["elapsed_ns"],
            "stock gauge timestamps differ",
        )
    summary = {}
    for call, key in fields:
        values = [
            (r["elapsed_ns"], r[call][key])
            for r in rows
            if isinstance(r.get(call, {}).get(key), int)
        ]
        if values:
            summary[call + "." + key] = {
                "count": len(values),
                "first": values[0][1],
                "last": values[-1][1],
                "maximum": max(v for t, v in values),
                "first_elapsed_s": values[0][0] / NS,
                "last_elapsed_s": values[-1][0] / NS,
            }
    return summary


def replication_setup(record):
    setup = record["setup"]
    arm = record.get("arm", "production")
    project, _ = PROJECTS[arm]
    require(
        setup["registration"] == REGISTRATIONS[arm]
        and setup["project"] == project
        and setup["source_ids_sha256"]
        == record["source"]["offered_id_sha256"]
        == ORDERED_SOURCE
        and setup["executable_sha256"] == record["release_sha256"],
        "replication identity differs",
    )
    require(
        setup["funding"]["height"] == 484 and setup["funding"]["hash"] == FUNDING,
        "replication funding differs",
    )
    if arm in WITNESSED_ARMS:
        expected_ids = REORDERED_IDS if arm in REORDERED_ARMS else ORDERED_SOURCE
        require(
            setup["arm"] == arm and setup["scheduled_order_id_sha256"] == expected_ids,
            "offered schedule differs",
        )
    if arm == "endorsed":
        require(
            bytes.fromhex(setup["endorsement_hex"]) == ENDORSEMENT,
            "endorsing wire differs",
        )
        check_header(ENDORSEMENT[:80], ENDORSED_HASH, FUNDING)
        _, txs, _ = decode_block(ENDORSEMENT, ENDORSED_HASH)
        require(
            len(txs) == 1
            and record["baseline"]
            == {"height": 485, "hash": ENDORSED_HASH, "wire_sha256": ENDORSED_SHA},
            "endorsed starting state differs",
        )
    else:
        require(setup["funding"] == record["baseline"], "cold starting state differs")
    flags = {
        "-checkmempool": "0",
        "-checkblockindex": "0",
        "-excessiveblocksize": "4000000000",
    }
    for key, value in flags.items():
        require(
            [a for a in setup["stock_arguments"] if a.split("=")[0] == key]
            == [key + "=" + value],
            "stock parameters differ from registration",
        )
    data_volumes = set()
    targets = {
        "as1": "/opt/aerospike/data",
        "as2": "/opt/aerospike/data",
        "tn1": "/app/data",
        "tn2": "/app/data",
        "svref": "/data",
        "scary-node": "/data",
    }
    for role, target in targets.items():
        mounts = [m for m in setup["topology"][role]["mounts"] if m["target"] == target]
        require(
            len(mounts) == 1
            and mounts[0]["type"] == "volume"
            and mounts[0]["read_write"]
            and mounts[0]["volume"].startswith(project + "_"),
            "unexpected replication data mount",
        )
        data_volumes.add(mounts[0]["volume"])
    require(len(data_volumes) == len(targets), "two roles shared a data volume")
    names = [e["event"] for e in setup["events"]]
    require(
        names[0] == "fresh_project_verified" and names[-1] == "four_stores_ready",
        "missing fresh project or independent funding observations",
    )


def verify_branch_history(record):
    event_chain = []
    detached_events = []
    known = {b["hash"]: b for b in record["blocks"] + record["detached_observations"]}
    for event in record["branch_events"]:
        if "detach" in event:
            require(
                event_chain and event_chain[-1] == (event["height"], event["detach"]),
                "branch event withdraws a different block",
            )
            detached_events.append(event_chain.pop())
        else:
            body = known[event["attach"]]
            expected_height, parent = (
                event_chain[-1]
                if event_chain
                else (record["baseline"]["height"], record["baseline"]["hash"])
            )
            require(
                body["height"] == event["height"] == expected_height + 1,
                "branch observation skips a height",
            )
            check_header(bytes.fromhex(body["header_hex"]), body["hash"], parent)
            event_chain.append((body["height"], body["hash"]))
    require(
        event_chain == [(b["height"], b["hash"]) for b in record["blocks"]]
        and detached_events
        == [(b["height"], b["hash"]) for b in record["detached_observations"]],
        "final branch or detached observations differ",
    )
    return len(detached_events)


def verify(record, executable=None):
    arm = record.get(
        "arm", "production" if record["registration"] == REPLICATION else "direct"
    )
    require(
        arm in REGISTRATIONS and REGISTRATIONS[arm] == record["registration"],
        "unregistered arm",
    )
    _, pinned = pinned_build(arm)
    require(
        executable is None or executable == pinned, "binary differs from registration"
    )
    require(record["schema"] == 1, "unsupported sustained receipt")
    require(record["network"] == "regtest", "unexpected trial network")
    require(
        record["release_sha256"] == pinned, "release differs from the build receipt"
    )
    replication = arm != "direct"
    inventory = None
    if replication:
        require(
            record["source_protocol"] == "inv-getdata-tx",
            "replication did not follow requested native transaction delivery",
        )
        replication_setup(record)
        inventory = inventory_evidence(
            record["inventory_progress"], record["inventory_final"]
        )
    source = record["source"]
    require(
        (source["scheduled"], source["batch_size"], source["batches"])
        == (PAYMENTS, BATCH_SIZE, BATCHES),
        "prespecified offer cohort differs",
    )
    require(
        sha256_hex(source["offered_id_sha256"])
        and sha256_hex(record["release_sha256"]),
        "missing source or executable identity",
    )
    done = record["completion"]
    elapsed = record["duration_ns"]
    require(elapsed > 0, "missing measurement interval")
    offers = [r for r in record["producer"] if r["phase"] == "offer"]
    ids = set()
    order = batch_order(arm)
    for r in record["producer"]:
        require(
            r["phase"] in ("offer", "rebroadcast")
            and 0 <= r["batch"] < BATCHES
            and r["started_ns"] <= r["finished_ns"]
            and 0 <= r["count"] <= BATCH_SIZE,
            "invalid source delivery",
        )
        if r["phase"] == "offer":
            require(
                r["count"] == BATCH_SIZE
                and r["batch"] not in ids
                and r.get("position", r["batch"]) == len(ids)
                and r["batch"] == order[len(ids)]
                and r["scheduled_ns"] == len(ids) * NS // 6
                and r["sender"] == r["batch"] % 4,
                "changed or repeated scheduled batch",
            )
            ids.add(r["batch"])
        elif replication:
            require(
                r["sender"] == r["batch"] % 4 and r["scheduled_ns"] >= 1818 * NS,
                "rebroadcast departed from registered drain and lane",
            )
    slots = {}
    counts = {"scary": Counter(), "sv": Counter()}
    for r in record["slots"]:
        require(
            r["phase"] in ("offer", "drain", "settle")
            and r["started_ns"] <= r["finished_ns"],
            "invalid miner opportunity",
        )
        if r["phase"] == "offer":
            i = r["index"]
            miner = "sv" if i % 4 == 3 else "scary"
            require(
                0 <= i < SLOTS
                and i not in slots
                and r["miner"] == miner
                and r["scheduled_ns"] == (i + 1) * 6 * NS,
                "opportunity missing or registered miner differs",
            )
            slots[i] = r
            counts[miner][r["outcome"]] += 1
    height, parent = record["baseline"]["height"], record["baseline"]["hash"]
    totals = Counter()
    cohorts = [0] * COHORTS
    last_payment = height
    previous_read = 0
    block_map = {}
    for b in record["blocks"]:
        height += 1
        require(b["height"] == height, "block height skipped")
        check_header(bytes.fromhex(b["header_hex"]), b["hash"], parent)
        parent = b["hash"]
        require(parent not in block_map, "repeated active block")
        block_map[parent] = b
        require(previous_read <= b["readback_ns"], "body readback moved backwards")
        previous_read = b["readback_ns"]
        require(
            b["wire_bytes"] > 80 and sha256_hex(b["wire_sha256"]),
            "missing wire identity",
        )
        require(
            b["independent_wire_sha256"]
            == dict.fromkeys(("sv", "tn1", "tn2"), b["wire_sha256"]),
            "stock wire digests differ",
        )
        require(b["entrant_hash"] == parent, "entrant selected a different block")
        for field in (
            "ordinary_transactions",
            "scheduled_payments",
            "foreign_payments",
            "duplicate_scheduled",
            "entrant_script_checks",
        ):
            require(
                isinstance(b[field], int) and b[field] >= 0, "invalid block accounting"
            )
            totals[field] += b[field]
        require(
            len(b["confirmed_by_source_cohort"]) == COHORTS
            and all(
                isinstance(x, int) and x >= 0 for x in b["confirmed_by_source_cohort"]
            ),
            "invalid source interval counts",
        )
        cohorts = [
            x + y for x, y in zip(cohorts, b["confirmed_by_source_cohort"], strict=True)
        ]
        require(
            sum(b["confirmed_by_source_cohort"]) == b["scheduled_payments"]
            and b["scheduled_payments"]
            + b["duplicate_scheduled"]
            + b["foreign_payments"]
            == b["ordinary_transactions"],
            "block transaction balance differs",
        )
        if b["scheduled_payments"]:
            last_payment = height
    require(
        sum(cohorts) == done["unique_confirmed"]
        and all(n <= PAYMENTS // COHORTS for n in cohorts),
        "distinct payment interval counts differ",
    )
    require(
        done["unique_confirmed"] + done["missing"] == PAYMENTS
        and done["duplicate_confirmations"] == totals["duplicate_scheduled"],
        "payment balance differs",
    )
    require(
        done["last_payment_height"] == last_payment
        and done["final_height"] == height
        and done["final_hash"] == parent,
        "final active branch differs",
    )
    require(
        done["all_accounted"] == (done["unique_confirmed"] == PAYMENTS),
        "completion flag differs from distinct membership",
    )
    if done["primary_complete"]:
        require(
            len(offers) == BATCHES and len(slots) == SLOTS and elapsed >= 1800 * NS,
            "completed primary interval is missing opportunities",
        )
    settled = done["unique_confirmed"] > 0 and height >= last_payment + 2
    require(
        done["two_successors"] == (settled and done["all_accounted"]),
        "final payment depth differs",
    )
    # A late result collected during shutdown still extends the outcome denominator.
    elapsed = max(elapsed, previous_read)
    rate = done["unique_confirmed"] * NS / elapsed
    resource = resources(record["resources"]) if record["resources"] else None
    sampled = record["samples"]
    require(
        all(a["elapsed_ns"] <= b["elapsed_ns"] for a, b in zip(sampled, sampled[1:])),
        "stock observations are out of order",
    )
    reads = {
        name: sum(
            isinstance(s.get(name), dict) and s[name].get("hash") is not None
            for s in sampled
        )
        for name in ("sv", "tn1", "tn2", "scary")
    }
    source_outcomes = Counter(r["outcome"] for r in offers)
    slot_adoption = {name: Counter() for name in counts}
    for r in slots.values():
        if r["outcome"] == "solved":
            h = r["hash"]
            if "solution_header" in r:
                check_header(bytes.fromhex(r["solution_header"]), h, r["parent"])
            slot_adoption[r["miner"]][
                "selected" if h in block_map else "outside_audited_prefix"
            ] += 1
    trials = record["blocks"]
    earliest = {
        role: distribution(
            [
                (b["first_observed_ns"][role] - b["first_observed_ns"]["sv"]) / 1e6
                for b in trials
                if b["first_observed_ns"].get("sv") is not None
                and b["first_observed_ns"].get(role) is not None
            ]
        )
        for role in ("tn1", "tn2", "scary")
    }
    retry = [r for r in record["producer"] if r["phase"] == "rebroadcast"]
    signed_by_miner = {
        name: sum(
            block_map[r["hash"]]["scheduled_payments"]
            for r in slots.values()
            if r["miner"] == name and r.get("hash") in block_map
        )
        for name in counts
    }
    coverage_observed = all(b["entrant_hash"] == b["hash"] for b in record["blocks"])
    unready = sum(s.get("scary", {}).get("ready") is False for s in sampled)
    origin = {}
    if arm in WITNESSED_ARMS:
        detach_count = verify_branch_history(record)
        intervals = [{"selected_nonempty": 0, "payments": 0} for _ in range(6)]
        for i, slot in slots.items():
            body = block_map.get(slot.get("hash"))
            if body and slot["miner"] == "scary" and body["scheduled_payments"]:
                require(
                    "solution_header" in slot
                    and slot["solution_header"] == body["header_hex"],
                    "entrant body differs from worker solution",
                )
                intervals[i // 50]["selected_nonempty"] += 1
                intervals[i // 50]["payments"] += body["scheduled_payments"]
        regular = all(x["selected_nonempty"] for x in intervals)
        origin = {
            "arm": arm,
            "previously_audited_detached_blocks": detach_count,
            "entrant_primary_by_five_minute_slot_interval": intervals,
            "regular_entrant_origin": regular,
            "entrant_primary_payments_per_total_second": signed_by_miner["scary"]
            * NS
            / elapsed,
        }
    if origin:
        origin["witnessed_miner_target_met"] = bool(
            regular
            and done["primary_complete"]
            and done["all_accounted"]
            and settled
            and totals["entrant_script_checks"] == PAYMENTS
            and not totals["foreign_payments"]
            and not totals["duplicate_scheduled"]
            and not done["resource_stop"]
            and not done["audit_failure"]
            and rate >= 3000
            and len(record["inventory_final"] or {})
            and inventory["unique_served"] == PAYMENTS
        )
    return {
        **origin,
        "registered_payments": PAYMENTS,
        "observed_offer_batches": len(offers),
        "registration": record["registration"],
        "wallet_delivery_protocol": record.get("source_protocol", "direct-tx"),
        "requested_inventory": inventory,
        "wallet_recorded_every_requested_transfer": (
            inventory["durable_final"] and inventory["unique_served"] == PAYMENTS
            if replication
            else None
        ),
        "stock_gauges": stock_activity(
            record.get("stock", []), record.get("start_wall_ns", 0)
        ),
        "source_batch_outcomes": dict(source_outcomes),
        "source_batches_by_lane": dict(Counter(r["sender"] for r in offers)),
        "offer_start_lag_ms": distribution(
            [(r["started_ns"] - r["scheduled_ns"]) / 1e6 for r in offers]
        ),
        "rebroadcast_payments_sent": sum(
            r["count"] for r in retry if r["outcome"] == "sent"
        ),
        "wallet_feedback": record.get("wallets", []),
        "sampled_peer_intake_change": intake_delta(sampled),
        "source_payments_in_selected_primary_blocks_by_miner": signed_by_miner,
        "first_observed_relative_to_sv_ms": earliest,
        "foreign_payments": totals["foreign_payments"],
        "duplicate_scheduled": totals["duplicate_scheduled"],
        "distinct_confirmed": done["unique_confirmed"],
        "missing": done["missing"],
        "confirmed_per_five_minute_offer_cohort": cohorts,
        "scheduled_miner_opportunities": {"scary": 225, "sv": 75},
        "recorded_primary_miner_outcomes": {k: dict(v) for k, v in counts.items()},
        "solved_primary_readback_by_miner": {
            k: dict(v) for k, v in slot_adoption.items()
        },
        "blocks_with_three_matching_stock_reads": len(record["blocks"]),
        "last_payment_settled_depth_two": settled,
        "observed_validation_checks": totals["entrant_script_checks"],
        "seconds_through_end": elapsed / NS,
        "confirmed_spends_per_second": rate,
        "adoption_target_met": bool(
            done["all_accounted"]
            and done["primary_complete"]
            and settled
            and not done["resource_stop"]
            and not done["audit_failure"]
            and coverage_observed
            and resource is not None
            and set(resource["services"]) == set(SERVICES)
            and not totals["duplicate_scheduled"]
            and rate >= 3000
        ),
        "sampled_heights_per_store": reads,
        "entrant_unready_samples": unready,
        "entrant_work_unready_samples": sum(
            s.get("scary", {}).get("work_ready") is False for s in sampled
        ),
        "audit_failure": done["audit_failure"],
        "resource_stop": done["resource_stop"],
        "resource_observations": resource,
    }


def read_source(corpus, source, *, funding=None):
    manifest = json.loads((corpus / "manifest.json").read_text())
    require(manifest == source, "source manifest differs from observed run")
    offsets = json.loads((corpus / "offsets.json").read_text())
    require(
        len(offsets) == BATCHES + 1 and offsets[0] == 0, "incomplete source offsets"
    )
    require(
        offsets[-1]
        == source["native_wire_bytes"]
        == (corpus / "frames.bin").stat().st_size,
        "native wire length differs",
    )
    ordinals = {}
    ordered = hashlib.sha256()
    with (
        (corpus / "frames.bin").open("rb") as wire,
        (corpus / "ids.bin").open("rb") as ids,
    ):
        for start, end in zip(offsets, offsets[1:]):
            require(
                wire.tell() == start and end > start, "invalid source batch boundary"
            )
            reference = ids.read(BATCH_SIZE * 32)
            require(len(reference) == BATCH_SIZE * 32, "incomplete source ID vector")
            ordered.update(reference)
            for i in range(BATCH_SIZE):
                header = wire.read(24)
                require(
                    len(header) == 24
                    and header[:4] == MAGIC
                    and header[4:16] == TX_COMMAND,
                    "invalid native transaction envelope",
                )
                size = struct.unpack_from("<I", header, 16)[0]
                require(
                    0 < size <= end - wire.tell(),
                    "transaction crosses source batch boundary",
                )
                raw = wire.read(size)
                digest = dsha(raw)
                require(
                    digest[:4] == header[20:]
                    and digest == reference[32 * i : 32 * (i + 1)],
                    "transaction differs from its envelope or ordered ID",
                )
                require(digest not in ordinals, "duplicate source transaction")
                ordinal = len(ordinals)
                if funding is not None:
                    require(len(raw) > 41 and raw[4] == 1, "expected one payment input")
                    expected_vout = 0
                    if ordinal < 65_536:
                        require(
                            raw[5:37] == funding[ordinal // 4],
                            "payment does not spend signed funding split",
                        )
                        expected_vout = ordinal % 4
                    else:
                        require(
                            ordinals.get(raw[5:37]) == ordinal - 65_536,
                            "spend does not extend its registered payment rail",
                        )
                    require(
                        struct.unpack_from("<I", raw, 37)[0] == expected_vout,
                        "payment spends unexpected output",
                    )
                ordinals[digest] = ordinal
            require(wire.tell() == end, "source batch contains extra bytes")
        require(not ids.read(1) and not wire.read(1), "trailing source data")
    require(
        len(ordinals) == PAYMENTS
        and ordered.hexdigest() == source["offered_id_sha256"],
        "ordered funding corpus differs",
    )
    return ordinals


def verify_block_membership(
    raw, block, ordinals, seen, cohort_size, *, heights=None, source_cohorts=None
):
    require(
        raw[:80].hex() == block["header_hex"]
        and len(raw) == block["wire_bytes"]
        and hashlib.sha256(raw).hexdigest() == block["wire_sha256"],
        "archived body differs from readback",
    )
    _, txs, outputs = decode_block(raw, block["hash"])
    counts = [0] * COHORTS
    duplicates = foreign = 0
    for tx in txs[1:]:
        ordinal = ordinals.get(dsha(tx))
        if ordinal is None:
            foreign += 1
        elif seen[ordinal]:
            duplicates += 1
        else:
            seen[ordinal] = 1
            if heights is not None:
                heights[ordinal] = block["height"]
            cohort = (
                ordinal // cohort_size
                if source_cohorts is None
                else source_cohorts[ordinal // BATCH_SIZE]
            )
            counts[cohort] += 1
    require(
        len(txs) - 1 == block["ordinary_transactions"]
        and counts == block["confirmed_by_source_cohort"]
        and sum(counts) == block["scheduled_payments"]
        and foreign == block["foreign_payments"]
        and duplicates == block["duplicate_scheduled"],
        "archived body membership differs from its compact observation",
    )
    return txs, outputs


def parent_ages(heights, funding_height, miners, rails=65_536):
    counts = Counter()
    by_miner = {}
    for i, height in enumerate(heights):
        if height == 0:
            continue
        previous = funding_height if i < rails else heights[i - rails]
        require(0 < previous <= height, "final branch has a child without its parent")
        distance = height - previous
        counts[distance] += 1
        role = miners.get(height, "unattributed")
        by_miner.setdefault(role, Counter())[distance] += 1
    return {
        "verified_source_outpoints": len(heights),
        "funding_height": funding_height,
        "confirmed_spends": sum(counts.values()),
        "inclusion_height_minus_parent_creation_height": dict(sorted(counts.items())),
        "distances_by_miner": {
            role: dict(sorted(d.items())) for role, d in by_miner.items()
        },
    }


def observed_bodies(record):
    # A body can be selected, detached and selected again under the same hash.
    return {
        b["hash"]: b for b in record.get("detached_observations", []) + record["blocks"]
    }


def verify_wires(record, corpus, blocks, *, parent_graph=False):
    funding = heights = None
    arm = record.get(
        "arm", "production" if record["registration"] == REPLICATION else "direct"
    )
    source_cohorts = cohort_by_batch(arm) if arm in REORDERED_ARMS else None
    if arm in WITNESSED_ARMS:
        signed_order = hashlib.sha256()
        with (corpus / "ids.bin").open("rb") as f:
            for batch in batch_order(arm):
                f.seek(batch * BATCH_SIZE * 32)
                signed_order.update(f.read(BATCH_SIZE * 32))
        require(
            signed_order.hexdigest() == record["setup"]["scheduled_order_id_sha256"],
            "raw offered ID order differs",
        )
    registered_funding = record.get("setup", {}).get("funding", record["baseline"])
    if parent_graph:
        raw = (corpus / "funding.block").read_bytes()
        require(
            registered_funding["hash"] == FUNDING
            and registered_funding["height"] == 484
            and hashlib.sha256(raw).hexdigest() == registered_funding["wire_sha256"],
            "registered funding receipt differs",
        )
        _, txs, outputs = decode_block(raw, FUNDING)
        require(
            len(txs) == 16385 and all(len(o) == 4 for o in outputs[1:]),
            "expected 65,536 split funding outputs",
        )
        funding = [dsha(tx) for tx in txs[1:]]
        heights = array("I", [0]) * PAYMENTS
    canonical_hashes = {b["hash"] for b in record["blocks"]}
    independent = {}
    for digest, b in observed_bodies(record).items():
        if digest in canonical_hashes:
            continue
        raw = (blocks / (digest + ".block")).read_bytes()
        require(
            raw[:80].hex() == b["header_hex"]
            and len(raw) == b["wire_bytes"]
            and hashlib.sha256(raw).hexdigest() == b["wire_sha256"]
            and b["independent_wire_sha256"]
            == dict.fromkeys(("sv", "tn1", "tn2"), b["wire_sha256"]),
            "detached wire differs from stock observations",
        )
        _, txs, _ = decode_block(raw, digest)
        require(
            len(txs) - 1 == b["ordinary_transactions"],
            "detached transaction count differs",
        )
        independent[digest] = b["wire_sha256"]
    ordinals = read_source(corpus, record["source"], funding=funding)
    seen = bytearray(PAYMENTS)
    for b in record["blocks"]:
        verify_block_membership(
            (blocks / (b["hash"] + ".block")).read_bytes(),
            b,
            ordinals,
            seen,
            PAYMENTS // COHORTS,
            heights=heights,
            source_cohorts=source_cohorts,
        )
    require(
        sum(seen) == record["completion"]["unique_confirmed"],
        "final distinct membership differs",
    )
    wallet = {}
    if arm != "direct" and record["inventory_final"]:
        wallet = inventory_membership(blocks.parent, record["inventory_final"], seen)
    graph = {}
    if parent_graph:
        hashes = {s["hash"]: s["miner"] for s in record["slots"] if s.get("hash")}
        miners = {
            b["height"]: hashes.get(b["hash"], "unattributed") for b in record["blocks"]
        }
        graph["source_parent_ages"] = parent_ages(
            heights, registered_funding["height"], miners
        )
    return {
        **wallet,
        **graph,
        "signed_source_envelopes": len(ordinals),
        "merkle_verified_bodies": len(record["blocks"]),
        **(
            {"merkle_verified_detached_only_bodies": independent} if independent else {}
        ),
        "confirmed_distinct_memberships": sum(seen),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path)
    parser.add_argument("--corpus", type=Path)
    parser.add_argument("--blocks", type=Path)
    parser.add_argument(
        "--parent-ages",
        action="store_true",
        help="verify every signed source outpoint and measure parent/child active-chain height distances",
    )
    parser.add_argument(
        "--build-receipt",
        type=Path,
        help="Linux release-build provenance for an independent reproduced run",
    )
    args = parser.parse_args()
    if args.parent_ages and not args.corpus:
        parser.error("--parent-ages requires the original --corpus and --blocks")
    if bool(args.corpus) != bool(args.blocks):
        parser.error("--corpus and --blocks must be supplied together")
    with (gzip.open if args.receipt.suffix == ".gz" else open)(args.receipt, "rt") as f:
        record = json.load(f)
    registered_source, expected = pinned_build(record.get("arm", "direct"))
    if args.build_receipt:
        b = json.loads(args.build_receipt.read_text())
        require(
            b["source_commit"] == registered_source and b["platform"] == "linux/arm64",
            "unexpected reproduction source or target",
        )
        expected = b["release"]["executable_sha256"]
    summary = verify(record, expected)
    if args.corpus:
        summary["local_wire_replay"] = verify_wires(
            record, args.corpus, args.blocks, parent_graph=args.parent_ages
        )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
