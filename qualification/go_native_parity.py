"""Compare the distributed and source-built Go native archives on the exact funded live corpus."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

from shadow_bdk import BDK, DISTRIBUTED_BDK, SV
from shadow_first_candidate import load, pack
from shadow_native import require
from verify_entrant_chronicle import extend, outputs
from verify_entrant_chronicle import verify as verify_corpus

ROLES = ("distributed", "source")


def sha(file):
    with file.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def reports(record):
    return record["opcodes"]["rows"] + record["signatures"]


def collect(corpus, manifest, folder, command=None):
    coins = outputs(corpus)
    rows = []
    for row in reports(corpus):
        raw = bytes.fromhex(row.get("raw_tx", row.get("raw")))
        ef, heights = extend(raw, coins)
        parent = row["observations"]["scary"]["parent_height"]
        actual = {}
        for role in ROLES:
            binary = folder / f"{role}-corpus"
            args = [
                *(command(binary) if command else [str(binary)]),
                ef.hex(),
                ",".join(map(str, heights)),
                str(parent),
                str(parent + 1),
            ]
            actual[role] = json.loads(subprocess.check_output(args, text=True))
        rows.append(
            {
                "txid": row["txid"],
                "parent": parent,
                "source_heights": heights,
                "results": actual,
            }
        )
    for role in ROLES:
        require(
            sha(folder / f"{role}-corpus")
            == manifest["variants"][role]["corpus_oracle_sha256"],
            "executed oracle differs from its build manifest",
        )
    return rows


def verify(record, corpus, manifest, corpus_sha, manifest_sha):
    live = verify_corpus(corpus)
    require(
        not live["disagreements"]
        and record["schema"] == 1
        and record["corpus_sha256"] == corpus_sha
        and record["manifest_sha256"] == manifest_sha,
        "corpus or native build receipt mismatch",
    )
    require(
        manifest["bdk_commit"] == BDK and manifest["sv_node_commit"] == SV,
        "unrecognized Go source checkouts",
    )
    cases = reports(corpus)
    coins = outputs(corpus)
    disagreement = []
    matched_rejections = 0
    for observed, wanted in zip(record["rows"], cases, strict=True):
        tx = bytes.fromhex(wanted.get("raw_tx", wanted.get("raw")))
        _, heights = extend(tx, coins)
        require(
            observed["txid"] == wanted["txid"]
            and observed["parent"] == wanted["observations"]["scary"]["parent_height"]
            and observed["source_heights"] == heights,
            "Go evidence differs from live funded case",
        )
        for role in ROLES:
            result = observed["results"][role]
            build = manifest["variants"][role]["linked"]
            pinned = BDK if role == "source" else DISTRIBUTED_BDK
            for key, pin in (("source", pinned), ("sv_source", SV)):
                report = result[key]
                require(
                    len(report) >= 7
                    and pin.startswith(report)
                    and report == build[key],
                    "running native revision differs",
                )
            require(
                (result["bdk_version"], result["sv_version"]) == ("1.4.0", "1.2.3"),
                "running native version differs",
            )
            for mode in ("policy", "consensus"):
                outcome = result[mode]
                require(type(outcome) is bool, "missing Go decision")
                if outcome != wanted["observations"]["sv"][mode]:
                    disagreement.append(
                        {"txid": wanted["txid"], "mode": mode, "variant": role}
                    )
                elif not outcome:
                    reason = result.get(mode + "_error")
                    expected_reason = wanted["observations"]["go"].get(mode + "_error")
                    require(
                        bool(reason) and reason == expected_reason,
                        "matching rejection has a different or absent script reason",
                    )
                    matched_rejections += 1
    return {
        "cases": len(cases),
        "contexts_per_variant": len(cases) * 2,
        "new_go_decisions": len(cases) * 4,
        "script_rejections_matching_original_go_reason": matched_rejections,
        "stock_policy_acceptances": live["stock_policy_acceptances"],
        "stock_consensus_acceptances": live["stock_consensus_acceptances"],
        "distributed_native": manifest["variants"]["distributed"]["linked"]["source"],
        "rebuilt_native": manifest["variants"]["source"]["linked"]["source"],
        "release_sha256": live["release_sha256"],
        "disagreements_with_live_stock": disagreement,
    }


def fresh_source_trial(fresh, original, manifest):
    summary = verify_corpus(fresh)
    require(
        not summary["disagreements"] and summary["all_policy_acceptances_mined"],
        "fresh four-way source build trial disagreed",
    )
    source = manifest["variants"]["source"]
    require(
        fresh["go_oracle_sha256"] == source["corpus_oracle_sha256"]
        and fresh["node_sha256"] == original["node_sha256"]
        and fresh["rust_oracle_sha256"] == original["rust_oracle_sha256"],
        "fresh trial ran a different executable",
    )
    for a, b in zip(reports(fresh), reports(original), strict=True):
        require(
            all(a.get(k) == b.get(k) for k in ("case", "family", "index")),
            "fresh case sequence differs",
        )
        go = a["observations"]["go"]
        for k in ("source", "sv_source", "bdk_version", "sv_version"):
            require(go[k] == source["linked"][k], "fresh Go runtime identity differs")
        for mode in ("policy", "consensus"):
            require(
                a["observations"]["sv"][mode] == b["observations"]["sv"][mode],
                "fresh SV decision differs from original funded trial",
            )
    return summary


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("corpus", type=Path)
    p.add_argument("manifest", type=Path)
    p.add_argument("receipt", type=Path)
    p.add_argument(
        "--capture", type=Path, help="directory containing the two Go corpus binaries"
    )
    p.add_argument(
        "--fresh-source-corpus",
        type=Path,
        help="also verify a second complete live trial with the rebuilt Go native archive",
    )
    a = p.parse_args()
    corpus = load(a.corpus)
    m = json.loads(a.manifest.read_text())
    cs = sha(a.corpus)
    ms = sha(a.manifest)
    if a.capture:
        record = {
            "schema": 1,
            "corpus_sha256": cs,
            "manifest_sha256": ms,
            "rows": collect(corpus, m, a.capture.resolve()),
        }
        a.receipt.write_bytes(pack(record))
    else:
        record = load(a.receipt)
    outcome = verify(record, corpus, m, cs, ms)
    if a.fresh_source_corpus:
        outcome["fresh_source_trial"] = fresh_source_trial(
            load(a.fresh_source_corpus), corpus, m
        )
    print(json.dumps(outcome, indent=2))
    raise SystemExit(1 if outcome["disagreements_with_live_stock"] else 0)
