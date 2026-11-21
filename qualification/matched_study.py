"""Preregister, run and independently replay a calibrated fixed-size propagation study."""

import argparse
import contextlib
import datetime
import fcntl
import gzip
import hashlib
import json
import math
import os
import random
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

from cohort_archive import ROOT, canonical, check, sha
from cohort_estimate import critical, decode_block, dsha, read, txid
from cohort_registration import HARNESS as ORIGINAL_FILES
from cohort_registration import git
from matched_archive import sessions, write_shards
from matched_propagation import TARGET, check_session, paired_delay_effect
from sv_reference import IMAGE
from verify_matched_calibration import verify as verify_calibration

HARNESS = sorted(
    set(
        ORIGINAL_FILES
        + [
            f"qualification/{f}.py"
            for f in (
                "matched_study",
                "matched_archive",
                "matched_continuation",
                "matched_fixture",
                "matched_propagation",
                "verify_matched_calibration",
            )
        ]
    )
)
PILOT = 16
REPEAT = 6
ARMS = ("witnessed/full", "full/full")


def atom(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".writing")
    with tmp.open("wb") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def registration(binary, manifest, linux_manifest, calibration, output):
    cal = read(calibration)
    timing = verify_calibration(cal)
    mac, linux = read(manifest), read(linux_manifest)
    check(
        sha(binary) == mac["executable_sha256"] == cal["build"]["executable_sha256"],
        "calibrated binary digest differs",
    )
    check(
        mac["source_commit"] == linux["source_commit"] == cal["build"]["source_commit"]
        and mac["source_tree"] == linux["source_tree"] == cal["build"]["source_tree"]
        and mac["cargo_lock_sha256"] == linux["cargo_lock_sha256"]
        and mac["source_archive_sha256"] == cal["build"]["source_archive_sha256"],
        "source manifests do not describe the calibrated release",
    )
    harness = git("rev-parse", "HEAD").decode().strip()
    pins = {f: sha(ROOT / f) for f in HARNESS}
    check(
        all(
            hashlib.sha256(git("show", f"{harness}:{f}")).hexdigest() == v
            for f, v in pins.items()
        ),
        "commit the prospective measurement harness before registration",
    )
    eps, hold, delta = (
        timing["proposed_tolerance_ns"],
        timing["proposed_hold_ns"],
        timing["proposed_signed_delay_ns"],
    )
    fence = max(250_000_000, 4 * eps)
    # The other registered study namespaces occupy only integers below 2**53.
    # A release/method/calibration-specific 128-bit namespace leaves 32 bits for sessions.
    base = (
        (1 << 128)
        + int(
            hashlib.sha256(
                canonical([sha(binary), sha(calibration), pins])
            ).hexdigest()[:32],
            16,
        )
    ) << 32
    design = {
        "kind": "matched native ingress",
        "release_commit": mac["source_commit"],
        "source_tree": mac["source_tree"],
        "source_archives_sha256": {
            "mac": mac["source_archive_sha256"],
            "linux": linux["source_archive_sha256"],
        },
        "binary_sha256": sha(binary),
        "linux_executable_sha256": linux["release"]["executable_sha256"],
        "python_minor": list(sys.version_info[:2]),
        "measurement_commit": harness,
        "measurement_file_sha256": pins,
        "reference_image": IMAGE,
        "calibration_sha256": sha(calibration),
        "seed_base_decimal": str(base),
        "pilot_sessions": PILOT,
        "opportunities_per_session": 6 * REPEAT,
        "opportunities_per_arm_and_offset": REPEAT,
        "hold_ns": hold,
        "tolerance_ns": eps,
        "signed_delay_ns": delta,
        "pong_timeout_ns": fence,
        "capture_window_ns": hold + delta + fence,
        "probe_lead_ns": 100_000_000,
        "offer_delay_ms": 1200,
        "job_response_window_ns": 1_500_000_000,
        "target_half_width": 0.01,
        "interpretation": "Report the interval endpoints and whether its upper bound is at most +0.01. A finite regtest cohort cannot establish exact equality or mainnet risk.",
        "primary": "Equal-assigned witnessed minus full stale/discovered. Pooled ratios and whole independent session paired influence, two-sided Student 95% interval.",
        "certification": "For an ingress-matched interpretation, both receivers must certify at least 95% of all two-discovery parents assigned zero offset in each producer arm. All assigned outcomes remain in their treatment.",
        "sizing": "max(16, ceil((t(0.975,15) * equal-assigned witnessed/full pilot session influence SD / 0.01)^2)); no evaluation with undefined primary denominator.",
        "phase_seeds": "Pilot base+1..16 inclusive. Evaluation base+17 onward; fixed pilot-derived size, no reuse.",
        "interruptions": "Retain every attempt. A failed setup before the first offered payment may restart the seed. A measured interruption must be preserved; unresolved slots prevent a complete phase.",
    }
    if output.exists():
        check(read(output)["design"] == design, "different registration already exists")
    else:
        atom(
            output,
            canonical(
                {
                    "registered_utc": datetime.datetime.now(datetime.UTC).isoformat(),
                    "design": design,
                }
            ),
        )
    return design


def schedule(d, seed):
    rng = random.Random(seed)
    swap = bool(rng.randrange(2))
    conditions = [(a, off) for a in ARMS for off in (-1, 0, 1) for _ in range(REPEAT)]
    rng.shuffle(conditions)
    plans = []
    for i, (arm, off) in enumerate(conditions):
        first = [rng.randrange(2) for _ in range(2)]
        timing = {
            k: d[k]
            for k in ("hold_ns", "tolerance_ns", "capture_window_ns", "pong_timeout_ns")
        }
        timing["offsets_ns"] = [
            max(0, off) * d["signed_delay_ns"],
            max(0, -off) * d["signed_delay_ns"],
        ]
        plans.append(
            {
                "index": i,
                "arm": arm,
                "timing": timing,
                "nonce": rng.randrange((1 << 32) - 128),
                "swap_nonce": bool(rng.randrange(2)),
                "first_receiver": first,
                "first_lane": rng.randrange(2),
                "successor_receiver": rng.randrange(2),
                **{
                    k: d[k]
                    for k in (
                        "probe_lead_ns",
                        "offer_delay_ms",
                        "job_response_window_ns",
                    )
                },
            }
        )
    return swap, plans


def run_session(path, binary, d, seed):
    from matched_continuation import native_branch, stock_readbacks
    from matched_fixture import begin_offer, fixture, witnessed_job
    from matched_propagation import (
        close_session,
        opportunity,
        receiver_roster,
        sample_jobs,
        settle,
    )
    from native_cohort import full_job

    swap, plans = schedule(d, seed)
    rec = {
        "seed_decimal": str(seed),
        "binary_sha256": d["binary_sha256"],
        "swapped": swap,
        "plan": plans,
        "rows": [],
        "stage": "fixture",
        "complete": False,
    }

    def save():
        atom(path, canonical(rec))

    save()
    with fixture(binary, len(plans), swap) as f:
        a, b, r0, r1 = f["stocks"]
        receivers = (r0, r1)
        rec["base"] = parent = f["base"]
        rec["private_stock_bridges"] = f["private_bridges"]
        height = r0.rpc("getblockcount") + 1
        for plan, payment in zip(plans, f["offers"]):
            before = receiver_roster(receivers)
            check(
                all(n.rpc("getbestblockhash") == parent for n in f["stocks"]),
                "stock branch not synchronized before offer",
            )
            rec["stage"] = "offering"
            rec["next_index"] = plan["index"]
            save()
            witnessed = plan["arm"] == ARMS[0]
            with ThreadPoolExecutor(5) as offers, contextlib.ExitStack() as jobs:
                start, attempts = begin_offer(f, payment, offers)

                def stock_job(node, label, parent=parent, payment=payment):
                    return full_job(node, parent, payment, label)[:2]

                left = (
                    partial(witnessed_job, f, jobs, parent, payment)
                    if witnessed
                    else partial(stock_job, a, "matched-full-left")
                )
                right = partial(stock_job, b, "matched-full-right")
                samples = sample_jobs(plan, start, [left, right])
                row = opportunity(
                    plan,
                    samples,
                    [f["scary"] if witnessed else f["roots"][0], f["roots"][1]],
                    f["dest"],
                    parent=parent,
                    height=height,
                    payment_txid=txid(payment),
                    receiver_probe=lambda hashes, t, due: stock_readbacks(
                        receivers, hashes, t, due
                    ),
                )
            row.update(
                receiver_peers_before_offer=before,
                offer_dispositions=[attempt.result() for attempt in attempts],
                common_payment_hex=payment.hex(),
            )
            rec["rows"].append(row)
            rec["stage"] = "native_gate_closed"
            save()
            hashes = [v["discovery"]["hash"] for v in row["lanes"] if v["discovery"]]
            row["postdeadline_readbacks"] = stock_readbacks(
                receivers, hashes, row["discovery_tick_monotonic_ns"]
            )
            save()

            def sync(row=row):
                save()
                native_branch(f, row)

            settle(row, receivers, sync)
            rec["stage"] = "settled"
            save()
            parent = row["settlement"]["endpoint_tips"][0]
            height += 3
        close_session(plans, rec["rows"], receivers)
        rec["stage"] = "complete"
        rec["complete"] = True
        save()
    return rec


def verify_reads(row, key):
    sample = row[key]
    check(len(sample) == 2, "missing independently timed stock readback")
    possible = {
        s["discovery"]["hash"]: s["discovery"]["body_sha256"]
        for s in row["lanes"]
        if s["discovery"]
    }
    for r in sample:
        a, b = r["active_query_ns"]
        check(
            0 <= a <= b and r["active_hash"] in {*possible, row["parent"]},
            "invalid observed active tip",
        )
        start = (
            row["capture_window_ns"]
            + row["pong_timeout_ns"]
            - row["plan"]["probe_lead_ns"]
            if key == "predeadline_readbacks"
            else row["gate_complete_ns"]
        )
        check(
            a >= start and set(r["bodies"]) == set(possible),
            "readback differs from assigned times or discovery list",
        )
        for h, body in sorted(
            r["bodies"].items(), key=lambda item: item[1]["query_ns"][0]
        ):
            s, e = body["query_ns"]
            check(
                b <= s <= e
                and (
                    body.get("body_sha256") == possible[h]
                    or body.get("rpc_code") in (-1, -5)
                ),
                "invalid stock body readback",
            )
            b = e


def verify_session(rec, d, seed):
    swap, plans = schedule(d, seed)
    check(
        rec["complete"]
        and rec["stage"] == "complete"
        and rec["swapped"] == swap
        and rec["plan"] == plans
        and rec["seed_decimal"] == str(seed)
        and rec["binary_sha256"] == d["binary_sha256"]
        and rec["private_stock_bridges"] == 4,
        "wrong session identity or schedule",
    )
    rows = rec["rows"]
    totals = check_session(plans, rows)
    check(rec["base"] == rows[0]["parent"], "wrong initial parent")
    for row in rows:
        payment = bytes.fromhex(row["common_payment_hex"])
        check(
            txid(payment) == row["payment_txid"]
            and len(row["offer_dispositions"]) == 5
            and all(type(x["accepted"]) is bool for x in row["offer_dispositions"]),
            "missing transaction offer",
        )
        for i, lane in enumerate(row["lanes"]):
            if not lane["ready"]:
                continue
            prefix = bytes.fromhex(lane["worker_header_prefix_hex"])
            cb = bytes.fromhex(lane["worker_coinbase_hex"])
            check(
                len(prefix) == 76
                and prefix[4:36][::-1].hex() == row["parent"]
                and prefix[72:76] == bytes.fromhex("ffff7f20")
                and prefix[36:68] == dsha(dsha(cb) + dsha(payment)),
                "worker template differs from common payment",
            )
            for tick in range(lane["hashes"]):
                nonce = (
                    row["plan"]["nonce"]
                    + 2 * tick
                    + (i ^ int(row["plan"]["swap_nonce"]))
                )
                hdr = prefix + nonce.to_bytes(4, "little")
                found = int.from_bytes(dsha(hdr), "little") <= TARGET
                check(
                    found == (tick == lane["hashes"] - 1 and bool(lane["discovery"])),
                    "earlier assigned nonce solved or discovery omitted",
                )
            if lane["discovery"]:
                wire = hdr + bytes([2]) + cb + payment
                ident = dsha(hdr)[::-1].hex()
                decode_block(wire, ident)
                check(
                    ident == lane["discovery"]["hash"]
                    and hashlib.sha256(wire).hexdigest()
                    == lane["discovery"]["body_sha256"],
                    "worker solution commitment differs",
                )
        for key in ("predeadline_readbacks", "postdeadline_readbacks"):
            verify_reads(row, key)
        arm = row["plan"]["arm"]
        offsets = row["offsets_ns"]
        group = totals[f"{arm}:{offsets[0] - offsets[1]}ns"]
        horizon = row["capture_window_ns"] + row["pong_timeout_ns"]
        for lane, metrics in zip(row["lanes"], group["lanes"]):
            discovery = lane["discovery"]
            if discovery is None:
                continue
            h = discovery["hash"]
            for rx, (before, after) in enumerate(
                zip(row["predeadline_readbacks"], row["postdeadline_readbacks"])
            ):
                leg = lane.get("legs", [{}, {}])[rx]
                bp = before["bodies"][h]
                prefix = f"receiver_{rx}_"
                metrics[prefix + "ingress_proven_by_horizon"] += int(
                    leg.get("pong_ns", horizon + 1) <= horizon
                )
                metrics[prefix + "storage_query_finished_by_horizon"] += int(
                    bp["query_ns"][1] <= horizon
                )
                metrics[prefix + "body_proven_stored_by_horizon"] += int(
                    bp["query_ns"][1] <= horizon and "body_sha256" in bp
                )
                metrics[prefix + "active_query_finished_by_horizon"] += int(
                    before["active_query_ns"][1] <= horizon
                )
                metrics[prefix + "observed_active_by_horizon"] += int(
                    before["active_query_ns"][1] <= horizon
                    and before["active_hash"] == h
                )
                metrics[prefix + "stored_before_continuation"] += int(
                    "body_sha256" in after["bodies"][h]
                )
                metrics[prefix + "observed_active_before_continuation"] += int(
                    after["active_hash"] == h
                )
        nc = row["native_continuation"]
        check(
            nc["source_receiver"] == row["plan"]["successor_receiver"]
            and nc["hashes"]
            == [
                dsha(bytes.fromhex(h))[::-1].hex()
                for h in row["settlement"]["selected_header_hex"]
            ]
            and nc["other_stock_active_tips"] == [nc["hashes"][-1]] * 3,
            "native post-deadline continuation differs",
        )
    return totals


def summarize(counts, d, phase):
    check(len(counts) >= PILOT, "phase below fixed minimum")
    ids = sorted(counts)
    effects = {
        arm: paired_delay_effect(counts, ids, arm, d["signed_delay_ns"]) for arm in ARMS
    }
    pooled = {}
    for c in counts.values():
        for k, v in c.items():
            g = pooled.setdefault(
                k,
                {"parents": 0, "alignment": Counter(), "lanes": [Counter(), Counter()]},
            )
            g["parents"] += v["parents"]
            g["alignment"].update(v["alignment"])
            for lane, agg in zip(v["lanes"], g["lanes"]):
                agg.update(lane)
    cert = {}
    for arm in ARMS:
        alignment = pooled[f"{arm}:0ns"]["alignment"]
        ties = sum(v for k, v in alignment.items() if k != "single_or_no_discovery")
        good = alignment["certified"]
        cert[arm] = {
            "two_discovery_parents": ties,
            "certified_at_both": good,
            "met_95_percent": ties > 0 and 100 * good >= 95 * ties,
        }
    primary = effects[ARMS[0]]["by_assigned_delay"][f"{ARMS[0]}:0ns"][
        "conditional_stale"
    ]
    result = {
        "phase": phase,
        "sessions": len(counts),
        "parents": sum(v["parents"] for v in pooled.values()),
        "pooled": pooled,
        "estimated_effects": effects,
        "equal_assigned_certification": cert,
        "upper_95_at_most_one_percentage_point": (
            primary["confidence_interval_95"] is not None
            and primary["confidence_interval_95"][1] <= d["target_half_width"]
        ),
        "achieved_primary_precision": primary["half_width"] is not None
        and primary["half_width"] <= d["target_half_width"],
    }
    if phase == "pilot":
        sd = primary["session_influence_sd"]
        result["fresh_evaluation_sessions"] = (
            None
            if sd is None
            else max(
                PILOT,
                math.ceil((critical(PILOT - 1) * sd / d["target_half_width"]) ** 2),
            )
        )
    return result


def phase_bounds(d, phase, pilot_summary):
    base = int(d["seed_base_decimal"])
    if phase == "pilot":
        return base + 1, PILOT
    check(
        pilot_summary
        and pilot_summary.get("phase") == "pilot"
        and pilot_summary["sessions"] == PILOT
        and pilot_summary["parents"] == PILOT * 6 * REPEAT
        and pilot_summary["binary_sha256"] == d["binary_sha256"],
        "evaluation requires its complete pilot",
    )
    n = pilot_summary["fresh_evaluation_sessions"]
    check(type(n) is int and PILOT <= n < (1 << 32) - 17, "invalid evaluation size")
    return base + 17, n


def public_registration(file):
    target = str(file.resolve().relative_to(ROOT))
    ref = (
        git("ls-remote", "--exit-code", "origin", "refs/heads/main").decode().split()[0]
    )
    git("fetch", "--quiet", "origin", ref)
    check(
        git("show", f"{ref}:{target}") == file.read_bytes(),
        "exact registration must already be published",
    )
    return read(file)


def close_phase(directory, registration, phase, pilot=None):
    d = registration["design"]
    start, n = phase_bounds(d, phase, pilot)
    expected = [str(start + i) + ".json.gz" for i in range(n)]
    check(
        not list(directory.glob("*.partial")),
        "refusing incomplete or unregistered phase",
    )
    counts = {}
    digests = {}
    for i, (name, record, digest) in enumerate(sessions(directory, expected)):
        counts[start + i] = verify_session(record, d, start + i)
        digests[name] = digest
    failures = {}
    for f in sorted((directory / "preflight").glob("*.json")):
        r = read(f)
        seed = int(r["seed_decimal"])
        check(
            start <= seed < start + n
            and not r["rows"]
            and r["stage"] == "fixture"
            and r["plan"] == schedule(d, seed)[1]
            and r["binary_sha256"] == d["binary_sha256"],
            "preflight contained measured exposure or different identity",
        )
        failures["preflight/" + f.name] = sha(f)
    return {
        **summarize(counts, d, phase),
        "binary_sha256": d["binary_sha256"],
        "registration_sha256": hashlib.sha256(canonical(registration)).hexdigest(),
        "session_sha256": digests,
        "zero_opportunity_preflight_sha256": failures,
    }


def run_phase(directory, reg_file, binary, phase, pilot_file=None, quiescence=None):
    from matched_fixture import measurement_lock

    reg = public_registration(reg_file)
    d = reg["design"]
    check(
        sha(binary) == d["binary_sha256"]
        and list(sys.version_info[:2]) == d["python_minor"]
        and d["reference_image"] == IMAGE
        and d["measurement_file_sha256"] == {f: sha(ROOT / f) for f in HARNESS},
        "registered executable, reference or measurement code differ",
    )
    if phase == "evaluation":
        check(pilot_file is not None, "a published pilot sizing artifact is required")
        pilot = public_registration(pilot_file)
        check(
            pilot == close_phase(directory / "pilot", reg, "pilot"),
            "published sizing does not match the complete registered pilot",
        )
    else:
        pilot = None
    start, n = phase_bounds(d, phase, pilot)
    path = directory / phase
    path.mkdir(parents=True, exist_ok=True)
    with (directory / ".matched-writer.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        local = directory / "registration.json"
        if local.exists():
            check(read(local) == reg, "study directory has a different registration")
        else:
            check(
                not list(path.glob("*.json.gz")) and not list(path.glob("*.partial")),
                "local registration missing for existing records",
            )
            atom(local, canonical(reg))
        for seed in range(start, start + n):
            final = path / f"{seed}.json.gz"
            partial = path / f"{seed}.partial"
            if final.exists():
                check(
                    not partial.exists(),
                    "unexpected partial and final receipt for same session",
                )
                continue
            if partial.exists():
                r = read(partial)
                if not r["complete"]:
                    check(
                        r["stage"] == "fixture" and not r["rows"],
                        "measured partial retained; phase cannot close with missing opportunities",
                    )
                    pre = path / "preflight" / f"{seed}-{time.time_ns()}.json"
                    pre.parent.mkdir(exist_ok=True)
                    os.replace(partial, pre)
            if not partial.exists():
                with measurement_lock(quiescence):
                    run_session(partial, binary, d, seed)
            r = read(partial)
            verify_session(r, d, seed)
            atom(final, gzip.compress(canonical(r), compresslevel=6, mtime=0))
            partial.unlink()
            print(
                f"completed {phase} session {seed - start + 1}/{n}",
                file=sys.stderr,
                flush=True,
            )
        result = close_phase(path, reg, phase, pilot)
        atom(directory / f"{phase}-summary.json", canonical(result))


def export_closed(directory, reg_file, phase, output, pilot_file=None):
    reg = read(reg_file)
    source = directory / phase
    summary = close_phase(source, reg, phase, read(pilot_file) if pilot_file else None)
    check(
        read(directory / f"{phase}-summary.json") == summary,
        "stored summary differs from replay",
    )
    output.mkdir(parents=True, exist_ok=True)
    atom(output / "registration.json", canonical(reg))
    names = sorted(summary["session_sha256"], key=lambda name: int(name.split(".")[0]))
    write_shards(source, output, names, atom)
    for name, digest in summary["zero_opportunity_preflight_sha256"].items():
        target = output / name
        if target.exists():
            check(sha(target) == digest, "existing public raw shard differs")
        else:
            atom(target, (source / name).read_bytes())
    atom(output / "summary.json", canonical(summary))
    check(
        close_phase(
            output,
            read(output / "registration.json"),
            phase,
            read(pilot_file) if pilot_file else None,
        )
        == summary,
        "exported raw evidence cannot reproduce summary",
    )
    return summary


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    sub = cli.add_subparsers(dest="action", required=True)
    p = sub.add_parser("register")
    for v in ("binary", "manifest", "linux-manifest", "calibration", "output"):
        p.add_argument("--" + v, required=True, type=Path)
    for op in ("run", "export", "check"):
        p = sub.add_parser(op)
        p.add_argument("directory", type=Path)
        p.add_argument("registration", type=Path)
        p.add_argument("--phase", choices=("pilot", "evaluation"), required=True)
        p.add_argument("--pilot-summary", type=Path)
        if op == "run":
            p.add_argument("--binary", type=Path, required=True)
            p.add_argument("--quiescence-receipt", type=Path)
        if op == "export":
            p.add_argument("--output", type=Path, required=True)
    a = cli.parse_args()
    if a.action == "register":
        registration(a.binary, a.manifest, a.linux_manifest, a.calibration, a.output)
    elif a.action == "run":
        run_phase(
            a.directory,
            a.registration,
            a.binary.resolve(),
            a.phase,
            a.pilot_summary,
            a.quiescence_receipt,
        )
    elif a.action == "export":
        export_closed(a.directory, a.registration, a.phase, a.output, a.pilot_summary)
    else:
        result = close_phase(
            a.directory,
            read(a.registration),
            a.phase,
            read(a.pilot_summary) if a.pilot_summary else None,
        )
        check(
            result == read(a.directory / "summary.json"),
            "claimed summary disagrees with independent replay",
        )
        print(
            json.dumps(
                {k: v for k, v in result.items() if not k.endswith("_sha256")}, indent=2
            )
        )
