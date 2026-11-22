"""Reconcile the observed prefix of an interrupted, preregistered native-wallet workload."""

import argparse
import gzip
import hashlib
import json
from collections import Counter
from pathlib import Path

from verify_sustained_native import (
    BATCH_SIZE,
    BATCHES,
    NS,
    PAYMENTS,
    REGISTRATIONS,
    SLOTS,
    batch_order,
    check_header,
    intake_delta,
    pinned_build,
    replication_setup,
    require,
    resources,
)

SCHEMA = "scary-sustained-interruption-v1"
ARCHIVE_LOGS = {
    "slots": "slots",
    "producer": "producer",
    "blocks": "body-events",
    "resources": "resources",
    "samples": "samples",
    "stock": "stock",
    "inventory_progress": "inventory-progress",
    "retries": "retries",
}


def capture(directory, output):
    require(
        not (directory / "receipt.json.gz").exists(), "a closed primary receipt exists"
    )
    result = json.loads((directory / "attempt.json").read_bytes())
    result.update(
        schema=SCHEMA,
        network="regtest",
        setup=json.loads((directory / "setup.json").read_bytes()),
    )
    logs = {}
    for field, stem in ARCHIVE_LOGS.items():
        data = (directory / (stem + ".jsonl")).read_bytes()
        lines = data.splitlines(keepends=True)
        complete = [s for s in lines if s.endswith(b"\n")]
        result[field] = [json.loads(line) for line in complete]
        logs[field] = {
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "complete_lines": len(complete),
            "partial_tail_bytes": len(data) - sum(map(len, complete)),
            **(
                {"partial_tail_hex": lines[-1].hex()}
                if len(complete) < len(lines)
                else {}
            ),
        }
    result["original_logs"] = logs
    frozen = json.loads((directory / "interruption/frozen.private.json").read_bytes())
    require(
        frozen["arm"] == result["arm"]
        and frozen["registration"] == result["registration"],
        "interruption handoff belongs to another study",
    )
    result["after_interruption"] = {
        "first_wall_ns": frozen["first_wall_ns"],
        "samples": frozen["samples"],
        "pause_start_wall_ns": frozen["pause_start_wall_ns"],
        "freeze_verified_wall_ns": frozen["verified_wall_ns"],
        "paused_services": {
            role: row["paused"] for role, row in frozen["after"].items()
        },
    }
    # This witness exposes aggregate queue commitments. Individual mempool members stay
    # recoverable from the public signed source and measured block membership.
    queue = json.loads(
        (directory / "interruption/queue-evidence.private.json").read_bytes()
    )
    result["after_interruption"]["queue_readbacks"] = [
        {
            "wall_ns": r["wall_ns"],
            "finished_wall_ns": r["finished_wall_ns"],
            "stock": r["stock"],
            "teranodes": {k: r[k] for k in ("tn1", "tn2")},
            "scary": {
                k: r["scary"][k]
                for k in (
                    "height",
                    "tip_hash",
                    "pending_transactions",
                    "witnessed_outputs",
                )
            },
        }
        for r in queue["samples"]
    ]
    verify(result)
    with output.open("xb") as stream:
        stream.write(
            gzip.compress(json.dumps(result, separators=(",", ":")).encode(), mtime=0)
        )


def observed_empty_pool_suffix(samples):
    """Describe the observed empty-pool tail using timestamped status replies."""
    last_populated = next(
        (
            i
            for i in reversed(range(len(samples)))
            if samples[i]["scary"].get("pending", 0) > 0
        ),
        None,
    )
    if last_populated is None:
        return None
    earlier = samples[last_populated]["scary"]
    tail = samples[last_populated + 1 :]
    observed = [row["scary"] for row in tail if "pending" in row.get("scary", {})]
    if not observed:
        return None
    return {
        "last_populated_status": {
            k: earlier[k] for k in ("height", "read_ns", "pending")
        },
        "first_empty_status": {k: observed[0][k] for k in ("height", "read_ns")},
        "last_status": {k: observed[-1][k] for k in ("height", "read_ns")},
        "empty_status_replies": len(observed),
        "unavailable_status_replies": len(tail) - len(observed),
        "minimum_connected_peers": min(row["connected_peers"] for row in observed),
        "ready_replies": sum(row["ready"] for row in observed),
        "storage_failure_replies": sum(row["storage_failed"] for row in observed),
        "intake_change_since_last_populated": intake_delta(samples[last_populated:]),
    }


def verify(record):
    require(
        record["schema"] == SCHEMA
        and record["arm"] == "endorsed"
        and record["network"] == "regtest",
        "unexpected interruption receipt",
    )
    arm = record["arm"]
    require(
        record["registration"] == REGISTRATIONS[arm]
        and record["release_sha256"] == pinned_build(arm)[1],
        "different registration or executable",
    )
    replication_setup(record)
    require(
        not record["retries"], "unexpected retry record in the interrupted offer prefix"
    )
    require(
        record["original_logs"].keys() == ARCHIVE_LOGS.keys(),
        "missing original source log",
    )
    for k, orig in record["original_logs"].items():
        digest = hashlib.sha256()
        size = 0
        for row in record[k]:
            line = (json.dumps(row, separators=(",", ":")) + "\n").encode()
            size += len(line)
            digest.update(line)
        tail = bytes.fromhex(orig.get("partial_tail_hex", ""))
        digest.update(tail)
        require(
            orig["complete_lines"] == len(record[k])
            and orig["sha256"] == digest.hexdigest()
            and orig["partial_tail_bytes"] == len(tail)
            and orig["bytes"] == size + len(tail),
            "original source log contents or framing differ",
        )
    offers = record["producer"]
    slots = record["slots"]
    require(
        0 < len(offers) < BATCHES and 0 < len(slots) < SLOTS,
        "interruption must preserve the observed prefix",
    )
    for i, row in enumerate(offers):
        require(
            row["batch"] == batch_order(arm)[i]
            and row["position"] == i
            and row["phase"] == "offer"
            and row["sender"] == row["batch"] % 4
            and row["count"] == BATCH_SIZE
            and row["outcome"] == "sent"
            and row["scheduled_ns"] == i * NS // 6
            and row["started_ns"] <= row["finished_ns"],
            "native offer order or outcome differs",
        )
    selected = {b["hash"]: b for b in record["blocks"]}
    origins = {}
    windows = [
        {"slots_recorded": 0, "scary_selected_payments": 0, "scary_selected_blocks": 0}
        for _ in range(6)
    ]
    dispositions = Counter()
    for i, s in enumerate(slots):
        miner = "sv" if i % 4 == 3 else "scary"
        require(
            s["index"] == i
            and s["miner"] == miner
            and s["phase"] == "offer"
            and s["scheduled_ns"] == (i + 1) * 6 * NS
            and s["started_ns"] <= s["finished_ns"],
            "opportunity order differs",
        )
        require(
            s["outcome"] in ("solved", "unready", "error", "rejected", "overlap"),
            "unknown recorded miner outcome",
        )
        window = windows[i // 50]
        window["slots_recorded"] += 1
        dispositions[(miner, s["outcome"])] += 1
        if s["outcome"] == "solved":
            h = s["hash"]
            require(h not in origins, "duplicate originating solution")
            origins[h] = miner
            if miner == "scary":
                check_header(bytes.fromhex(s["solution_header"]), h, s["parent"])
                if h in selected and selected[h]["scheduled_payments"]:
                    window["scary_selected_payments"] += selected[h][
                        "scheduled_payments"
                    ]
                    window["scary_selected_blocks"] += 1
    parent = record["baseline"]["hash"]
    by_miner = Counter()
    for height, b in enumerate(record["blocks"], record["baseline"]["height"] + 1):
        h = b["hash"]
        check_header(bytes.fromhex(b["header_hex"]), h, parent)
        require(
            b["height"] == height
            and b["entrant_hash"] == h
            and b["scheduled_payments"]
            == b["entrant_script_checks"]
            == b["ordinary_transactions"]
            and not (b["foreign_payments"] or b["duplicate_scheduled"])
            and b["independent_wire_sha256"]
            == dict.fromkeys(("sv", "tn1", "tn2"), b["wire_sha256"]),
            "body or validation receipt differs",
        )
        by_miner[origins.get(h, "unattributed")] += b["scheduled_payments"]
        parent = h
    seen = sum(by_miner.values())
    offered = sum(r["count"] for r in offers)
    last = offers[-1]["finished_ns"]
    end = record["start_wall_ns"] + last
    samples = record["samples"][-1]
    require(
        all(samples[k]["hash"] == parent for k in ("sv", "tn1", "tn2", "scary")),
        "four last primary tip readings disagree",
    )
    host = resources(record["resources"])
    after = record["after_interruption"]
    require(
        end
        < after["first_wall_ns"]
        <= after["samples"][0]["wall_ns"]
        <= after["samples"][-1]["finished_wall_ns"]
        <= after["pause_start_wall_ns"]
        <= after["freeze_verified_wall_ns"],
        "handoff timing differs",
    )
    require(
        set(after["paused_services"])
        == {
            "svref",
            "tn1",
            "tn2",
            "scary-node",
            "as1",
            "as2",
            "ingress",
            "wallet-ingress",
        }
        and all(after["paused_services"].values()),
        "unverified original service freeze",
    )
    for sample in after["samples"]:
        require(
            all(row["hash"] == parent for row in sample["tips"].values())
            and sample["stock"]["getmempoolinfo"]["size"] == offered - seen,
            "post-interruption pools or tips differ",
        )
    q = after["queue_readbacks"]
    require(
        len(q) == 2
        and q[0]["wall_ns"] > after["freeze_verified_wall_ns"]
        and q[1]["wall_ns"] >= q[0]["finished_wall_ns"]
        and q[0]["stock"] == q[1]["stock"]
        and q[0]["stock"]["pending"] == offered - seen
        and q[0]["stock"]["tip"] == parent,
        "separate post-interruption mempool readbacks disagree",
    )
    return {
        "registered_offer_batches": BATCHES,
        "observed_offer_batches": len(offers),
        "advertised_transactions": offered,
        "scheduled_transactions_without_send_record": PAYMENTS - offered,
        "last_logged_inventory_advertisement_ns": last,
        "last_logged_delivery": record["inventory_progress"][-1],
        "primary_opportunities": SLOTS,
        "recorded_opportunities": len(slots),
        "opportunities_without_outcomes": SLOTS - len(slots),
        "recorded_dispositions": {
            m: {k: v for (who, k), v in dispositions.items() if who == m}
            for m in ("scary", "sv")
        },
        "audited_bodies": len(selected),
        "final_audited_height": record["blocks"][-1]["height"],
        "final_audited_hash": parent,
        "confirmed_by_origin": dict(by_miner),
        "confirmed_payments": seen,
        "observed_windows": windows,
        "per_window_criterion_already_missed": any(
            w["slots_recorded"] == 50 and not w["scary_selected_blocks"]
            for w in windows
        ),
        "final_stock_mempool_after_interruption": q[0]["stock"]["pending"],
        "sorted_internal_mempool_txids_sha256": q[0]["stock"][
            "sorted_internal_txids_sha256"
        ],
        "minimum_host_free_bytes": host["lowest_host_free_bytes"],
        "minimum_docker_free_bytes": host["lowest_docker_free_bytes"],
        "observed_empty_pool_suffix": observed_empty_pool_suffix(record["samples"]),
        "scary_unready_reasons": dict(
            Counter(
                r.get("job_wait_reason") or "receiver preconditions"
                for r in slots
                if r["miner"] == "scary" and r["outcome"] == "unready"
            )
        ),
        "scary_unready_with_receiver_present": sum(
            r["receiver_ready"]
            for r in slots
            if r["miner"] == "scary" and r["outcome"] == "unready"
        ),
    }


def compare_membership(record, corpus, blocks):
    from array import array

    from prove import decode_block, dsha
    from verify_sustained_native import (
        FUNDING,
        parent_ages,
        read_source,
        verify_block_membership,
    )

    summary = verify(record)
    funding = (corpus / "funding.block").read_bytes()
    require(
        hashlib.sha256(funding).hexdigest()
        == record["setup"]["funding"]["wire_sha256"],
        "different funding bytes",
    )
    _, funding_txs, funding_outputs = decode_block(funding, FUNDING)
    require(
        len(funding_txs) == 16385 and all(len(o) == 4 for o in funding_outputs[1:]),
        "different funding rails",
    )
    initial = [value for tx in funding_outputs[1:] for value, _ in tx]
    ordinals = read_source(
        corpus, record["source"], funding=[dsha(tx) for tx in funding_txs[1:]]
    )
    submitted = summary["advertised_transactions"]
    seen = bytearray(PAYMENTS)
    heights = array("I", [0]) * PAYMENTS
    amounts = array("Q", [0]) * PAYMENTS
    claimed = Counter()
    fees = Counter()
    miners = {s["hash"]: s["miner"] for s in record["slots"] if "hash" in s}
    block_miners = {}
    for b in record["blocks"]:
        ident = b["hash"]
        raw = (blocks / (ident + ".block")).read_bytes()
        verify_block_membership(raw, b, ordinals, seen, PAYMENTS // 6, heights=heights)
        _, txs, outputs = decode_block(raw, ident)
        total_fee = 0
        for tx, out in zip(txs[1:], outputs[1:], strict=True):
            n = ordinals[dsha(tx)]
            require(
                n < submitted and len(out) == 1,
                "non-offered payment or unexpected outputs",
            )
            input_value = initial[n] if n < len(initial) else amounts[n - len(initial)]
            amounts[n] = out[0][0]
            require(
                input_value >= amounts[n] and input_value > 0,
                "input has no earlier evidenced value",
            )
            total_fee += input_value - amounts[n]
        miner = miners.get(ident, "unattributed")
        subsidy = 5_000_000_000 >> (b["height"] // 150)
        payout = sum(value for value, _ in outputs[0])
        require(
            payout <= subsidy + total_fee, "coinbase exceeds evidenced fees and subsidy"
        )
        fees[miner] += total_fee
        claimed[miner] += max(0, payout - subsidy)
        block_miners[b["height"]] = miner
    require(
        sum(seen) == summary["confirmed_payments"], "different confirmed membership"
    )
    with (corpus / "ids.bin").open("rb") as f:
        ids = f.read(submitted * 32)
    pending = b"".join(
        sorted(ids[i * 32 : (i + 1) * 32] for i in range(submitted) if not seen[i])
    )
    require(
        len(pending) // 32 == summary["final_stock_mempool_after_interruption"]
        and hashlib.sha256(pending).hexdigest()
        == summary["sorted_internal_mempool_txids_sha256"],
        "confirmed-offer complement differs from stock mempool",
    )
    return {
        "source_envelopes_verified": len(ordinals),
        "merkle_verified_bodies": len(block_miners),
        "confirmed_distinct_memberships": sum(seen),
        "advertised_unmined_matched_stock_mempool": len(pending) // 32,
        "transaction_fees_satoshis_by_origin": dict(fees),
        "claimed_above_subsidy_satoshis_by_origin": dict(claimed),
        "source_parent_ages": parent_ages(
            heights, record["setup"]["funding"]["height"], block_miners
        ),
    }


def restore_stock_mempool(stock, record, corpus, private_ids):
    """Reoffer the original pending set in dependency order on a fresh reference chain."""
    from sustained_block_archive import signed_frames

    summary = verify(record)
    original = gzip.decompress(private_ids.read_bytes())
    count = summary["final_stock_mempool_after_interruption"]
    digest = summary["sorted_internal_mempool_txids_sha256"]
    require(
        len(original) == count * 32 and hashlib.sha256(original).hexdigest() == digest,
        "pending identifiers differ from original stock query",
    )
    wanted = {original[i : i + 32] for i in range(0, len(original), 32)}
    require(
        len(wanted) == count
        and sorted(wanted)
        == [original[i : i + 32] for i in range(0, len(original), 32)],
        "original mempool identifiers are duplicated or unordered",
    )
    require(
        stock.rpc("getrawmempool") == [],
        "fresh stock already has unconfirmed transactions",
    )
    accepted = 0
    with (
        signed_frames(corpus, record["source"]) as (frames, starts),
        (corpus / "ids.bin").open("rb") as ids,
    ):
        batch = []
        for ordinal in range(summary["advertised_transactions"]):
            ident = ids.read(32)
            if ident not in wanted:
                continue
            raw = frames[starts[ordinal] + 24 : starts[ordinal + 1]]
            batch.append({"hex": raw.hex()})
            if len(batch) == 256:
                reply = stock.rpc("sendrawtransactions", batch)
                require(
                    not any(reply.get(key) for key in ("invalid", "evicted", "known")),
                    "stock refused or already held a reoffered payment",
                )
                accepted += len(batch)
                batch = []
        if batch:
            reply = stock.rpc("sendrawtransactions", batch)
            require(
                not any(reply.get(key) for key in ("invalid", "evicted", "known")),
                "stock refused the final mempool batch",
            )
            accepted += len(batch)
    reopened = b"".join(
        sorted(bytes.fromhex(value)[::-1] for value in stock.rpc("getrawmempool"))
    )
    require(
        accepted == count and reopened == original,
        "restored stock mempool differs in membership",
    )
    return {
        "fresh_initial_count": 0,
        "reoffered": accepted,
        "restored_count": len(reopened) // 32,
        "sorted_internal_txids_sha256": hashlib.sha256(reopened).hexdigest(),
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    cmd = p.add_subparsers(dest="command", required=True)
    c = cmd.add_parser("capture")
    c.add_argument("directory", type=Path)
    c.add_argument("receipt", type=Path)
    v = cmd.add_parser("verify")
    v.add_argument("receipt", type=Path)
    v.add_argument("--corpus", type=Path)
    v.add_argument("--blocks", type=Path)
    a = p.parse_args()
    if a.command == "capture":
        capture(a.directory, a.receipt)
    else:
        with gzip.open(a.receipt, "rt") as f:
            r = json.load(f)
        if bool(a.corpus) != bool(a.blocks):
            p.error("use --blocks and --corpus together")
        print(json.dumps(verify(r), indent=2))
        if a.corpus:
            print(json.dumps(compare_membership(r, a.corpus, a.blocks), indent=2))
