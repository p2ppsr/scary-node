"""Build test-only source probes and replay the preregistered subtree scheduling experiment."""

import argparse
import gzip
import hashlib
import itertools
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "regtest"))
from prove import decode_block, dsha, txid
from run_cohort import session_lock

REFERENCE = "5148c31ae620544c31a61b1710172c4bcc524c54"
REGISTRATION = "aed924238ab3fd4d9d18ea2c69fd066e5b727a74"
TREATMENTS = {"original": [1, 16], "serial": [1, 256], "parallel": [4, 256]}
TEST = "peer::fetch::subtree_scheduler_probe::compare"
PROBE = ROOT / "qualification/subtree_scheduler_probe.rs"
SIGNED = ROOT / "docs/evidence/chronicle-mixed-workload.json.gz"


def required(test, msg):
    if not test:
        raise ValueError(msg)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def fixture():
    from native_workload import solve

    w = json.loads(gzip.decompress(SIGNED.read_bytes()))["mixed_workload"]
    full = bytes.fromhex(w["body_hex"])
    _, txs, _ = decode_block(full, w["solved_hash"])
    funding = bytes.fromhex(w["funding_block_hex"])
    decode_block(funding, w["funding_block"])
    required(len(txs) == 4097 and int(w["height"]) == 109, "signed fixture differs")
    child = solve(
        w["funding_block"], 108, int.from_bytes(funding[68:72], "little") + 1, []
    ).serialize()
    roots = []
    for i in range(1, 129, 2):
        a, b = txs[i : i + 2]
        roots.append(
            {
                "root": dsha(dsha(a) + dsha(b))[::-1].hex(),
                "wire": (a + b).hex(),
                "ids": [txid(a), txid(b)],
            }
        )
    return {
        "source_archive_sha256": sha(SIGNED.read_bytes()),
        "funding_hex": funding.hex(),
        "successor_hex": child.hex(),
        "parent": txid(child[:80]),
        "roots": roots,
    }


def build(out, rev, variant):
    out.mkdir(parents=True, exist_ok=True)
    rev = subprocess.check_output(
        ["git", "-C", ROOT, "rev-parse", rev + "^{commit}"], text=True
    ).strip()
    with tempfile.TemporaryDirectory(prefix="scary-subtree-source-") as t:
        archive = Path(t)
        with (
            subprocess.Popen(
                [
                    "git",
                    "-C",
                    ROOT,
                    "archive",
                    rev,
                    "--",
                    "Cargo.toml",
                    "Cargo.lock",
                    "src",
                    "native",
                    "LICENSE",
                ],
                stdout=subprocess.PIPE,
            ) as proc,
            tarfile.open(fileobj=proc.stdout, mode="r|") as tar,
        ):
            tar.extractall(archive, filter="data")
        required(not proc.returncode, "frozen source archive failed")
        (archive / ".native").symlink_to(
            (ROOT / ".native").resolve(), target_is_directory=True
        )
        module = archive / "src/peer/fetch.rs"
        with module.open("a") as f:
            f.write(
                '\n#[cfg(test)] mod subtree_scheduler_probe { include!("subtree_scheduler_probe.rs"); }\n'
            )
        wrapper = "fn start_probe_queue(state: Arc<tokio::sync::Mutex<Mining>>, parallel:usize, capacity:usize)->Queue {"
        if variant == "reference":
            wrapper += 'assert_eq!((parallel,capacity),(1,16)); Queue::start_subtrees("regtest".into(),state) }'
        else:
            # The live node consumes completion events. Give all 64 events room
            # and drain the receiver as the live node does.
            wrapper += """
                let (events, results) = tokio::sync::mpsc::channel(256);
                let queue = Queue::start_subtrees("regtest".into(), state, parallel, capacity, events);
                tokio::spawn(async move { let mut inbox=results; while inbox.recv().await.is_some() {} });
                queue
            }"""
        target = archive / "src/peer/subtree_scheduler_probe.rs"
        target.write_text(PROBE.read_text() + "\n" + wrapper)
        rustc = subprocess.check_output(
            ["rustup", "which", "--toolchain", "1.98.1", "rustc"], text=True
        ).strip()
        env = {**os.environ, "CARGO_TARGET_DIR": str(ROOT / "target"), "RUSTC": rustc}
        cmd = [
            "rustup",
            "run",
            "1.98.1",
            "cargo",
            "test",
            "--locked",
            "--offline",
            "--release",
            "--bin",
            "scary-node",
            "--no-run",
            "--message-format=json-render-diagnostics",
        ]
        with subprocess.Popen(
            cmd, cwd=archive, env=env, stdout=subprocess.PIPE, text=True
        ) as proc:
            artifacts = [
                j["executable"]
                for line in proc.stdout
                if (j := json.loads(line)).get("executable")
            ]
        required(
            not proc.returncode and len(artifacts) == 1,
            "failed to build archived test executable",
        )
        dest = out / variant
        shutil.copy2(artifacts[0], dest)
        meta = {
            "source": rev,
            "harness_sha256": sha(PROBE.read_bytes()),
            "injected_module_sha256": sha(target.read_bytes()),
            "probe_sha256": sha(dest.read_bytes()),
            "rustc": subprocess.check_output(
                ["rustup", "run", "1.98.1", "rustc", "--version"], text=True
            ).strip(),
        }
        (out / (variant + ".json")).write_text(json.dumps(meta, indent=2) + "\n")


def one(row, base):
    treatment = row["treatment"]
    data = row["sample"]
    par, cap = TREATMENTS[treatment]
    required((data["parallel"], data["capacity"]) == (par, cap), "wrong treatment")
    offered = data["offers"]
    required(
        [o["index"] for o in offered] == list(range(64))
        and all(a["at_us"] <= b["at_us"] for a, b in itertools.pairwise(offered)),
        "unexpected offer order",
    )
    accepted = [o["index"] for o in offered if o["accepted"]]
    raw = data["requests"]
    req = {i: (start, end) for i, start, end in raw}
    required(
        set(req) == set(accepted) and len(req) == len(raw),
        "requests differ from admitted queue",
    )
    target = [tx for i in accepted for tx in base["roots"][i]["ids"]]
    required(data["parent_before"] == base["parent"], "wrong starting parent")
    actual = data["transaction_ids"]
    ordered = actual == target
    ticks = []
    for i, (start, end) in req.items():
        required(
            offered[i]["at_us"] <= start
            and end - start >= (400000 if i == 0 else 100000)
            and end <= data["drained_us"],
            "unexpected request latency",
        )
        ticks.extend([(start, 1), (end, -1)])
    concurrent = peak = 0
    for _, change in sorted(ticks):
        concurrent += change
        peak = max(peak, concurrent)
    return {
        "accepted": len(accepted),
        "full": 64 - len(accepted),
        "ordered_payments": len(actual) if ordered else 0,
        "template_payments": len(actual),
        "matches_admission_order": ordered,
        "same_parent": data["parent_after"] == base["parent"],
        "peak_requests": peak,
        "last_offer_us": offered[-1]["at_us"],
        "after_last_offer_us": data["drained_us"] - offered[-1]["at_us"],
        "max_offer_schedule_offset_us": max(
            abs(o["at_us"] - 20000 * o["index"]) for o in offered
        ),
        "drained_us": data["drained_us"],
        "first_root_finished_after_later_root": any(
            i != 0 and end < req[0][1] for i, (s, end) in req.items()
        ),
    }


def report(r):
    base = fixture()
    required(
        r["registration"] == REGISTRATION
        and r["fixture_sha256"]
        == sha(json.dumps(base, separators=(",", ":")).encode()),
        "fixture or registration differs",
    )
    required(
        r["builds"]["reference"]["source"] == REFERENCE
        and all(
            v["harness_sha256"] == sha(PROBE.read_bytes()) for v in r["builds"].values()
        ),
        "probe/source mismatch",
    )
    sequence = list(itertools.permutations(TREATMENTS))
    rows = r["trials"]
    required(
        [(a["block"], a["treatment"]) for a in rows]
        == [(i, t) for i, group in enumerate(sequence, 1) for t in group],
        "incomplete or reordered balanced trials",
    )
    by = {k: [] for k in TREATMENTS}
    for row in rows:
        by[row["treatment"]].append(one(row, base))
    medians = {k: statistics.median(x["drained_us"] for x in v) for k, v in by.items()}
    passed = (
        all(
            x["ordered_payments"] == 128
            and x["same_parent"]
            and 2 <= x["peak_requests"] <= 4
            for x in by["parallel"]
        )
        and medians["parallel"] < medians["serial"]
    )
    return {"trials": by, "median_drained_us": medians, "parallel_acceptance": passed}


def run(directory, out):
    partial = out.with_suffix(".partial")
    temp = out.with_suffix(".next")
    required(
        not any(p.exists() for p in (out, partial, temp)),
        "preserve any prior trial or interruption; choose a fresh receipt",
    )

    base = fixture()
    fixture_bytes = json.dumps(base, separators=(",", ":")).encode()
    rows = []
    result = {
        "registration": REGISTRATION,
        "host": {
            "architecture": platform.machine(),
            "system": platform.system(),
            "kernel": platform.release(),
            "logical_cpu_count": os.cpu_count(),
        },
        "bdk": {
            k: v
            for k, v in json.loads(
                (ROOT / ".native/provenance.json").read_bytes()
            ).items()
            if k in {"bdk_commit", "sv_node_commit", "native_archive_sha256"}
        },
        "fixture_sha256": sha(fixture_bytes),
        "builds": {
            v: json.loads((directory / (v + ".json")).read_bytes())
            for v in ("reference", "candidate")
        },
        "trials": rows,
    }
    for v, meta in result["builds"].items():
        required(
            sha((directory / v).read_bytes()) == meta["probe_sha256"],
            "probe executable changed",
        )
    with (
        session_lock(),
        tempfile.TemporaryDirectory(prefix="scary-subtree-trials-") as td,
    ):
        tmp = Path(td)
        f = tmp / "fixture.json"
        f.write_bytes(fixture_bytes)
        for i, group in enumerate(itertools.permutations(TREATMENTS), 1):
            for treatment in group:
                variant = "reference" if treatment == "original" else "candidate"
                par, cap = TREATMENTS[treatment]
                sample = tmp / "sample.json"
                env = {
                    **os.environ,
                    "SCARY_SCHEDULER_FIXTURE": str(f),
                    "SCARY_SCHEDULER_PARALLEL": str(par),
                    "SCARY_SCHEDULER_CAPACITY": str(cap),
                    "SCARY_SCHEDULER_RESULT": str(sample),
                }
                temp.write_text(
                    json.dumps(
                        {
                            **result,
                            "running_trial": {"block": i, "treatment": treatment},
                        }
                    )
                )
                temp.replace(partial)
                subprocess.run(
                    [
                        directory / variant,
                        "--exact",
                        TEST,
                        "--ignored",
                        "--test-threads=1",
                    ],
                    env=env,
                    check=True,
                )
                rows.append(
                    {
                        "block": i,
                        "treatment": treatment,
                        "sample": json.loads(sample.read_bytes()),
                    }
                )
                sample.unlink()
                # Preserve every completed test before moving to the next treatment.
                temp.write_text(json.dumps(result))
                temp.replace(partial)
    out.write_bytes(
        gzip.compress(json.dumps(result, separators=(",", ":")).encode(), mtime=0)
    )
    print(json.dumps(report(result), indent=2))
    partial.unlink()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="action", required=True)
    b = sub.add_parser("build")
    b.add_argument("directory", type=Path)
    b.add_argument("variant", choices=["reference", "candidate"])
    b.add_argument("revision")
    r = sub.add_parser("run")
    r.add_argument("directory", type=Path)
    r.add_argument("output", type=Path)
    v = sub.add_parser("verify")
    v.add_argument("input", type=Path)
    args = p.parse_args()
    if args.action == "build":
        with session_lock():
            build(args.directory, args.revision, args.variant)
    elif args.action == "run":
        run(args.directory, args.output)
    else:
        print(
            json.dumps(
                report(json.loads(gzip.decompress(args.input.read_bytes()))), indent=2
            )
        )
