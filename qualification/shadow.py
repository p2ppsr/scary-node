"""Audit the passive mainnet journal, unsigned commitments and contemporaneous tip reports."""

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path


def digest(data):
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def merkle(ids):
    while len(ids) > 1:
        if len(ids) % 2:
            ids.append(ids[-1])
        ids = [digest(a + b) for a, b in zip(ids[::2], ids[1::2])]
    return ids[0]


def check_header(row):
    raw = bytes.fromhex(row["header_hex"])
    if len(raw) != 80 or digest(raw)[::-1].hex() != row["hash"]:
        raise ValueError("observed header hash differs")
    bits = int.from_bytes(raw[72:76], "little")
    exponent, mantissa = bits >> 24, bits & 0x7FFFFF
    if (
        bits != row["bits"]
        or bits & 0x800000
        or not 3 <= exponent <= 32
        or mantissa == 0
    ):
        raise ValueError("observed difficulty encoding differs")
    target = mantissa << (8 * (exponent - 3))
    if target > 0xFFFF << 208 or int.from_bytes(digest(raw), "little") > target:
        raise ValueError("observed header proof of work differs")
    if raw[4:36][::-1].hex() != row["previous"]:
        raise ValueError("observed parent differs")


def check_template(row):
    job = row["unsigned_candidate"]
    header = bytes.fromhex(job["header_hex"])
    leaves = [digest(bytes.fromhex(job["coinbase"]))] + [
        bytes.fromhex(h)[::-1] for h in job["transactions"]
    ]
    if (
        len(header) != 80
        or job["height"] != row["height"] + 1
        or header[4:36][::-1].hex() != row["hash"]
        or header[36:68] != merkle(leaves)
        or int.from_bytes(header[72:76], "little") != job["bits"]
        or job["bits"] != row["expected_next_bits"]
        or not row["directives"]["ready"]
    ):
        raise ValueError(
            "unsigned template differs from selected parent, commitments or effective rules"
        )


def audit(lines):
    previous = previous_source = None
    longest = current = starts = restored = assembled = directive_ready = 0
    path_switches = forced_switches = checked_headers = 0
    transport = "all"
    references = defaultdict(list)
    tips = []
    failures = []
    sources = set()
    for number, line in enumerate(lines, 1):
        row = json.loads(line)
        event = row.get("event", "tip")
        if event == "start":
            starts += 1
            if row.get("network") != "mainnet":
                raise ValueError("unexpected network at record " + str(number))
            if previous and row.get("restored_tip") == previous["hash"]:
                restored += 1
            transport = row.get("body_transport", "all")
            continue
        if event == "reference_tip":
            references[(row["height"], row["hash"])].append(row["observed_at"])
            continue
        if event != "tip":
            raise ValueError("unknown journal record " + str(number))
        if row.get("header_hex"):
            check_header(row)
            checked_headers += 1
        continuous = (
            previous
            and row["height"] == previous["height"] + 1
            and row["previous"] == previous["hash"]
        )
        if previous and not continuous:
            failures.append(
                {
                    "record": number,
                    "kind": "gap_or_reorg",
                    "from": previous["height"],
                    "to": row["height"],
                }
            )
        if continuous and previous.get("expected_next_bits") not in (None, row["bits"]):
            failures.append({"record": number, "kind": "unexpected_bits"})
        current = current + 1 if continuous else 1
        longest = max(longest, current)
        source = row.get("delivery_source")
        direct = row.get("receipt_trigger_hash") == row["hash"]
        if source and direct:
            sources.add(source)
            if previous_source and previous_source != source:
                path_switches += 1
                if (
                    continuous
                    and transport == "sv_only"
                    and source.startswith("sv:")
                    and not previous_source.startswith("sv:")
                ):
                    forced_switches += 1
            previous_source = source
        advertised = any(
            t["hash"] == row["hash"] and t["height"] == row["height"]
            for t in row.get("reported_tips", {}).values()
        )
        tips.append(((row["height"], row["hash"]), row["observed_at"], advertised))
        if row.get("unsigned_candidate") is not None:
            check_template(row)
            assembled += 1
        directive_ready += bool(row["directives"]["ready"])
        previous = row
    agreed = sum(
        snapshot or any(abs(at - t) <= 7200 for at in references[key])
        for key, t, snapshot in tips
    )
    return {
        "observed_transitions": len(tips),
        "longest_contiguous_run": longest,
        "current_contiguous_run": current,
        "restart_boundaries_recovered": restored,
        "process_starts": starts,
        "headers_rechecked": checked_headers,
        "distinct_delivery_sources": len(sources),
        "delivery_source_switches": path_switches,
        "sv_only_failovers": forced_switches,
        "contemporaneous_peer_hash_matches": agreed,
        "unsigned_templates": assembled,
        "directive_ready_transitions": directive_ready,
        "exceptions": failures,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("journal", type=Path)
    with parser.parse_args().journal.open() as journal:
        print(json.dumps(audit(journal), indent=2))
