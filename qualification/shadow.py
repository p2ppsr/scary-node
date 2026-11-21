"""Audit observed mainnet shadow transitions. Each gap resets the contiguous run."""
import argparse
import json
from pathlib import Path


def audit(lines):
    previous = None
    longest = current = starts = restored = agreement = assembled = directive_ready = 0
    failures = []
    sources = set()
    blocks = 0
    for number, line in enumerate(lines, 1):
        row = json.loads(line)
        if row.get("event") == "start":
            starts += 1
            if row.get("network") != "mainnet":
                raise ValueError("unexpected network at record " + str(number))
            if previous and row.get("restored_tip") == previous["hash"]:
                restored += 1
            continue
        blocks += 1
        continuous = (previous and row["height"] == previous["height"] + 1
                      and row["previous"] == previous["hash"])
        if previous and not continuous:
            failures.append({"record": number, "kind": "gap_or_reorg", "from": previous["height"], "to": row["height"]})
        if continuous and previous.get("expected_next_bits") not in (None, row["bits"]):
            failures.append({"record": number, "kind": "unexpected_bits"})
        current = current + 1 if continuous else 1
        longest = max(longest, current)
        if row.get("delivery_source"):
            sources.add(row["delivery_source"])
        tips = row.get("reported_tips", {}).values()
        agreement += any(t["hash"] == row["hash"] and t["height"] == row["height"] for t in tips)
        assembled += row.get("unsigned_candidate") is not None
        directive_ready += bool(row["directives"]["ready"])
        previous = row
    return {"observed_transitions": blocks, "longest_contiguous_run": longest,
            "restart_boundaries_recovered": restored, "process_starts": starts,
            "distinct_delivery_sources": len(sources), "contemporaneous_peer_hash_matches": agreement,
            "unsigned_templates": assembled, "directive_ready_transitions": directive_ready,
            "exceptions": failures}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("journal", type=Path)
    with parser.parse_args().journal.open() as journal:
        print(json.dumps(audit(journal), indent=2))
