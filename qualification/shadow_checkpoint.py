"""Export and independently replay a passive mainnet checkpoint, including per-header CW-144."""

import argparse
import gzip
import hashlib
import json
import os
import tempfile
import time
from collections import Counter, defaultdict, deque
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from shadow import audit, check_header
from shadow_export import export

SCALE = 1 << 256
LIMIT = 0xFFFF << 208
TARGET_BLOCKS = 1008
MAX_PEER_AGE = 1200
FINAL_CONTRACT = "chronicle-mainnet-shadow-v1"


def target(bits):
    exponent, coefficient = bits >> 24, bits & 0x7FFFFF
    if not 0 < bits <= 0xFFFFFFFF or bits & 0x800000 or exponent > 34:
        raise ValueError("invalid compact target")
    shift = 8 * (exponent - 3)
    n = coefficient << shift if shift >= 0 else coefficient >> -shift
    if not 0 < n <= LIMIT:
        raise ValueError("target outside mainnet range")
    return n


def compact(n):
    size = (n.bit_length() + 7) // 8
    shift = 8 * (size - 3)
    mantissa = n >> shift if shift >= 0 else n << -shift
    if mantissa & 0x800000:
        size += 1
        mantissa >>= 8
    return size << 24 | mantissa


def next_bits(headers):
    """Each entry holds a wire timestamp and relative cumulative chainwork, oldest first."""
    if len(headers) != 147:
        raise ValueError("CW-144 needs 147 headers")

    def suitable(a, b, c):
        if a[0] > c[0]:
            a, c = c, a
        if a[0] > b[0]:
            a, b = b, a
        if b[0] > c[0]:
            b, c = c, b
        return b

    start = suitable(headers[0], headers[1], headers[2])
    end = suitable(headers[-3], headers[-2], headers[-1])
    elapsed = max(72 * 600, min(288 * 600, end[0] - start[0]))
    work = (end[1] - start[1]) * 600 // elapsed
    if not 0 < work < SCALE:
        raise ValueError("CW-144 work outside uint256 range")
    return compact(min(LIMIT, SCALE // work - 1))


def utc(time):
    return datetime.fromtimestamp(time, UTC).isoformat().replace("+00:00", "Z")


def current_run(records):
    tips = []
    for row in records:
        if row.get("event", "tip") != "tip":
            continue
        if tips and (
            tips[-1]["height"] + 1 != row["height"]
            or tips[-1]["hash"] != row["previous"]
        ):
            tips = []
        tips.append(row)
    if not tips:
        raise ValueError("no observed tip")
    return tips


def timed_reports(records):
    reports = defaultdict(list)
    for row in records:
        if row.get("event") == "reference_tip":
            reports[(row["height"], row["hash"])].append(
                (row["peer"], row["observed_at"])
            )
    return reports


def separate_report(row, reports):
    peers = {
        peer
        for peer, at in reports[(row["height"], row["hash"])]
        if abs(at - row["observed_at"]) <= 7200
    }
    delivery = row.get("delivery_source", "")
    # SV delivery and status use the same run-local peer aliases. An Asset URL
    # has a separate alias namespace; one reporter might own that URL as well.
    if delivery.startswith("sv:"):
        return bool(peers - {delivery.removeprefix("sv:")})
    return len(peers) >= 2


class InactiveReference(Exception):
    """An independently queried hash is currently outside the provider's active chain."""


def reference_hashes(records):
    tips = current_run(records)
    reports = timed_reports(records)
    wanted = (
        [tips[0]["previous"]]
        + [
            r["hash"]
            for r in tips
            if not r.get("header_hex") or not separate_report(r, reports)
        ]
        + [tips[-1]["hash"]]
    )
    return dict.fromkeys(wanted)


def public_reference(records):
    # The API supplies retrospective readbacks. Timed P2P reports are kept separately.
    rows = {}
    for block_hash in reference_hashes(records):
        if len(block_hash) != 64 or bytes.fromhex(block_hash).hex() != block_hash:
            raise ValueError("invalid journal block hash")
        time.sleep(1)
        request = Request(
            "https://api.whatsonchain.com/v1/bsv/main/block/" + block_hash + "/header",
            headers={"User-Agent": "scary-node-mainnet-qualification/1"},
        )
        for attempt in range(4):
            try:
                with urlopen(request, timeout=25) as response:
                    row = json.loads(response.read(128 * 1024))
                break
            except HTTPError as error:
                if error.code != 429 or attempt == 3:
                    raise
                time.sleep(int(error.headers.get("Retry-After", "5")))
        wire = (
            int(row["versionHex"], 16).to_bytes(4, "little")
            + bytes.fromhex(row["previousblockhash"])[::-1]
            + bytes.fromhex(row["merkleroot"])[::-1]
            + row["time"].to_bytes(4, "little")
            + int(row["bits"], 16).to_bytes(4, "little")
            + row["nonce"].to_bytes(4, "little")
        )
        if row["hash"] != block_hash:
            raise ValueError("reference served a different header")
        if row["confirmations"] < 1:
            raise InactiveReference(
                "queried hash is outside the reference active chain"
            )
        rows[block_hash] = {
            "height": row["height"],
            "hash": block_hash,
            "previous": row["previousblockhash"],
            "bits": int(row["bits"], 16),
            "header_hex": wire.hex(),
            "chainwork": row["chainwork"],
            "confirmations": row["confirmations"],
            "read_at_utc": utc(time.time()),
        }
    return {"provider": "WhatsOnChain mainnet block header API", "by_hash": rows}


def check_reference(records, reference, work):
    tips = current_run(records)
    refs = reference["by_hash"]
    for block_hash, ref in refs.items():
        if ref["hash"] != block_hash or ref["confirmations"] < 1:
            raise ValueError("reference readback is for a different or inactive block")
        check_header(ref)
    anchor = refs[tips[0]["previous"]]
    if (
        anchor["height"] + 1 != tips[0]["height"]
        or anchor["hash"] != tips[0]["previous"]
    ):
        raise ValueError("reference work anchor differs")
    total = int(anchor["chainwork"], 16)
    supplemented = 0
    for row in tips:
        total += SCALE // (target(row["bits"]) + 1)
        if row["hash"] not in refs and row.get("header_hex"):
            continue
        ref = refs[row["hash"]]
        for key in ["height", "hash", "previous", "bits"]:
            if row[key] != ref[key]:
                raise ValueError(f"height {row['height']}: reference header differs")
        if row.get("header_hex") and row["header_hex"] != ref["header_hex"]:
            raise ValueError(f"height {row['height']}: wire and reference bytes differ")
        supplemented += not bool(row.get("header_hex"))
        if total != int(ref["chainwork"], 16):
            raise ValueError(
                f"height {row['height']}: absolute accumulated work differs"
            )
    last = refs[tips[-1]["hash"]]
    if total - int(anchor["chainwork"], 16) != work:
        raise ValueError("external chainwork delta differs from journal")
    return {
        "provider": reference["provider"],
        "anchor_height": anchor["height"],
        "anchor_hash": anchor["hash"],
        "tip_height": last["height"],
        "tip_hash": last["hash"],
        "tip_chainwork": last["chainwork"],
        "reference_tip_read_at_utc": last["read_at_utc"],
        "retrospective_headers_supplemented": supplemented,
        "absolute_chainwork_delta_matched": True,
    }


def check_coinbase(job, previous_headers, observed_at):
    raw = bytes.fromhex(job["coinbase"])
    header = bytes.fromhex(job["header_hex"])
    script_size = raw[41] if len(raw) > 41 else 0
    height = job["height"]
    encoded = height.to_bytes((height.bit_length() + 7) // 8, "little")
    if encoded[-1] & 0x80:
        encoded += b"\x00"
    prefix = bytes([len(encoded)]) + encoded
    if (
        raw[4:41] != b"\x01" + bytes(32) + b"\xff" * 4
        or not 2 <= script_size <= 100
        or len(raw) < 42 + script_size + 4
        or not raw[42 : 42 + script_size].startswith(prefix)
    ):
        raise ValueError("unsigned coinbase has invalid prevout or height")
    stamp = int.from_bytes(header[68:72], "little")
    if len(previous_headers) >= 11:
        median = sorted(entry[0] for entry in list(previous_headers)[-11:])[5]
        if stamp <= median:
            raise ValueError("unsigned timestamp precedes median")
    if stamp > observed_at + 7200:
        raise ValueError("unsigned timestamp exceeds future limit")


def verified_failover(records, receipt):
    # A new attempt starts at its restriction. Preserve unsuccessful and
    # partial attempts in the archive without joining stages across attempts.
    starts = [
        i
        for i, row in enumerate(receipt)
        if row.get("event") == "restrict_body_transport"
    ]
    ends = starts[1:] + [len(receipt)]
    return any(
        verified_attempt(records, receipt[i:end]) for i, end in zip(starts, ends)
    )


def verified_attempt(records, receipt):
    # The restricted and restored starts must surround the specific direct successor.
    if [r.get("event") for r in receipt] != [
        "restrict_body_transport",
        "sv_body_received",
        "restore_all_body_transports",
    ]:
        return False
    restricted, received, restored = receipt
    if not (
        received.get("contiguous_failover")
        and received.get("restored")
        and restricted["unix_s"] <= received["unix_s"] <= restored["unix_s"]
        and received["height"] == restricted["height"] + 1
    ):
        return False
    stages = [
        lambda r: (
            r.get("event", "tip") == "tip"
            and r["hash"] == restricted["hash"]
            and r["height"] == restricted["height"]
            and r["observed_at"] <= restricted["unix_s"]
            and r.get("receipt_trigger_hash") == r["hash"]
            and r.get("delivery_source", "").startswith("asset:")
        ),
        lambda r: (
            r.get("event") == "start"
            and r.get("body_transport") == "sv_only"
            and restricted["unix_s"] <= r["at"] <= received["unix_s"]
            and r.get("restored_tip") == restricted["hash"]
        ),
        lambda r: (
            r.get("event") == "tip"
            and r["previous"] == restricted["hash"]
            and r["hash"] == received["hash"]
            and r["height"] == received["height"]
            and restricted["unix_s"] <= r["observed_at"] <= received["unix_s"]
            and r.get("receipt_trigger_hash") == r["hash"]
            and r.get("delivery_source", "").startswith("sv:")
        ),
        lambda r: (
            r.get("event") == "start"
            and r.get("body_transport") == "all"
            and r["at"] >= restored["unix_s"]
            and r.get("restored_tip") == received["hash"]
        ),
    ]
    transitions = deque(maxlen=len(stages))
    for row in records:
        if row.get("event", "tip") not in ("start", "tip"):
            continue
        transitions.append(row)
        if len(transitions) == len(stages) and all(
            check(event) for check, event in zip(stages, transitions)
        ):
            return True
    return False


def reconcile(snapshot):
    records = snapshot["records"]
    baseline = audit(json.dumps(row) for row in records)
    if baseline != snapshot["audit"]:
        raise ValueError("embedded commitment audit differs from replay")

    reports = timed_reports(records)
    recent = deque(maxlen=147)
    reasons, match_histogram = Counter(), Counter()
    previous = forecast = first = None
    expected_count = observed_count = signed_ready = mtp_checks = ready_assemblies = 0
    current_start = current_work = 0
    latest_signed = None
    coverage_recoveries = []
    gaps = []
    restored_mismatches = []
    for i, row in enumerate(records, 1):
        event = row.get("event", "tip")
        if event == "coverage_failure":
            gaps.append(row)
        if (
            event == "start"
            and previous
            and row.get("restored_tip") != previous["hash"]
        ):
            restored_mismatches.append(i)
        if event != "tip":
            continue
        first = first or row
        direct = previous and (
            row["height"] == previous["height"] + 1
            and row["previous"] == previous["hash"]
        )
        if not direct:
            recent.clear()
            current_start, current_work, forecast = row["height"], 0, None
        if forecast is not None:
            if row["bits"] != forecast:
                raise ValueError(
                    f"height {row['height']}: measured CW-144 differs from received bits"
                )
            observed_count += 1
        if gaps:
            coverage_recoveries.extend(
                {
                    "retained_height": gap["retained_height"],
                    "next_observed_height": row["height"],
                    "elapsed_seconds": row["observed_at"] - gap["observed_at"],
                    "direct_successor": bool(
                        direct and previous["height"] == gap["retained_height"]
                    ),
                }
                for gap in gaps
            )
            gaps.clear()
        current_work += SCALE // (target(row["bits"]) + 1)
        if row.get("header_hex"):
            wire = bytes.fromhex(row["header_hex"])
            stamp = int.from_bytes(wire[68:72], "little")
            if len(recent) >= 11:
                median = sorted(entry[0] for entry in list(recent)[-11:])[5]
                if stamp <= median:
                    raise ValueError(
                        f"height {row['height']}: header time precedes median"
                    )
                mtp_checks += 1
            recent.append((stamp, current_work))
        else:
            recent.clear()
        forecast = next_bits(recent) if len(recent) == 147 else None
        if forecast is not None:
            if forecast != row["expected_next_bits"]:
                raise ValueError(
                    f"height {row['height']}: node difficulty prediction differs from CW-144"
                )
            expected_count += 1
        directive = row["directives"]
        if directive["ready"]:
            sequence = directive["last_applied"]
            sync = directive.get("last_peer_sync") or {}
            if not (
                directive.get("connected")
                and directive.get("genesis_verified")
                and directive.get("peer_sync_required")
                and sequence == directive["last_verified"]
                and sequence == directive["reported_sequence"]
                and sequence == sync.get("sequence")
                and 0 <= row["observed_at"] - sync["unix_s"] <= MAX_PEER_AGE
            ):
                raise ValueError(
                    f"height {row['height']}: directive readiness lacks a fresh signed prefix"
                )
            signed_ready += 1
            latest_signed = sequence
        if not 1 <= row["coverage"] <= 147:
            raise ValueError(f"height {row['height']}: invalid body coverage window")
        if row["ready"] and (
            not directive["ready"]
            or row["coverage"] < 147
            or row["expected_next_bits"] is None
        ):
            raise ValueError(
                f"height {row['height']}: readiness contradicts observed state"
            )
        present = row.get("unsigned_candidate") is not None
        if present:
            if (
                not row["ready"]
                or row.get("coverage", 0) < 147
                or row["expected_next_bits"] is None
            ):
                raise ValueError(
                    f"height {row['height']}: unsigned work outside readiness"
                )
            job = row["unsigned_candidate"]
            check_coinbase(job, recent, row["observed_at"])
            ids = job["transactions"]
            if len(ids) != row["pending"] or any(
                len(bytes.fromhex(txid)) != 32 for txid in ids
            ):
                raise ValueError(
                    f"height {row['height']}: candidate commitments differ from pending count or txid width"
                )
            if len(set(ids)) != len(ids):
                raise ValueError(
                    f"height {row['height']}: duplicate transaction commitment"
                )
            if forecast is not None:
                ready_assemblies += 1
        else:
            reason = []
            if row["expected_next_bits"] is None:
                reason.append("difficulty window incomplete")
            if not directive["ready"]:
                reason.append("directives unready")
            if not reason:
                reason.append(
                    "no template recorded with available difficulty and directives"
                )
            reasons[" + ".join(reason)] += 1
        candidates = reports[(row["height"], row["hash"])]
        peers = {
            peer for peer, at in candidates if abs(at - row["observed_at"]) <= 7200
        }
        match_histogram[len(peers)] += 1
        previous = row

    if previous is None:
        raise ValueError("no observed tip")
    completed_failover = (
        verified_failover(records, snapshot["failover"])
        and baseline["sv_only_failovers"] > 0
    )
    return {
        "external_reference": check_reference(
            records, snapshot["reference"], current_work
        )
        if "reference" in snapshot
        else None,
        "first": {
            "height": first["height"],
            "hash": first["hash"],
            "observed_utc": utc(first["observed_at"]),
        },
        "latest": {
            "height": previous["height"],
            "hash": previous["hash"],
            "observed_utc": utc(previous["observed_at"]),
        },
        "target_contiguous_blocks": TARGET_BLOCKS,
        "target_length_observed": baseline["longest_contiguous_run"] >= TARGET_BLOCKS,
        "current_contiguous_blocks": baseline["current_contiguous_run"],
        "current_run_first_height": current_start,
        "current_run_accumulated_work_hex": hex(current_work),
        "wire_headers_rehashed": baseline["headers_rechecked"],
        "header_median_time_checks": mtp_checks,
        "independent_cw144_predictions": expected_count,
        "predictions_compared_to_successors": observed_count,
        "signed_ready_records": signed_ready,
        "latest_ready_signed_sequence": latest_signed,
        "unsigned_templates": baseline["unsigned_templates"],
        "unsigned_templates_with_independent_cw144": ready_assemblies,
        "withheld_decisions": dict(sorted(reasons.items())),
        "restored_process_starts": baseline["restart_boundaries_recovered"],
        "restore_mismatch_records": restored_mismatches,
        "receipt_confirmed_sv_only_failover": completed_failover,
        "distinct_timed_reporting_peers_per_tip": dict(sorted(match_histogram.items())),
        "coverage_failure_recoveries": coverage_recoveries,
        "coverage_failures_awaiting_successor": len(gaps),
        "continuity_or_node_prediction_exceptions": baseline["exceptions"],
    }


def qualification(snapshot, summary):
    """Assess the current linked run after reconcile has replayed its commitments."""
    records = snapshot["records"]
    run = current_run(records)
    start = next(i for i, row in enumerate(records) if row is run[0])
    positions = [
        i for i in range(start, len(records)) if records[i].get("event", "tip") == "tip"
    ]
    # A missing prospective decision or a lost boundary requires a new clean
    # observation period. Keep the entire prefix and its failures in the audit.
    disqualified = [i for i in summary["restore_mismatch_records"] if i > start]
    disqualified.extend(
        e["record"]
        for e in summary["continuity_or_node_prediction_exceptions"]
        if e["kind"] == "unexpected_bits" and e["record"] > start
    )
    incomplete_coverage = []
    missing_decisions = []
    for i, row in enumerate(run):
        if row["coverage"] < min(147, i + 1):
            incomplete_coverage.append(row["height"])
            disqualified.append(positions[i] + 1)
        if row["ready"] and row["pending"] and row.get("unsigned_candidate") is None:
            missing_decisions.append(row["height"])
            disqualified.append(positions[i] + 1)
    # Exceptions use one-based journal record numbers. Slicing at that number
    # excludes the affected record and includes only later prospective events.
    boundary = max([start, *disqualified])
    suffix = records[boundary:]
    window = [row for row in suffix if row.get("event", "tip") == "tip"]
    reports = timed_reports(records)
    readbacks = snapshot.get("reference", {}).get("by_hash", {})
    missing_corroboration = [
        r["height"]
        for r in window
        if not separate_report(r, reports) and r["hash"] not in readbacks
    ]
    # An unsigned template on the last observed tip still awaits a successor.
    # Retrospective header readbacks never advance this on-wire window.
    wire_run = templates = 0
    for i, row in enumerate(window):
        wire_run = wire_run + 1 if row.get("header_hex") else 0
        job = row.get("unsigned_candidate")
        if (
            wire_run >= 147
            and job
            and job["transactions"]
            and i + 1 < len(window)
            and window[i + 1].get("header_hex")
            and window[i + 1]["bits"] == job["bits"]
        ):
            templates += 1
    failover = verified_failover(suffix, snapshot["failover"])
    reference = summary["external_reference"]
    endpoint_matched = bool(
        reference
        and reference["absolute_chainwork_delta_matched"]
        and reference["tip_hash"] == run[-1]["hash"]
        and reference["anchor_hash"] == run[0]["previous"]
    )
    pending = []
    for passed, reason in (
        (
            len(window) >= TARGET_BLOCKS,
            "current clean linked window is shorter than the target",
        ),
        (
            failover,
            "current run lacks a verified failover and matching restored tips",
        ),
        (
            templates > 0,
            "no transaction-bearing unsigned template with an independently verified successor in the current run",
        ),
        (
            not summary["coverage_failures_awaiting_successor"],
            "a coverage failure still awaits a successor",
        ),
        (
            bool(window and window[-1]["directives"]["ready"]),
            "latest signed directive state is unready",
        ),
    ):
        if not passed:
            pending.append(reason)
    local_complete = not pending
    if not endpoint_matched:
        pending.append(
            "independent readbacks of the active tip and chainwork anchor are required"
        )
    if missing_corroboration:
        pending.append(
            "tip observations lack a separate timed report or independent readback"
        )
    return {
        "contract": FINAL_CONTRACT,
        "qualified": not pending,
        "local_evidence_complete": local_complete,
        "current_linked_blocks": len(run),
        "qualifying_window_blocks": len(window),
        "qualifying_window_first_height": window[0]["height"] if window else None,
        "excluded_prefix_blocks": len(run) - len(window),
        "disqualifying_records_in_linked_run": sorted(set(disqualified)),
        "required_linked_blocks": TARGET_BLOCKS,
        "templates_with_verified_successor": templates,
        "unexplained_missing_templates": missing_decisions,
        "incomplete_coverage_heights": incomplete_coverage,
        "uncorroborated_heights": missing_corroboration,
        "active_tip_and_anchor_matched": endpoint_matched,
        "failover_and_restores_in_current_run": failover,
        "pending": pending,
    }


def atomic_write(path, payload):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        name = Path(tmp.name)
        try:
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp.close()
            name.replace(path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            name.unlink(missing_ok=True)


def checkpoint(snapshot, output, with_reference, *, final=False):
    snapshot = dict(snapshot)
    if with_reference:
        snapshot["reference"] = public_reference(snapshot["records"])
    summary = reconcile(snapshot)
    if final:
        result = qualification(snapshot, summary)
        if not result["qualified"]:
            raise ValueError(
                "shadow qualification pending: " + "; ".join(result["pending"])
            )
        snapshot["qualification_contract"] = FINAL_CONTRACT
        summary["qualification"] = result
    # Stable gzip bytes also support a public digest of the exact redacted observations.
    packed = gzip.compress(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode(),
        compresslevel=9,
        mtime=0,
    )
    summary["observations_sha256"] = hashlib.sha256(packed).hexdigest()
    atomic_write(output, packed)
    return summary


def replay(path, *, assess=False):
    packed = path.read_bytes()
    snapshot = json.loads(gzip.decompress(packed))
    summary = reconcile(snapshot)
    contract = snapshot.get("qualification_contract")
    if contract is not None and contract != FINAL_CONTRACT:
        raise ValueError("unknown mainnet shadow qualification contract")
    if assess or contract is not None:
        result = qualification(snapshot, summary)
        if contract is not None and not result["qualified"]:
            raise ValueError("checkpoint claims an unmet shadow qualification")
        summary["qualification"] = result
    summary["observations_sha256"] = hashlib.sha256(packed).hexdigest()
    return summary


def follow(journal, failover, output):
    last_revision = None
    while True:
        try:
            revision = (journal.stat().st_mtime_ns, failover.stat().st_mtime_ns)
        except FileNotFoundError:
            time.sleep(60)
            continue
        if revision != last_revision:
            snapshot = export(journal, failover)
            if not snapshot["audit"]["observed_transitions"]:
                time.sleep(60)
                continue
            result = qualification(snapshot, reconcile(snapshot))
            last_revision = revision
            if result["local_evidence_complete"]:
                try:
                    return checkpoint(snapshot, output, with_reference=True, final=True)
                except HTTPError as exc:
                    if exc.code not in {404, 408, 425, 429, 500, 502, 503, 504}:
                        raise
                except (TimeoutError, ConnectionError, URLError, InactiveReference):
                    pass
                # Refresh the journal too. A fork may have changed the
                # selected run while the public readback was unavailable.
                last_revision = None
        time.sleep(60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    for command in ("capture", "follow"):
        save = sub.add_parser(command)
        save.add_argument("journal", type=Path)
        save.add_argument("failover", type=Path)
        save.add_argument("output", type=Path)
        save.add_argument(
            "--reference",
            action="store_true",
            help="reconcile against public mainnet header and chainwork readbacks",
        )
    for command in ("replay", "qualify"):
        check = sub.add_parser(command)
        check.add_argument("snapshot", type=Path)
    args = parser.parse_args()
    if args.mode == "follow":
        report = follow(args.journal, args.failover, args.output)
    elif args.mode == "capture":
        report = checkpoint(
            export(args.journal, args.failover), args.output, args.reference
        )
    else:
        report = replay(args.snapshot, assess=args.mode == "qualify")
    print(json.dumps(report, indent=2))
    if args.mode == "qualify" and not report["qualification"]["qualified"]:
        raise SystemExit(1)
