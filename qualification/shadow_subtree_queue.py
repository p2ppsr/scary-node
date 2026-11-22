"""Replay a fixed passive subtree trace using anonymized publishers and committed roots."""

import argparse
import collections
import gzip
import hashlib
import itertools
import json
import re
import struct
from pathlib import Path

from shadow import check_header, merkle

BASE = 6003
FOLLOW = 1000
RUN = 18
CAPACITY = 16
SOURCE = "5148c31ae620544c31a61b1710172c4bcc524c54"
RELEASE = "1c653db328c8c5af9b49dc9b1939fb320611097c507be7bac35449f668ced1ef"
START = b'"event":"started","network":"mainnet"'
HEX = rb"[0-9a-f]{64}"
ROOT_FRAME = (
    rb'\{"event":"subtree_notice","hash":"('
    + HEX
    + rb')","peer":"([1-9A-HJ-NP-Za-km-z]+)","queue_full":(true|false)\}'
)
FRAME = re.compile(ROOT_FRAME + rb'|\{"event":"body_observed","height":([0-9]+)\}')
DOMAIN = b"Scary Node public confirmed subtree comparison v1\x00"


def check(flag, message):
    if not flag:
        raise ValueError(message)


def commitment(display_hash):
    return hashlib.sha256(DOMAIN + bytes.fromhex(display_hash)).hexdigest()


def trace(path, run, limit):
    roots, peers, notices, errors = {}, {}, [], []
    starts = mixed = read_bytes = 0
    height = None
    with path.open("rb") as source:
        for raw in source:
            read_bytes += len(raw)
            if START in raw:
                starts += 1
            if starts < run:
                continue
            check(starts == run, "observer restarted before the fixed cutoff")
            # The enrolled process writes stdout and stderr to the same file. A complete
            # event can share a physical line with a diagnostic on either side.
            matches = list(FRAME.finditer(raw))
            check(
                raw.count(b'"event":"subtree_notice"')
                == sum(m[1] is not None for m in matches),
                "incomplete or unrecognized subtree frame",
            )
            if b"transaction subtree " in raw:
                check(b" unavailable:" in raw, "incomplete source error frame")
                errors.append(len(notices))
            for m in matches:
                if m[4]:
                    height = int(m[4])
                    continue
                ordinal = roots.setdefault(m[1], len(roots))
                author = peers.setdefault(m[2], len(peers))
                mixed += raw.strip() != m[0]
                notices.append([ordinal, author, int(m[3] == b"true"), height])
                if len(notices) == limit:
                    return {
                        "rows": notices,
                        "root_commitments": [commitment(h.decode()) for h in roots],
                        "source_errors_at_notice_count": errors,
                        "mixed_lines": mixed,
                        "bytes_scanned": read_bytes,
                    }
    raise ValueError(f"waiting for {limit} notices, observed {len(notices)}")


def describe(rows, errors):
    by_root = collections.defaultdict(list)
    by_height = collections.defaultdict(list)
    for h, p, full, height in rows:
        by_root[h].append((p, full))
        by_height[height].append(full)
    admitted = {h for h, v in by_root.items() if any(not q for p, q in v)}
    full = {h for h, v in by_root.items() if any(q for p, q in v)}
    runs = sorted(
        sum(1 for _ in group)
        for flag, group in itertools.groupby(rows, key=lambda row: row[2])
        if flag
    )
    return {
        "notices": len(rows),
        "full_notices": sum(row[2] for row in rows),
        "distinct_roots": len(by_root),
        "distinct_publishers": len({row[1] for row in rows}),
        "distinct_publisher_root_pairs": len({(r, p) for r, p, _, _ in rows}),
        "roots_seen_across_publishers": sum(
            len({p for p, q in e}) > 1 for e in by_root.values()
        ),
        "only_full_roots": len(full - admitted),
        "ever_nonfull_roots": len(admitted),
        "first_full_then_nonfull_roots": sum(
            e[0][1] and any(not q for p, q in e) for e in by_root.values()
        ),
        "logged_failed_sources": len(errors),
        "successful_root_lower_bound": max(
            0, len(admitted) - CAPACITY - 1 - len(errors)
        ),
        "full_run_count": len(runs),
        "full_run_lengths": {
            label: runs[int((len(runs) - 1) * q)] if runs else 0
            for label, q in (("median", 0.5), ("p95", 0.95), ("max", 1))
        },
        "by_last_body_callback": [
            {"height": h, "notices": len(v), "full": sum(v)}
            for h, v in by_height.items()
        ],
    }


def confirmed(rows, commitments, blocks):
    # These are complete published block subtrees reported by the stock Asset
    # service. Recreate every root from independently cached full block bytes.
    by_key = {key: i for i, key in enumerate(commitments)}
    selected = {}
    for ident, _, full, _ in rows:
        selected.setdefault(ident, set()).add(full)
    earliest = {}
    for ident, _, _, last_height in rows:
        earliest.setdefault(ident, last_height or 0)
    results = []
    previous = None
    check(
        [b["height"] for b in blocks] == list(range(976547, 976556)),
        "wrong covered range",
    )
    for b in blocks:
        raw = bytes.fromhex(b["header_hex"])
        check_header(
            {
                **b,
                "previous": raw[4:36][::-1].hex(),
                "bits": int.from_bytes(raw[72:76], "little"),
            }
        )
        check(
            previous is None or previous == raw[4:36][::-1].hex(),
            "broken confirmed block chain",
        )
        previous = b["hash"]
        ids = [bytes.fromhex(t)[::-1] for t in b["transaction_ids"]]
        check(
            bool(ids) and merkle(ids.copy()) == raw[36:68],
            "confirmed transaction commitment differs",
        )
        expected = (
            [] if len(ids) == 1 else [merkle([b"\xff" * 32, *ids[1:]])[::-1].hex()]
        )
        check(
            expected == b["reference_subtrees"],
            "stock partition differs from the reconstructed whole-block subtree",
        )
        header = b["reference_header"]
        rebuilt = (
            struct.pack("<I", header["version"])
            + bytes.fromhex(header["hash_prev_block"])[::-1]
            + bytes.fromhex(header["hash_merkle_root"])[::-1]
            + struct.pack(
                "<III", header["timestamp"], int(header["bits"], 16), header["nonce"]
            )
        )
        check(
            rebuilt == raw
            and b["asset_tx_count"] == len(ids)
            and b["asset_size"] == b["wire_bytes"],
            "stock metadata differs",
        )
        offers = [
            sorted(selected.get(by_key.get(commitment(h)), set())) for h in expected
        ]
        prefix_matches = []
        ordinary_ids = ids[1:]
        for n, payment in enumerate(ordinary_ids, 1):
            prefix = ordinary_ids[:n]
            for form, key in (
                ("single_payment", payment),
                ("ordinary_prefix", merkle(prefix.copy())),
                ("coinbase_placeholder_prefix", merkle([b"\xff" * 32, *prefix])),
            ):
                ordinal = by_key.get(commitment(key[::-1].hex()))
                if ordinal in selected:
                    prefix_matches.append(
                        {
                            "form": form,
                            "payments": 1 if form == "single_payment" else n,
                            "ending_payment": n,
                            "root": ordinal,
                            "offer_flags": sorted(selected[ordinal]),
                            "first_seen_through_height": earliest[ordinal],
                        }
                    )
        results.append(
            {
                "confirmed_content_matches": prefix_matches,
                "height": b["height"],
                "payments": len(ids) - 1,
                "confirmed_roots": len(expected),
                "matched_full_only": offers.count([1]),
                "matched_nonfull": sum(0 in o for o in offers),
                "unannounced_in_trace": offers.count([]),
            }
        )
    return results


def report(data):
    t = data["trace"]
    rows = t["rows"]
    keys = t["root_commitments"]
    first = rows[:BASE]
    follow = rows[BASE:]
    check(
        data["source"] == SOURCE
        and data["release"] == RELEASE
        and len(first) == BASE
        and len(follow) in (0, FOLLOW),
        "wrong source or window",
    )
    check(
        len(set(keys)) == len(keys) and all(len(bytes.fromhex(k)) == 32 for k in keys),
        "root commitments invalid",
    )
    seen, pubs = set(), set()
    for r, p, q, h in rows:
        check(r in seen or r == len(seen), "invalid root ordinal")
        check(p in pubs or p == len(pubs), "invalid publisher ordinal")
        check(
            q in (0, 1) and (h is None or isinstance(h, int)),
            "invalid scheduling observation",
        )
        seen.add(r)
        pubs.add(p)
    check(len(seen) == len(keys), "root table incomplete")
    snap = data.get("operator")
    if snap:
        n = snap["notices"]
        check(
            snap["release_sha256"] == RELEASE
            and 0 < n <= len(rows)
            and sum(v[2] for v in rows[:n]) == snap["full"],
            "operator snapshot differs from the event sequence",
        )
    errors = t["source_errors_at_notice_count"]
    base = describe(first, [n for n in errors if n < BASE])
    check(
        base["full_notices"] == 5581 and first[-1][-1] == 976554,
        "wrong original queue checkpoint",
    )
    first_full = {r for r, p, q, h in first if q} - {r for r, p, q, h in first if not q}
    retried = {r for r, p, q, h in follow if r in first_full}
    return {
        "original": base,
        "followup_notices": len(follow),
        "original_only_full_seen_again": len(retried),
        "original_only_full_nonfull_later": len(
            {r for r, p, q, h in follow if r in first_full and not q}
        ),
        "confirmed_blocks": confirmed(first, keys, data["confirmed"]),
        "all_observed_nonfull": describe(rows, errors)["ever_nonfull_roots"],
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mode", choices=["capture", "verify"])
    ap.add_argument("receipt", type=Path)
    ap.add_argument("--log", type=Path)
    ap.add_argument(
        "--confirmed",
        type=Path,
        help="confirmed-block records from shadow_subtree_blocks.py",
    )
    ap.add_argument(
        "--operator",
        type=Path,
        help="optional sanitized local status snapshot within trace",
    )
    ap.add_argument("--base-only", action="store_true")
    a = ap.parse_args()
    if a.mode == "capture":
        check(
            a.log and a.confirmed,
            "capture needs notice log and confirmed block records",
        )
        blocks = json.loads(a.confirmed.read_bytes())
        data = {
            "source": SOURCE,
            "release": RELEASE,
            "trace": trace(a.log, RUN, BASE if a.base_only else BASE + FOLLOW),
            "confirmed": [blocks[str(h)] for h in range(976547, 976556)],
        }
        if a.operator:
            snap = json.loads(a.operator.read_bytes())
            data["operator"] = {
                k: snap[k]
                for k in (
                    "observed_at_unix",
                    "release_sha256",
                    "height",
                    "coverage_start",
                    "coverage_end",
                    "data_peers",
                    "coverage_gaps",
                    "notices",
                    "full",
                )
            }
            fields = (
                "admitted",
                "awaiting_successor",
                "discarded_after_recheck",
                "duplicate",
                "ineligible",
                "promoted_after_successor",
            )
            data["operator"]["intake"] = {
                route: {k: int(snap["intake"][route][k]) for k in fields}
                for route in ("sv", "teranode_subtree")
            }
            data["operator"]["capacity"] = {
                k: int(snap["capacity"][k])
                for k in (
                    "block_bytes",
                    "transaction_bytes",
                    "pending_bytes",
                    "pending_transactions",
                )
            }
        print(json.dumps(report(data), indent=2))
        with a.receipt.open("xb") as f:
            f.write(
                gzip.compress(json.dumps(data, separators=(",", ":")).encode(), mtime=0)
            )
    else:
        data = json.loads(gzip.decompress(a.receipt.read_bytes()))
        print(json.dumps(report(data), indent=2))
