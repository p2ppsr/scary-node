"""Verify native proof of work, continuity, independently reported wire digests, validation counts and throughput."""

import argparse
import json
import math
import statistics
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "regtest"))
from verify_receipt import check_header


def verify(data):
    fanout, rows = data["transactions_per_block"], data["rows"]
    assert data["network"] == "regtest" and fanout in (1024, 4096, 16384)
    assert data["rounds"] in (8, 32, 64) and len(rows) == data["rounds"]
    assert set(data["docker"]) == {"architecture", "vcpus", "memory_bytes"}
    assert data["docker"]["vcpus"] >= 1 and data["docker"]["memory_bytes"] > 0
    assert len(bytes.fromhex(data["scary_binary_sha256"])) == 32
    parent = data["funding_block"]
    check_header(bytes.fromhex(data["funding_header"]), parent)
    start_height = rows[0]["height"]
    last = 0
    for i, row in enumerate(rows):
        assert row["height"] == start_height + i
        check_header(bytes.fromhex(row["header_hex"]), row["hash"], parent)
        parent = row["hash"]
        digests = row["independent_wire_sha256"]
        assert set(digests) == {"sv", "tn1", "tn2"} and set(digests.values()) == {
            row["wire_sha256"]
        }
        assert len(bytes.fromhex(row["wire_sha256"])) == 32
        assert row["bytes"] > 80 + 60 * fanout
        assert row["witnessed_signature_checks"] == fanout
        assert last <= row["offered_ns"] < row["source_accepted_ns"]
        last = row["source_accepted_ns"]
    assert last <= data["measured_wall_ns"]
    total = len(rows) * fanout
    tps = total * 1_000_000_000 / data["measured_wall_ns"]
    assert math.isclose(tps, data["transactions_per_second"], rel_tol=1e-12)
    acceptance_ms = [
        (row["source_accepted_ns"] - row["offered_ns"]) / 1e6 for row in rows
    ]
    observations = {}
    if "final_native_observations" in data:
        names = {"tn1", "tn2", "scary_node"}
        final = data["final_native_observations"]
        assert set(final) == names
        assert all(
            entry["height"] == rows[-1]["height"]
            and entry["hash"] == rows[-1]["hash"]
            and last <= entry["first_observed_ns"] <= data["measured_wall_ns"]
            for entry in final.values()
        )
        lagging = {name: 0 for name in sorted(names)}
        blocked, tied = 0, 0
        for i, row in enumerate(rows):
            floor = row["height"] - data["receiver_window"] + 1
            polls = row.get("receiver_polls", [])
            assert bool(polls) == (i >= data["receiver_window"] - 1)
            sample_time = row["source_accepted_ns"]
            upper = (
                rows[i + 1]["offered_ns"]
                if i + 1 < len(rows)
                else data["measured_wall_ns"]
            )
            for j, poll in enumerate(polls):
                heights = poll["heights"]
                assert set(heights) == names and sample_time <= poll["read_ns"] <= upper
                sample_time = poll["read_ns"]
                lowest = min(heights.values())
                assert max(heights.values()) <= row["height"]
                assert (lowest >= floor) == (j == len(polls) - 1)
                if lowest < floor:
                    blocked += 1
                    slow = [k for k, h in heights.items() if h == lowest]
                    tied += int(len(slow) > 1)
                    for k in slow:
                        lagging[k] += 1
        observations = {
            "blocked_polls": blocked,
            "tied_lowest_polls": tied,
            "lowest_observed_height_counts": lagging,
            "seconds_after_final_source_acceptance": {
                k: (entry["first_observed_ns"] - last) / 1e9
                for k, entry in sorted(final.items())
            },
        }
    return {
        **observations,
        "blocks": len(rows),
        "median_source_submit_ms": statistics.median(acceptance_ms),
        "seconds_to_final_adoption_after_source": (data["measured_wall_ns"] - last)
        / 1e9,
        "signed_spends": total,
        "wire_bytes": sum(r["bytes"] for r in rows),
        "full_node_readbacks": 3 * len(rows),
        "seconds": data["measured_wall_ns"] / 1e9,
        "transactions_per_second": tps,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path)
    print(
        json.dumps(
            verify(json.loads(parser.parse_args().receipt.read_text())), indent=2
        )
    )
