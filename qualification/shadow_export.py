"""Export passive observations and failover evidence using run-local peer identifiers."""

import argparse
import io
import json
from pathlib import Path
from shadow import audit


def export(journal, failover):
    peers, assets = {}, {}

    def alias(mapping, key, prefix):
        return mapping.setdefault(key, f"{prefix}-{len(mapping) + 1}")

    def transport(raw):
        if raw.startswith("sv:"):
            return "sv:" + alias(peers, raw, "peer")
        return "asset:" + alias(assets, raw, "endpoint")

    records = []
    with journal.open() as file:
        for line in file:
            if not line.endswith("\n"):
                break
            row = json.loads(line)
            if row.get("event") == "start":
                interpreter = row.pop("interpreter")
                row["interpreter"] = {
                    k: interpreter[k]
                    for k in (
                        "bdk_commit",
                        "sv_node_commit",
                        "native_archive_sha256",
                        "linked_bdk_version",
                        "linked_sv_version",
                    )
                }
            if "delivery_source" in row:
                row["delivery_source"] = transport(row["delivery_source"])
            if row.get("event") == "reference_tip":
                row["peer"] = alias(peers, row["peer"], "peer")
            if row.get("event") == "coverage_failure":
                # The private journal retains diagnostic strings, including endpoint addresses.
                row["reason"] = "body coverage failure"
            if "reported_tips" in row:
                row["reported_tips"] = {
                    alias(peers, k, "peer"): {"hash": v["hash"], "height": v["height"]}
                    for k, v in row["reported_tips"].items()
                }
            if "directives" in row:
                row["directives"] = {
                    k: v
                    for k, v in row["directives"].items()
                    if k
                    in {
                        "last_applied",
                        "last_verified",
                        "ready",
                        "connected",
                        "genesis_verified",
                        "reported_sequence",
                        "last_peer_sync",
                        "peer_sync_required",
                    }
                }
            records.append(row)
    summary = audit(io.StringIO("\n".join(json.dumps(row) for row in records)))
    actions = [json.loads(l) for l in failover.read_text().splitlines()]
    return {"records": records, "failover": actions, "audit": summary}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("journal", type=Path)
    p.add_argument("failover", type=Path)
    p.add_argument("output", type=Path)
    a = p.parse_args()
    a.output.write_text(json.dumps(export(a.journal, a.failover), indent=2) + "\n")
