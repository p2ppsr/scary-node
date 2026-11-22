"""Attribute prospective tip decisions in the existing shadow qualification window to executable starts."""

import argparse
import gzip
import hashlib
import json
import re
from pathlib import Path

from shadow_checkpoint import current_run, replay, utc, verified_attempt


def identity(start):
    value = start.get("executable_sha256")
    return (
        value.lower()
        if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value)
        else None
    )


def describe_tip(index, row):
    return {
        "record": index,
        "height": row["height"],
        "hash": row["hash"],
        "observed_utc": utc(row["observed_at"]),
    }


def align_failovers(transitions, receipt):
    """Use the qualification verifier on the actual four adjacent tip/start transitions."""
    restrictions = [
        i
        for i, event in enumerate(receipt)
        if event.get("event") == "restrict_body_transport"
    ]
    result = []
    for begin, end in zip(restrictions, restrictions[1:] + [len(receipt)]):
        attempt = receipt[begin:end]
        matches = []
        for i in range(len(transitions) - 3):
            group = transitions[i : i + 4]
            if verified_attempt([row for row, _ in group], attempt):
                matches = [ref for _, ref in group]
                break
        result.append(
            {
                "first_receipt_record": begin + 1,
                "last_receipt_record": end,
                "restricted_height": attempt[0].get("height"),
                "verified_in_window": bool(matches),
                "matched_tip_and_start_records": matches,
            }
        )
    return result


def segments(snapshot, qualified):
    records = snapshot["records"]
    count = qualified["qualifying_window_blocks"]
    window = current_run(records)[-count:] if count else []
    first_record = next(
        (i for i, row in enumerate(records, 1) if window and row is window[0]), None
    )
    if (
        len(window) != count
        or (window[0]["height"] if window else None)
        != qualified["qualifying_window_first_height"]
    ):
        raise ValueError("selected records differ from the qualification window")

    groups, starts, transitions = [], [], []
    previous = session = None
    for i, row in enumerate(records, 1):
        event = row.get("event", "tip")
        inside = first_record is not None and i >= first_record
        if event == "start":
            restored = row.get("restored_tip")
            session = {
                "record": i,
                "started_utc": utc(row["at"]),
                "executable_sha256": identity(row),
                "body_transport": row.get("body_transport"),
                "preceding_tip": previous,
                "restored_tip_hash": restored,
                "restores_preceding_tip": previous["hash"] == restored
                if previous
                else None,
                "observed_tips_until_next_start_in_window": 0,
            }
            starts.append(session)
        elif event == "tip":
            previous = describe_tip(i, row)
            if not inside:
                continue
            sha = session["executable_sha256"] if session else None
            start_record = session["record"] if session else None
            if session:
                session["observed_tips_until_next_start_in_window"] += 1
            if not groups or groups[-1]["executable_sha256"] != sha:
                groups.append(
                    {
                        "executable_sha256": sha,
                        "first_tip": previous,
                        "start_record_for_first_tip": start_record,
                        "observed_tips": 0,
                        "transaction_bearing_unsigned_templates": 0,
                    }
                )
            group = groups[-1]
            group["last_tip"] = previous
            group["start_record_for_last_tip"] = start_record
            group["observed_tips"] += 1
            if (row.get("unsigned_candidate") or {}).get("transactions"):
                group["transaction_bearing_unsigned_templates"] += 1
        else:
            continue
        if inside:
            transitions.append(
                (
                    row,
                    {
                        "record": i,
                        "event": event,
                        "executable_sha256": session["executable_sha256"]
                        if session
                        else None,
                    },
                )
            )

    if sum(group["observed_tips"] for group in groups) != count:
        raise ValueError("release segments do not cover the selected tip observations")
    drills = align_failovers(transitions, snapshot["failover"])
    if (
        any(drill["verified_in_window"] for drill in drills)
        != qualified["failover_and_restores_in_current_run"]
    ):
        raise ValueError(
            "transport drill attribution differs from the qualification replay"
        )
    return {
        "schema": 1,
        "qualified": qualified["qualified"],
        "required_observed_tips": qualified["required_linked_blocks"],
        "observed_tips": count,
        "excluded_prefix_in_current_run": qualified["excluded_prefix_blocks"],
        "unknown_executable_tips": sum(
            g["observed_tips"] for g in groups if g["executable_sha256"] is None
        ),
        "transaction_bearing_unsigned_templates": sum(
            g["transaction_bearing_unsigned_templates"] for g in groups
        ),
        "templates_with_verified_successor": qualified[
            "templates_with_verified_successor"
        ],
        "segments": groups,
        "starts": [
            s
            for s in starts
            if first_record is not None
            and (
                s["record"] >= first_record
                or s["observed_tips_until_next_start_in_window"]
            )
        ],
        "failover_attempts": drills,
    }


def report(path):
    checked = replay(path, assess=True)
    archived = path.read_bytes()
    if hashlib.sha256(archived).hexdigest() != checked["observations_sha256"]:
        raise ValueError("archive changed between qualification replay and attribution")
    result = segments(json.loads(gzip.decompress(archived)), checked["qualification"])
    result["archive_sha256"] = checked["observations_sha256"]
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("archive", type=Path)
    print(json.dumps(report(p.parse_args().archive), indent=2))
