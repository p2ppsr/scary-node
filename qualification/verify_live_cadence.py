"""Independently reconcile a registered cadence run with its native observations."""

import argparse
import gzip
import json
import math
from pathlib import Path

from live_cadence_body import COUNT, FUNDING, HEIGHT, RAILS, check_header, dsha, require

PEERS = {"sv", "tn1", "tn2", "scary"}
STOCK = PEERS - {"scary"}
EVIDENCE = Path(__file__).resolve().parents[1] / "docs/evidence"


def check(result, events):
    fixture = json.loads((EVIDENCE / "chronicle-live-cadence-body.json").read_bytes())
    build = json.loads((EVIDENCE / "chronicle-recovery-linux-build.json").read_bytes())
    require(
        result["network"] == "regtest"
        and result["project"] == "scary-node-live-cadence",
        "wrong network or registered project",
    )
    require(
        result["executable_sha256"] == build["release"]["executable_sha256"],
        "unexpected entrant executable",
    )
    require(
        result["fixture"] == fixture and result["starting_parent"] == FUNDING,
        "fixture differs from independently reconstructed body",
    )
    ident = fixture["block_hash"]
    check_header(bytes.fromhex(fixture["header_hex"]), ident, FUNDING)
    require(
        fixture["height"] == HEIGHT and fixture["payment_count"] == COUNT,
        "unexpected enrolled population",
    )
    events = list(events)
    require(
        events and events[-1]["event"] == "finished", "missing terminal observation"
    )
    require(
        all(isinstance(e["elapsed_ns"], int) and e["elapsed_ns"] >= 0 for e in events),
        "invalid elapsed time",
    )

    observed, settled, stored = {}, {}, {}
    transport = {}
    successors = []
    prev = ident
    for header_hex in result.get("successors", []):
        header = bytes.fromhex(header_hex)
        candidate = dsha(header)[::-1].hex()
        check_header(header, candidate, prev)
        successors.append(candidate)
        prev = candidate
    for event in events:
        name = event["event"]
        if name in (
            "ready_to_announce",
            "stock_requested",
            "body_written",
            "stock_pong",
        ):
            require(name not in transport, "duplicate native handoff event")
            transport[name] = event["elapsed_ns"]
        elif name == "selected":
            peer = event["peer"]
            require(peer in PEERS, "unexpected observing peer")
            if event["block_hash"] == ident:
                group = observed
            else:
                require(
                    len(successors) == 2 and event["block_hash"] == successors[-1],
                    "terminal peer report names an unexpected successor",
                )
                group = settled
            require(peer not in group, "duplicate peer selection")
            group[peer] = event["elapsed_ns"]
        elif name == "body_read":
            peer = event["peer"]
            require(peer in STOCK and peer not in stored, "unexpected stock readback")
            stored[peer] = {
                k: event[k] for k in ("wire_sha256", "wire_bytes", "elapsed_ns")
            }

    require(
        result.get("observations", {}) == observed
        and result.get("reads", {}) == stored
        and result.get("settled", {}) == settled,
        "summaries differ from native observations",
    )
    if "transport" in result:
        require(result["transport"] == transport, "native handoff timestamps differ")
    names = ("ready_to_announce", "stock_requested", "body_written", "stock_pong")
    require(
        tuple(transport) == names[: len(transport)]
        and list(transport.values()) == sorted(transport.values()),
        "wrong transport sequence",
    )

    seconds, rate = None, None
    if set(observed) == PEERS:
        elapsed = max(observed.values()) - transport["ready_to_announce"]
        require(elapsed > 0, "invalid four-peer adoption interval")
        seconds, rate = elapsed / 1e9, COUNT * 1e9 / elapsed
        # Interrupted runs may have received all peer observations before computing the summary.
        if "payments_per_second" in result or result["completed"]:
            require(
                result["four_node_elapsed_ns"] == elapsed
                and math.isclose(result["payments_per_second"], rate, rel_tol=1e-12),
                "published adoption rate has a different numerator or denominator",
            )
    valid_reads = {
        p
        for p, row in stored.items()
        if row["wire_bytes"] == fixture["wire_bytes"]
        and row["wire_sha256"] == fixture["wire_sha256"]
        and p in observed
        and row["elapsed_ns"] >= observed[p]
    }
    checks = result.get("witnessed_validation", {})
    determinate = (
        checks.get("hash") == ident
        and checks.get("same_block_inputs") == COUNT - RAILS
        and checks.get("coinbase_reward_checked") is True
        and all(
            checks.get(k) == COUNT
            for k in (
                "known_inputs",
                "script_checks_passed",
                "amount_checks_passed",
                "transactions_determinate",
            )
        )
    )
    complete = (
        len(transport) == 4
        and set(observed) == PEERS
        and valid_reads == STOCK
        and len(successors) == 2
        and set(settled) == PEERS
        and determinate
        and not result.get("failure")
        and not result.get("errors")
        and not result["resource_stop"]
    )
    passed = bool(complete and rate >= 3000)
    for record in (result, events[-1]):
        require(
            record["completed"] is complete and record["cadence_target_met"] is passed,
            "published completion or rate verdict disagrees with recorded evidence",
        )
    return {
        "selected_peers": sorted(observed),
        "correct_stock_readbacks": sorted(valid_reads),
        "complete_witnessed_validation": determinate,
        "settled_peers": sorted(settled),
        "four_node_seconds": seconds,
        "payments_per_second": rate,
        "cadence_target_met": passed,
    }


def operating_summary(receipt):
    setup = receipt["setup"]
    require(
        setup["executable_sha256"] == receipt["result"]["executable_sha256"]
        and setup["events"][-1]["selected"] == FUNDING,
        "different prepared release or boundary",
    )
    require(
        setup["capacity"]["block_bytes"] == 512 << 20
        and setup["capacity"]["journal_bytes"] == 1 << 30
        and setup["capacity"]["undo_items"] == 8_000_000,
        "unexpected entrant capacity",
    )
    namespace = receipt["namespace_readback"]["stores"]
    for name in ("as1", "as2"):
        row = namespace[name]
        require(
            all(
                row[k] == 5 << 30
                for k in (
                    "configured_file_bytes",
                    "namespace_total_bytes",
                    "stat_file_bytes",
                )
            ),
            "incorrect active namespace capacity",
        )
        require(
            row["volume"] in {m["volume"] for m in setup["topology"][name]["mounts"]},
            "namespace evidence belongs to another mount",
        )
    require(
        namespace["as1"]["volume"] != namespace["as2"]["volume"], "shared UTXO store"
    )
    rows = receipt["resources"]
    require(rows, "missing resource trace")

    def minimum(key):
        values = [r[key] for r in rows if r.get(key) is not None]
        return min(values) if values else None

    services = sorted(set().union(*(r.get("services", {}).keys() for r in rows)))
    peaks = {}
    for name in services:
        values = [
            r["services"][name]["memory_bytes"]
            for r in rows
            if "memory_bytes" in r.get("services", {}).get(name, {})
        ]
        if values:
            peaks[name] = max(values)
    return {
        "samples": len(rows),
        "minimum_host_free_bytes": minimum("host_filesystem_free_bytes"),
        "minimum_docker_free_bytes": minimum("docker_filesystem_free_bytes"),
        "sampled_peak_memory_bytes": peaks,
    }


def read(path):
    with gzip.open(path, "rt") if path.suffix == ".gz" else path.open() as src:
        return json.load(src)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "receipt", type=Path, help="published receipt or completed run directory"
    )
    parser.add_argument(
        "--export", type=Path, help="write the verified receipt to a fresh .json.gz"
    )
    args = parser.parse_args()
    if args.receipt.is_dir():
        directory = args.receipt
        setup = read(directory / "setup.json")
        recorded = directory / "namespace-readback.json"
        namespace = (
            read(recorded)
            if recorded.exists()
            else {
                "stores": {
                    n: setup["topology"][n]["namespace_readback"]
                    for n in ("as1", "as2")
                }
            }
        )

        def lines(name):
            return [
                json.loads(line) for line in (directory / name).read_text().splitlines()
            ]

        receipt = {
            "setup": setup,
            "namespace_readback": namespace,
            "result": read(directory / "result.json"),
            "measurement_events": lines("measurement.jsonl"),
            "resources": lines("resources.jsonl"),
        }
    else:
        receipt = read(args.receipt)
    summary = {
        **check(receipt["result"], receipt["measurement_events"]),
        "resources": operating_summary(receipt),
    }
    if args.export:
        require(args.export.suffix == ".gz", "use a fresh .json.gz destination")
        raw = json.dumps(receipt, separators=(",", ":"), allow_nan=False).encode()
        with args.export.open("xb") as out:
            out.write(gzip.compress(raw, mtime=0))
    print(json.dumps(summary, indent=2))
