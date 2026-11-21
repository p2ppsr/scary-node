"""Export and independently replay closed, fixed-size native stale-rate cohorts."""

import argparse
import csv
import gzip
import hashlib
import json
import math
import os
import random
import re
import statistics
import tempfile
from collections import Counter
from pathlib import Path

from cohort_estimate import count_session, critical, read
from sv_reference import IMAGE

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs/evidence"
ARMS = ("witnessed/full", "full/full")
MEASURES = (
    "scheduled",
    "ready",
    "discovered",
    "settled",
    "stale",
    "hashes",
    "later_detached",
)
SHARD = 128


def check(condition, message):
    if not condition:
        raise ValueError(message)


def canonical(value):
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


def sha(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def original_registration():
    return {
        "source": "docs/production-cohort.md (de4ac29), published original sizing (a0d7ea0)",
        "design": {
            "study": "original",
            "binary_sha256": read(EVIDENCE / "chronicle-native-sizing.json")[
                "binary_sha256"
            ],
            "pilot_sessions": 16,
            "first_seeds": {"pilot": 41001, "evaluation": 51001},
            "opportunities_per_session": 32,
            "offer_delay_ms": 1200,
            "target_half_width": 0.01,
        },
    }


def bind_registration(registration, independent=None):
    study = registration["design"]["study"]
    if independent is None:
        if study == "original":
            independent = original_registration()
        else:
            check(
                study == "immediate"
                or (study.startswith("release-") and study[8:].isdigit()),
                "unknown registration; supply its published source",
            )
            independent = read(EVIDENCE / f"chronicle-{study}-registration.json")
    check(
        registration == independent,
        "archive registration differs from the independently supplied design",
    )


def bind_pilot(design, phase, sizing, pilot_archive, registration):
    if phase != "evaluation":
        return
    if design["study"] == "original" and pilot_archive is None:
        reference = read(EVIDENCE / "chronicle-native-sizing.json")
        sd = reference["arms"][ARMS[0]]["session_influence_sd"]
        fixed = reference["fresh_evaluation_sessions"]
    else:
        pilot_archive = pilot_archive or EVIDENCE / f"chronicle-{design['study']}-pilot"
        check(
            read(pilot_archive / "manifest.json")["phase"] == "pilot",
            "sizing archive must be a completed pilot",
        )
        pilot = verify(pilot_archive, registration)
        sd = pilot["arms"][ARMS[0]]["conditional_stale"]["session_influence_sd"]
        fixed = pilot["fresh_evaluation_sessions"]
    check(
        fixed == sizing["fresh_evaluation_sessions"]
        and math.isclose(
            sd,
            sizing["arms"][ARMS[0]]["session_influence_sd"],
            rel_tol=1e-12,
            abs_tol=1e-14,
        ),
        "evaluation design differs from independent completed pilot",
    )


def phase_size(design, phase, sizing):
    check(
        design["pilot_sessions"] == 16
        and design["opportunities_per_session"] == 32
        and design["offer_delay_ms"] == 1200
        and design["target_half_width"] == 0.01,
        "unexpected study design",
    )
    if phase == "pilot":
        check(sizing is None, "a pilot has a preset size")
        return 16
    check(
        phase == "evaluation" and sizing,
        "evaluation needs its own completed sizing pilot",
    )
    check(
        sizing["phase"] == "pilot"
        and sizing.get("study", "original") == design["study"]
        and sizing["binary_sha256"] == design["binary_sha256"]
        and sizing["sessions"] == 16,
        "pilot identity differs from registration",
    )
    fixed = max(
        16,
        math.ceil(
            (critical(15) * sizing["arms"][ARMS[0]]["session_influence_sd"] / 0.01) ** 2
        ),
    )
    check(
        sizing["fresh_evaluation_sessions"] == fixed,
        "evaluation size disagrees with the registered formula",
    )
    return fixed


def check_schedule(r, design, seed):
    check(
        r["schema"] == 1
        and r["seed"] == seed
        and r["binary_sha256"] == design["binary_sha256"]
        and r["sv_image"] == design.get("sv_image", IMAGE),
        "session identity disagrees with registration",
    )
    rng = random.Random(seed)
    arms = list(ARMS) * 16
    rng.shuffle(arms)
    expected = [
        {
            "index": i,
            "arm": arm,
            "nonce": rng.randrange(2**31),
            "swap_nonce": bool(rng.randrange(2)),
            "successor_receiver": rng.randrange(2),
            "offer_delay_ms": 1200,
        }
        for i, arm in enumerate(arms)
    ]
    check(
        expected == r["planned"] and r["physical_swap"] is bool(rng.randrange(2)),
        "realized assignment differs from the seeded schedule",
    )


def check_public_diagnostics(r):
    check(
        not re.search(
            rb'/(?:Users|home)/[^/"]+/|https?://[^/\s"]+:[^/@\s"]+@|Authorization: (?:Basic|Bearer) ',
            canonical(r),
            re.IGNORECASE,
        ),
        "diagnostic contains a local path or authentication detail; review before publication",
    )


def validate_session(r, design, seed):
    check_schedule(r, design, seed)
    check(r.get("stage", "closed") == "closed", "unclosed opportunity stage")
    # Retain and recheck the reference wire bodies independently of the experiment's success flags.
    count_session(r)
    check_public_diagnostics(r)
    totals = {a: [Counter(), Counter()] for a in ARMS}
    for plan, row in zip(r["planned"], r["rows"]):
        for lane, producer in enumerate(row["producers"]):
            c = totals[row["arm"]][lane]
            ready, hashes, solution = (
                producer["ready"],
                producer["hashes"],
                producer["discovery"],
            )
            check(
                type(ready) is bool and type(hashes) is int,
                "invalid ready flag or hash count",
            )
            check(
                1 <= hashes <= 64 if ready else hashes == 0,
                "hash count does not match availability",
            )
            check(len(row["receiver_first_active_ns"]) == 2, "missing a receiver")
            c.update(scheduled=1, ready=int(ready), hashes=hashes)
            if not ready:
                c["reason:" + str(producer["readiness_reason"] or "unspecified")] += 1
            if solution is None:
                check(
                    producer["native_submit"] is None, "submission without a discovery"
                )
                continue
            submit = producer["native_submit"]
            check(
                ready
                and 0 <= submit["start_ns"] <= submit["finish_ns"]
                and ("ack" in submit) != ("error" in submit),
                "bad publication record",
            )
            prefix = bytes.fromhex(solution["wire_hex"][:152])
            for tick in range(hashes - 1):
                nonce = plan["nonce"] + 2 * tick + (lane ^ int(plan["swap_nonce"]))
                h = hashlib.sha256(
                    hashlib.sha256(prefix + nonce.to_bytes(4, "little")).digest()
                ).digest()
                check(
                    int.from_bytes(h, "little") > 0x7FFFFF << 232,
                    "discovery preceded the registered winning tick",
                )
            settled = solution["hash"] == row["selected_ancestry"][0]
            detached = settled and solution["hash"] != row["session_final_ancestry"][0]
            c.update(
                discovered=1,
                settled=int(settled),
                stale=int(not settled),
                later_detached=int(detached),
            )
    return totals


def contrast(counts, arm, numerator, denominator):
    n = len(counts)
    sums = [
        [
            sum(s[arm][lane][metric] for s in counts)
            for metric in (numerator, denominator)
        ]
        for lane in range(2)
    ]
    rates = [x / y if y else None for x, y in sums]
    if None in rates:
        return {
            "rates": rates,
            "difference": None,
            "session_influence_sd": None,
            "half_width": None,
            "confidence_interval_95": None,
            "reason": "zero denominator in a scheduled lane",
        }
    residuals = [
        sum(
            (1 if i == 0 else -1)
            * n
            * (s[arm][i][numerator] - rates[i] * s[arm][i][denominator])
            / sums[i][1]
            for i in range(2)
        )
        for s in counts
    ]
    sd = statistics.stdev(residuals)
    delta = rates[0] - rates[1]
    half = critical(n - 1) * sd / math.sqrt(n)
    return {
        "rates": rates,
        "difference": delta,
        "session_influence_sd": sd,
        "half_width": half,
        "confidence_interval_95": [max(-1, delta - half), min(1, delta + half)],
    }


def fresh_size(measures):
    sd = measures[ARMS[0]]["conditional_stale"]["session_influence_sd"]
    return None if sd is None else max(16, math.ceil((critical(15) * sd / 0.01) ** 2))


def summary(counts):
    return {
        a: {
            "roles": [
                {m: sum(s[a][lane][m] for s in counts) for m in MEASURES}
                for lane in range(2)
            ],
            "unready_reasons": [
                dict(
                    sum(
                        (
                            Counter(
                                {
                                    k[7:]: v
                                    for k, v in s[a][i].items()
                                    if k.startswith("reason:")
                                }
                            )
                            for s in counts
                        ),
                        Counter(),
                    )
                )
                for i in range(2)
            ],
            "conditional_stale": contrast(counts, a, "stale", "discovered"),
            "per_scheduled_opportunity": {
                m: contrast(counts, a, m, "scheduled")
                for m in ("ready", "discovered", "settled", "stale")
            },
        }
        for a in ARMS
    }


def closed_files(source, start, n):
    # Make the fixed-size decision using filenames before opening any race outcomes.
    files = {p.name: p for p in source.glob("*.json.gz")}
    names = [f"{i}.json.gz" for i in range(start, start + n)]
    check(
        set(files) == set(names),
        f"fixed phase is open or contains extraneous sessions ({len(files)}/{n} closed)",
    )
    check(
        {p.name for p in source.iterdir()}
        <= set(names) | {".writer.lock", "preflight", ".DS_Store"},
        "unfinished or unaccounted attempt exists; preserve it before export",
    )
    return [files[k] for k in names]


def inspect_preflight(failed, design, start, n):
    r = failed["receipt"]
    check_public_diagnostics(r)
    check(
        start <= r["seed"] < start + n and r["rows"] == [] and r["complete"] is False,
        "a measured interruption cannot be classified as a setup retry",
    )
    check_schedule(r, design, r["seed"])
    if r.get("stage") != "fixture":
        historical = (
            EVIDENCE / "chronicle-native-pilot/preflight/41007-fixture-race.json"
        )
        check(
            design["study"] == "original" and r == read(historical),
            "unverified setup classification",
        )
    check(Path(failed["name"]).name == failed["name"], "invalid attempt filename")


def csv_rows(start, counts):
    yield ["seed", "arm", "lane", *MEASURES]
    for seed, session in enumerate(counts, start):
        for arm in ARMS:
            for lane in range(2):
                yield [seed, arm, lane, *(session[arm][lane][m] for m in MEASURES)]


def export(source, destination, registration, phase, sizing, pilot_archive=None):
    design = registration["design"]
    n, start = phase_size(design, phase, sizing), design["first_seeds"][phase]
    bind_pilot(design, phase, sizing, pilot_archive, registration)
    files = closed_files(source, start, n)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        stage = Path(temporary) / "archive"
        stage.mkdir()
        counts, shards, failures = [], [], []
        for batch in range(0, n, SHARD):
            name = (
                f"sessions-{start + batch}-{start + min(n, batch + SHARD) - 1}.jsonl.gz"
            )
            out = stage / name
            with (
                out.open("wb") as f,
                gzip.GzipFile(
                    filename="", mode="wb", fileobj=f, mtime=0, compresslevel=6
                ) as stream,
            ):
                for offset, path in enumerate(files[batch : batch + SHARD], batch):
                    r = read(path)
                    counts.append(validate_session(r, design, start + offset))
                    stream.write(canonical(r))
            shards.append({"file": name, "sha256": sha(out)})
        for p in sorted((source / "preflight").glob("*")):
            check(p.is_file() and p.suffix == ".json", "unaccounted setup attempt file")
            attempt = {"name": p.name, "receipt": read(p)}
            inspect_preflight(attempt, design, start, n)
            failures.append(attempt)
        with (stage / "sessions.csv").open("w", newline="") as f:
            csv.writer(f, lineterminator="\n").writerows(csv_rows(start, counts))
        measures = summary(counts)
        m = {
            "schema": 1,
            "registration": registration,
            "phase": phase,
            "sizing": sizing,
            "fixed_sessions": n,
            "scheduled_parents": 32 * n,
            "session_counts_sha256": sha(stage / "sessions.csv"),
            "shards": shards,
            "setup_retries": failures,
            "arms": measures,
        }
        if phase == "pilot":
            m["fresh_evaluation_sessions"] = fresh_size(measures)
        if phase == "evaluation":
            width = measures[ARMS[0]]["conditional_stale"]["half_width"]
            m["achieved_one_point_precision"] = (
                width is not None and width <= design["target_half_width"]
            )
        (stage / "manifest.json").write_text(
            json.dumps(m, sort_keys=True, indent=2, allow_nan=False) + "\n"
        )
        verify(stage, registration, pilot_archive)
        if destination.exists():
            verify(destination, registration, pilot_archive)
            check(
                (destination / "manifest.json").read_bytes()
                == (stage / "manifest.json").read_bytes(),
                "destination already contains a different closed phase",
            )
        else:
            os.rename(stage, destination)
    return m


def verify(directory, independent_registration=None, pilot_archive=None):
    m = read(directory / "manifest.json")
    design, phase = m["registration"]["design"], m["phase"]
    bind_registration(m["registration"], independent_registration)
    n = phase_size(design, phase, m["sizing"])
    bind_pilot(design, phase, m["sizing"], pilot_archive, m["registration"])
    start = design["first_seeds"][phase]
    check(
        m["schema"] == 1
        and n == m["fixed_sessions"]
        and n * 32 == m["scheduled_parents"],
        "wrong fixed cohort size",
    )
    counts = []
    names = {"manifest.json", "sessions.csv"}
    for shard in m["shards"]:
        current = start + len(counts)
        length = min(SHARD, n - len(counts))
        check(
            length > 0
            and shard["file"] == f"sessions-{current}-{current + length - 1}.jsonl.gz",
            "unexpected shard boundary",
        )
        names.add(shard["file"])
        p = directory / shard["file"]
        check(sha(p) == shard["sha256"], "shard digest differs")
        with gzip.open(p, "rt") as stream:
            for line in stream:
                counts.append(
                    validate_session(json.loads(line), design, start + len(counts))
                )
        check(
            start + len(counts) == current + length, "wrong number of receipts in shard"
        )
    check(
        len(counts) == n and {p.name for p in directory.iterdir()} == names,
        "missing or extra cohort evidence",
    )
    for retry in m["setup_retries"]:
        inspect_preflight(retry, design, start, n)
    check(
        len({r["name"] for r in m["setup_retries"]}) == len(m["setup_retries"]),
        "duplicate preflight",
    )
    p = directory / "sessions.csv"
    check(sha(p) == m["session_counts_sha256"], "session count digest differs")
    with p.open(newline="") as f:
        check(
            list(csv.reader(f))
            == [[str(x) for x in row] for row in csv_rows(start, counts)],
            "session counts differ from raw opportunities",
        )
    computed = summary(counts)
    check(computed == m["arms"], "paired uncertainty or opportunity counts differ")
    if phase == "pilot":
        check(
            m["fresh_evaluation_sessions"] == fresh_size(computed),
            "pilot sizing differs",
        )
    if phase == "evaluation":
        width = computed[ARMS[0]]["conditional_stale"]["half_width"]
        check(
            m["achieved_one_point_precision"]
            is (width is not None and width <= design["target_half_width"]),
            "incorrect achieved precision status",
        )
    return m


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    verbs = p.add_subparsers(dest="verb", required=True)
    v = verbs.add_parser("verify")
    v.add_argument("archive", type=Path)
    v.add_argument(
        "--registration", type=Path, help="independently published study registration"
    )
    v.add_argument(
        "--pilot-archive", type=Path, help="independent completed pilot for evaluation"
    )
    e = verbs.add_parser("export")
    e.add_argument("source", type=Path)
    e.add_argument("archive", type=Path)
    e.add_argument(
        "--registration", type=Path, help="omit only for the original registered study"
    )
    e.add_argument("--phase", choices=("pilot", "evaluation"), required=True)
    e.add_argument("--pilot-summary", type=Path)
    e.add_argument("--pilot-archive", type=Path)
    a = p.parse_args()
    m = (
        verify(
            a.archive, read(a.registration) if a.registration else None, a.pilot_archive
        )
        if a.verb == "verify"
        else export(
            a.source,
            a.archive,
            read(a.registration) if a.registration else original_registration(),
            a.phase,
            read(a.pilot_summary) if a.pilot_summary else None,
            a.pilot_archive,
        )
    )
    print(
        f"Verified {m['phase']} with {m['fixed_sessions']} sessions, {m['scheduled_parents']} scheduled parents and {len(m['setup_retries'])} preflight retries."
    )
