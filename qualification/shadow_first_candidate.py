"""Verify the first prospective unsigned shadow candidate, its actual successor and native fee evidence."""

import argparse
import fcntl
import gzip
import json
import os
import struct
import time
from functools import partial
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from shadow import check_template
from shadow_checkpoint import SCALE, atomic_write, check_coinbase, next_bits, target
from shadow_native import (
    checked_header,
    inputs,
    parse_tx,
    proofs,
    require,
    txid,
    unpack_state,
    verify_fees,
    verify_funding,
    wire_body,
)


def pack(x):
    return gzip.compress(
        json.dumps(x, sort_keys=True, separators=(",", ":")).encode(), mtime=0
    )


def load(path):
    return json.loads(gzip.decompress(path.read_bytes()))


def journal_rows(path):
    with path.open() as f:
        return [
            r
            for line in f
            if line.endswith("\n")
            and (r := json.loads(line)).get("event", "tip") == "tip"
        ]


def first_decision(rows):
    return next(
        (
            i
            for i, r in enumerate(rows)
            if (r.get("unsigned_candidate") or {}).get("transactions")
        ),
        None,
    )


def status(url, token):
    if not url:
        return None
    parsed = urlsplit(url)
    require(
        parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "localhost", "::1"},
        "clock status must use the local observer",
    )
    req = Request(
        url.rstrip("/") + "/status",
        headers={"Authorization": "Bearer " + token.read_text().strip()},
    )
    with build_opener(NoRedirect()).open(req, timeout=5) as resp:
        r = json.load(resp)
    require(r["network"] == "mainnet", "expected mainnet status")
    m = r["mining"]
    return {
        "at": int(time.time()),
        "height": m["height"],
        "hash": m["tip_hash"],
        "clock_step": m["clock_step_seconds"],
        "clock_step_limit": m["clock_step_limit_seconds"],
        "peer_clock": m["peer_clock"],
        "release": r["release"]["executable_sha256"],
    }


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def cached_path(work, block):
    require(
        len(bytes.fromhex(block)) == 32 and block == bytes.fromhex(block).hex(),
        "invalid block key",
    )
    return work / "bodies" / (block + ".gz")


def save_bodies(work, s, budget):
    for applied in s["chain"]["applied"]:
        stored = applied["raw"]
        header = stored[:80] if isinstance(stored, bytes) else stored[0][:80]
        dest = cached_path(work, txid(header))
        if dest.exists():
            continue
        size = len(stored) if isinstance(stored, bytes) else sum(map(len, stored))
        if size > budget:
            continue
        blob = gzip.compress(wire_body(applied), compresslevel=1, mtime=0)
        if len(blob) > budget:
            continue
        cached = sorted(
            (work / "bodies").glob("*.gz"), key=lambda f: f.stat().st_mtime_ns
        )
        size = len(blob) + sum(f.stat().st_size for f in cached)
        for old in cached:
            if size <= budget:
                break
            size -= old.stat().st_size
            old.unlink()
        atomic_write(dest, blob)


def capture(s, row, clock):
    job = row["unsigned_candidate"]
    require(
        row["ready"]
        and job
        and row["pending"] == len(job["transactions"])
        and job["transactions"],
        "missing eligible recorded candidate",
    )
    check_template(row)
    require(
        s["height"] == row["height"] and txid(s["headers"][-1]) == row["hash"],
        "checkpoint has another parent",
    )
    require(
        s["applied_sequence"]
        == row["directives"]["last_applied"]
        == row["directives"]["last_verified"]
        and row["directives"]["ready"],
        "signed state mismatch",
    )
    active = {
        height: bytes(h)[::-1].hex() for h, height in s["active_since_entry"].items()
    }
    previous = {}
    raws = []
    fees = []
    earlier = set()
    for tid in job["transactions"]:
        entry = s["pending"].get(tuple(bytes.fromhex(tid)[::-1]))
        require(
            entry is not None and txid(entry["raw"]) == tid,
            "recorded payment bytes absent from this parent checkpoint",
        )
        raw = entry["raw"]
        points, outs = inputs(raw)
        require(
            [(bytes(h)[::-1].hex(), n) for h, n in entry["inputs"]] == points,
            "admitted inputs differ from native bytes",
        )
        for p in points:
            if p in earlier:
                continue
            coin = s["coins"].get((tuple(bytes.fromhex(p[0])[::-1]), p[1]))
            require(
                coin is not None and coin["height"] < s["height"],
                "input is missing witnessed creation or its successor",
            )
            previous[f"{p[0]}:{p[1]}"] = {
                "value": coin["value"],
                "script": coin["script"].hex(),
                "height": coin["height"],
                "block": active[coin["height"]],
                "coinbase": coin["coinbase"],
                "confiscation": coin["confiscation"],
            }
        earlier.update((tid, i) for i in range(len(outs)))
        raws.append(raw.hex())
        fees.append(entry["fee"])
    selected = {
        k: row[k]
        for k in (
            "height",
            "hash",
            "previous",
            "header_hex",
            "bits",
            "ready",
            "coverage",
            "observed_at",
            "pending",
            "expected_next_bits",
            "unsigned_candidate",
        )
    }
    selected["directives"] = {
        k: row["directives"][k]
        for k in (
            "ready",
            "last_applied",
            "last_verified",
            "reported_sequence",
            "last_peer_sync",
            "connected",
            "genesis_verified",
        )
    }
    return {
        "schema": 1,
        "checkpoint_payload_sha256": s["_checkpoint_sha256"],
        "decision": selected,
        "headers": [h.hex() for h in s["headers"]],
        "clock": clock,
        "raw_transactions": raws,
        "witnessed": previous,
        "admitted_fees": fees,
        "funding_proofs": {},
    }


def fetch_block(h, bases, limit, decode=None):
    for base in bases:
        parsed = urlsplit(base)
        require(
            parsed.scheme in {"https", "http"}
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment,
            "unusable approved Asset origin",
        )
        try:
            req = Request(
                base.rstrip("/") + "/block_legacy/" + h + "?wire=1",
                headers={
                    "User-Agent": "scary-node-passive-proof/1",
                    "Accept": "application/octet-stream",
                },
            )
            with build_opener(NoRedirect()).open(req, timeout=20) as f:
                declared = f.headers.get("Content-Length")
                if declared and int(declared) > limit:
                    continue
                raw = bytearray()
                while len(raw) <= limit:
                    part = f.read(min(1 << 20, limit + 1 - len(raw)))
                    if not part:
                        break
                    raw.extend(part)
            if len(raw) <= limit and txid(raw[:80]) == h:
                return decode(raw) if decode else raw
        except (OSError, ValueError, struct.error, IndexError):
            continue
    return None


def complete_sources(record, work, bases, limit):
    groups = {}
    for key, p in record["witnessed"].items():
        group = groups.setdefault(p["block"], (p["height"], set()))
        group[1].add(key.split(":")[0])
    for h, (height, ids) in groups.items():
        if h in record["funding_proofs"]:
            continue
        file = cached_path(work, h)
        inspect = partial(proofs, height=height, expected=h, wanted=ids)
        proof = (
            inspect(gzip.decompress(file.read_bytes()))
            if file.exists()
            else fetch_block(h, bases, limit, inspect)
        )
        if proof is None:
            continue
        require(
            set(proof["transactions"]) == ids,
            "funding transaction missing from proved native block",
        )
        record["funding_proofs"][h] = proof


def assess(rec):
    d = rec["decision"]
    j = d["unsigned_candidate"]
    check_template(d)
    require(
        rec["schema"] == 1
        and d["ready"]
        and d["coverage"] == 147
        and len(rec["headers"]) == 147
        and j["height"] == d["height"] + 1,
        "candidate lacks complete contemporary DAA evidence",
    )
    chain = {}
    recent = []
    work = 0
    previous = None
    for n, encoded in enumerate(rec["headers"], d["height"] - 146):
        raw = bytes.fromhex(encoded)
        h = checked_header(raw, n)
        if previous:
            require(h["previous"] == previous, "gap in native difficulty evidence")
        stamp = int.from_bytes(raw[68:72], "little")
        work += SCALE // (target(h["bits"]) + 1)
        recent.append((stamp, work))
        chain[n] = h["hash"]
        previous = h["hash"]
    require(
        previous == d["hash"] and rec["headers"][-1] == d["header_hex"],
        "native difficulty window does not end at selected parent",
    )
    bits = next_bits(recent)
    require(bits == j["bits"] == d["expected_next_bits"], "independent CW-144 differs")
    check_coinbase(j, recent, d["observed_at"])
    require(
        bytes.fromhex(j["header_hex"])[76:] == bytes(4),
        "unsigned candidate nonce differs",
    )
    directive = d["directives"]
    require(
        directive["ready"]
        and directive["connected"]
        and directive["genesis_verified"]
        and directive["last_applied"]
        == directive["last_verified"]
        == directive["reported_sequence"]
        == directive["last_peer_sync"]["sequence"]
        and 0 <= d["observed_at"] - directive["last_peer_sync"]["unix_s"] <= 1200,
        "candidate lacks fresh verified directives",
    )
    ids = [txid(bytes.fromhex(w)) for w in rec["raw_transactions"]]
    require(
        ids == j["transactions"] and len(set(ids)) == len(ids) == d["pending"],
        "candidate's raw payments do not match its committed order",
    )
    fees = verify_fees(rec["raw_transactions"], rec["witnessed"], rec["admitted_fees"])
    coinbase = bytes.fromhex(j["coinbase"])
    _, payout, end = parse_tx(coinbase, 0)
    require(end == len(coinbase) and len(payout) == 1, "unexpected coinbase layout")
    subsidy = (50 * 10**8) >> (j["height"] // 210000)
    require(
        payout[0][0] == subsidy + sum(fees),
        "coinbase does not collect subsidy and independently calculated fees",
    )
    # Older sources must link all the way to the first of the 147 committed headers.
    child = bytes.fromhex(rec["headers"][0])[4:36][::-1].hex()
    for height, encoded in enumerate(reversed(rec.get("earlier_headers", [])), 1):
        n = d["height"] - 146 - height
        h = checked_header(bytes.fromhex(encoded), n)
        require(h["hash"] == child, "older funding header chain is disconnected")
        chain[n] = child
        child = h["previous"]
    wanted = {}
    for k, p in rec["witnessed"].items():
        wanted.setdefault(p["block"], []).append((k, p))
    proven = 0
    proofs_by_hash = rec["funding_proofs"]
    for h, group in proofs_by_hash.items():
        found = verify_funding(group, h)
        require(h in wanted, "unrelated native funding block")
        for k, p in wanted[h]:
            tid, n = k.split(":")
            require(
                p["height"] == group["height"]
                and p["height"] < d["height"]
                and tid in found
                and found[tid][int(n)] == (p["value"], bytes.fromhex(p["script"])),
                "native funding differs from witnessed script or amount",
            )
            coinbase = group["transactions"][tid]["index"] == 0
            confiscation = found[tid][0][1].startswith(b"\x00\x6a\x04cftx")
            require(
                p["coinbase"] == coinbase and p["confiscation"] == confiscation,
                "witnessed output class differs from its native source",
            )
            age = j["height"] - p["height"]
            require(
                (not coinbase or age >= 100) and (not confiscation or age >= 1000),
                "candidate spends an immature native output",
            )
            if p["height"] in chain:
                require(
                    chain[p["height"]] == h,
                    "funding is outside the selected header chain",
                )
                proven += 1
    successor = rec.get("successor")
    matched = False
    if successor:
        raw = bytes.fromhex(successor["header_hex"])
        h = checked_header(raw, successor["height"])
        timestamp = int.from_bytes(raw[68:72], "little")
        median = sorted(t for t, _ in recent[-11:])[5]
        require(
            median < timestamp <= successor["observed_at"] + 7200,
            "successor time outside observed bounds",
        )
        matched = (
            h["previous"] == d["hash"]
            and h["hash"] == successor["hash"]
            and successor["height"] == j["height"]
            and h["bits"] == bits
        )
        require(
            matched,
            "actual next observed header contradicts candidate parent, height or difficulty",
        )
    c = rec.get("clock")
    clock_ok = False
    if c:
        pc = c["peer_clock"]
        clock_ok = (
            c["hash"] == d["hash"]
            and c["height"] == d["height"]
            and abs(c["at"] - d["observed_at"]) <= 120
            and pc["connected_network_groups"] >= pc["minimum_groups"] >= 3
            and not pc["disagreeing_samples"]
            and pc["offset_from_wall_seconds"] is not None
            and abs(pc["offset_from_wall_seconds"]) <= pc["hold_threshold_seconds"]
            and abs(c["clock_step"]) <= c["clock_step_limit"]
        )
        if clock_ok:
            stamp = int.from_bytes(bytes.fromhex(j["header_hex"])[68:72], "little")
            adjusted = d["observed_at"] + min(0, pc["offset_from_wall_seconds"])
            require(
                stamp <= adjusted + 7200,
                "candidate time exceeds corroborated future limit",
            )
    total = len(rec["witnessed"])
    return {
        "parent_height": d["height"],
        "parent_hash": d["hash"],
        "candidate_transactions": len(ids),
        "cw144_bits": bits,
        "candidate_fee_satoshis": sum(fees),
        "subsidy_satoshis": subsidy,
        "coinbase_satoshis": payout[0][0],
        "witnessed_external_inputs": total,
        "external_inputs_with_native_inclusion": proven,
        "in_template_inputs": sum(
            len(inputs(bytes.fromhex(w))[0]) for w in rec["raw_transactions"]
        )
        - total,
        "actual_successor_matches": matched,
        "prospective_peer_clock_confirmed": clock_ok,
        "signed_sequence": directive["last_applied"],
        "complete": matched and clock_ok and proven == total,
    }


def snapshot_for(state, work, expected):
    """Preserve a committed inode even if the observer renames its next checkpoint."""
    dest = work / "snapshots" / (cached_path(work, expected).stem + ".state")
    if not dest.exists():
        os.link(state, dest)
    with dest.open("rb") as handle:
        s = unpack_state(handle)
    if txid(s["headers"][-1]) != expected:
        dest.unlink()
        return None
    return s


def save_clock(args, work, expected):
    file = work / "clocks" / (expected + ".json")
    if file.exists():
        return json.loads(file.read_text())
    try:
        c = status(args.status_url, args.token_file)
    except (OSError, ValueError):
        return None
    if c and c["hash"] == expected:
        atomic_write(file, (json.dumps(c) + "\n").encode())
        return c
    return None


def earlier_headers(data, rows, work, bases, limit):
    if not data["witnessed"]:
        return []
    first_height = data["decision"]["height"] - 146
    min_height = min(p["height"] for p in data["witnessed"].values())
    by_hash = {r["hash"]: r for r in rows if r.get("header_hex")}
    child = bytes.fromhex(data["headers"][0])[4:36][::-1].hex()
    result = []
    for height in range(first_height - 1, min_height - 1, -1):
        row = by_hash.get(child)
        if row and row["height"] == height:
            encoded = row["header_hex"]
        else:
            file = cached_path(work, child)
            raw = (
                gzip.decompress(file.read_bytes())
                if file.exists()
                else fetch_block(child, bases, limit)
            )
            if raw is None:
                break
            encoded = raw[:80].hex()
        h = checked_header(bytes.fromhex(encoded), height)
        require(h["hash"] == child, "historical source header differs from its child")
        result.append(encoded)
        child = h["previous"]
    return result[::-1]


def step(args, work, bases, prior):
    rows = journal_rows(args.journal)
    if not rows:
        return None, prior
    tip = rows[-1]
    idx = first_decision(rows)
    path = work / "first.json.gz"
    if not path.exists():
        needed = rows[idx] if idx is not None else tip
        h = needed["hash"]
        # Capture the clock and the signed bytes independently. A temporary
        # clock API outage must not discard a durably recorded transaction set.
        clock = save_clock(args, work, h)
        if idx is not None or h != prior:
            s = snapshot_for(args.state, work, h)
            if s is None:
                if idx is not None and tip["hash"] != h:
                    return {
                        "complete": False,
                        "parent_height": needed["height"],
                        "missing": "first candidate checkpoint unavailable",
                    }, prior
                return None, prior
            if idx is not None:
                data = capture(s, needed, clock)
                atomic_write(path, pack(data))
            save_bodies(work, s, args.cache_bytes)
            # Keep short-lived recovery snapshots solely in this reader's spool.
            preserve = {r["hash"] for r in rows[-4:]} | {h}
            for f in (work / "snapshots").glob("*.state"):
                if f.stem not in preserve:
                    f.unlink()
            prior = h
    if not path.exists():
        return None, prior
    data = load(path)
    first = rows[idx]
    require(
        data["decision"]["hash"] == first["hash"],
        "earliest prospective candidate changed",
    )
    if not data["clock"]:
        data["clock"] = save_clock(args, work, first["hash"])
    if len(rows) > idx + 1:
        nxt = rows[idx + 1]
        data["successor"] = {
            k: nxt[k] for k in ("height", "hash", "header_hex", "observed_at")
        }
    # Preserve prospective evidence before potentially unavailable older Asset reads.
    atomic_write(path, pack(data))
    data["earlier_headers"] = earlier_headers(
        data, rows, work, bases, args.max_block_bytes
    )
    complete_sources(data, work, bases, args.max_block_bytes)
    atomic_write(path, pack(data))
    return assess(data), prior


def main():
    p = argparse.ArgumentParser(description=__doc__)
    cmd = p.add_subparsers(dest="command", required=True)
    check = cmd.add_parser("verify")
    check.add_argument("receipt", type=Path)
    observe = cmd.add_parser("follow")
    observe.add_argument("journal", type=Path)
    observe.add_argument("state", type=Path)
    observe.add_argument("work", type=Path)
    observe.add_argument("--status-url")
    observe.add_argument("--token-file", type=Path)
    observe.add_argument(
        "--asset-list", type=Path, help="approved mainnet Asset base URLs, one per line"
    )
    observe.add_argument("--max-block-bytes", type=int, default=1 << 30)
    observe.add_argument("--cache-bytes", type=int, default=2 << 30)
    a = p.parse_args()
    if a.command == "verify":
        result = assess(load(a.receipt))
        print(json.dumps(result, indent=2))
        return 0 if result["complete"] else 1
    if a.max_block_bytes <= 0 or a.cache_bytes <= 0:
        p.error("body limits must be positive")
    if a.status_url and not a.token_file:
        p.error("clock capture requires the local token file")
    os.umask(0o077)
    a.work.mkdir(parents=True, exist_ok=True)
    for name in ("bodies", "clocks", "snapshots"):
        (a.work / name).mkdir(exist_ok=True)
    with (a.work / "reader.lock").open("a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return follow(a)


def follow(a):
    bases = (
        [x.strip() for x in a.asset_list.read_text().splitlines() if x.strip()]
        if a.asset_list
        else []
    )
    prior = last_rev = None
    retry_at = 0
    while True:
        try:
            rev = a.journal.stat().st_mtime_ns
            now = time.monotonic()
            if rev != last_rev or now >= retry_at:
                # New tip decisions are examined immediately during warm-up.
                # Once the raw candidate is held, throttle repeat reference work.
                recorded = (a.work / "first.json.gz").exists()
                if not recorded or now >= retry_at:
                    result, prior = step(a, a.work, bases, prior)
                    retry_at = now + (60 if recorded or result else 5)
                    last_rev = rev
                    if result:
                        atomic_write(
                            a.work / "summary.json",
                            (json.dumps(result, indent=2) + "\n").encode(),
                        )
                        if result["complete"]:
                            return 0
        except (OSError, URLError) as error:
            print(type(error).__name__, "unable to read current evidence", flush=True)
            time.sleep(5)
        time.sleep(0.25)


if __name__ == "__main__":
    raise SystemExit(main())
