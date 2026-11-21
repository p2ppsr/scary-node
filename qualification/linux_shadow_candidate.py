"""Replay the actual first saved mainnet candidate through both frozen Linux ARM64 Go oracles."""

import argparse
import gzip
import hashlib
import json
from pathlib import Path

from go_native_parity import ROLES
from linux_go_parity import EVIDENCE, isolated_oracles, verify_runtime
from shadow_bdk import replay
from shadow_first_candidate import assess, atomic_write
from shadow_native import inputs, require


def checked_summary(candidate, macos, macos_manifest, linux, release, manifest, sha256):
    result = assess(candidate)
    require(result["complete"], "original prospective evidence is incomplete")
    require(macos["evidence_sha256"] == sha256, "macOS checked another candidate")
    verify_runtime(linux["runtime"], release, manifest)
    confiscations = sum(
        inputs(bytes.fromhex(raw))[1][0][1].startswith(b"\x00\x6a\x04cftx")
        for raw in candidate["raw_transactions"]
    )
    ordinary = result["candidate_transactions"] - confiscations
    decisions = {}
    for role in ROLES:
        mac = macos["variants"][role]["report"]
        arm = linux["variants"][role]
        require(
            mac["go_oracle_sha256"]
            == macos_manifest["variants"][role]["mainnet_oracle_sha256"]
            and arm["go_oracle_sha256"]
            == linux["runtime"]["oracle_sha256"][role + "-mainnet"]
            and mac["build"] == macos_manifest["variants"][role]["linked"]
            and arm["build"] == linux["runtime"]["linked"][role],
            "candidate oracle provenance differs",
        )
        for report in (mac, arm):
            require(
                report["kind"] == "first-prospective-unsigned-candidate"
                and report["native_variant"] == role
                and report["parent_height"] == result["parent_height"]
                and report["containing_height"] == result["parent_height"] + 1
                and report["transactions"] == result["candidate_transactions"]
                and report["confiscations_requiring_signed_authorization"]
                == confiscations,
                "oracle input context differs from the prospective decision",
            )
        agree = (
            all(v == mac.get(k) for k, v in arm.items() if k != "go_oracle_sha256")
            and mac.keys() == arm.keys()
        )
        decisions[role] = {
            "macos_matches_linux": agree,
            "macos_oracle_sha256": mac["go_oracle_sha256"],
            "linux_oracle_sha256": arm["go_oracle_sha256"],
            "ordinary_policy_accepted": arm["ordinary_policy_accepted"],
            "ordinary_consensus_accepted": arm["ordinary_consensus_accepted"],
            "ordinary_checks_passed": macos["variants"][role]["exit"] == 0
            and agree
            and not arm["ordinary_rejections"]
            and arm["ordinary_policy_accepted"]
            == arm["ordinary_consensus_accepted"]
            == ordinary,
        }
    return {
        "schema": 1,
        "private_candidate_receipt_sha256": sha256,
        "confirmed_parent": result["parent_hash"],
        "parent_height": result["parent_height"],
        "candidate_height": result["parent_height"] + 1,
        "actual_successor": candidate["successor"]["hash"],
        "candidate_transactions": result["candidate_transactions"],
        "candidate_fees_satoshis": result["candidate_fee_satoshis"],
        "cw144_bits": result["cw144_bits"],
        "runtime": linux["runtime"],
        "variants": decisions,
        "confiscations_requiring_signed_authorization": confiscations,
        "ordinary_validation_passed": all(
            v["ordinary_checks_passed"] for v in decisions.values()
        ),
    }


def origin_in_journal(journal, decision):
    current = None
    with journal.open() as stream:
        for line in stream:
            if not line.endswith("\n"):
                break
            event = json.loads(line)
            if event.get("event") == "start":
                current = event["executable_sha256"]
            elif event.get("event", "tip") == "tip" and (
                event.get("unsigned_candidate") or {}
            ).get("transactions"):
                require(
                    current
                    and len(current) == 64
                    and len(bytes.fromhex(current)) == 32
                    and event["hash"] == decision["hash"]
                    and event["height"] == decision["height"]
                    and event["observed_at"] == decision["observed_at"]
                    and event["unsigned_candidate"] == decision["unsigned_candidate"],
                    "first unsigned journal decision differs from the original private receipt",
                )
                return current
    raise ValueError("station journal has no first eligible unsigned candidate")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("candidate", type=Path, help="complete private first.json.gz")
    p.add_argument(
        "macos", type=Path, help="paired macOS replay receipt for these same bytes"
    )
    p.add_argument(
        "oracles", type=Path, help="four frozen Linux oracle binaries and manifest"
    )
    p.add_argument("output", type=Path, help="sanitized summary")
    p.add_argument(
        "--release", type=Path, default=EVIDENCE / "chronicle-waiting-linux-build.json"
    )
    p.add_argument(
        "--linux-manifest",
        type=Path,
        default=EVIDENCE / "chronicle-go-native-linux-arm64.json",
    )
    p.add_argument(
        "--macos-manifest",
        type=Path,
        default=EVIDENCE / "chronicle-go-native-macos.json",
    )
    p.add_argument(
        "--journal",
        type=Path,
        help="contemporaneous station journal identifying the executable that issued the first candidate",
    )
    p.add_argument(
        "--private-decisions",
        type=Path,
        help="optional local full oracle results, which can identify unconfirmed payments",
    )
    a = p.parse_args()
    packed = a.candidate.read_bytes()
    candidate_sha = hashlib.sha256(packed).hexdigest()
    candidate = json.loads(gzip.decompress(packed))
    del packed
    require(
        assess(candidate)["complete"],
        "waiting for the originally observed successor and funding proofs",
    )
    folder = a.oracles.resolve()
    release = json.loads(a.release.read_text())
    manifest = json.loads(a.linux_manifest.read_text())
    require(
        manifest == json.loads((folder / "manifest.json").read_text()),
        "installed oracles differ from the selected Linux archive build",
    )
    macos = json.loads(a.macos.read_text())
    macos_manifest = json.loads(a.macos_manifest.read_text())
    require(
        macos["evidence_sha256"] == candidate_sha,
        "macOS replay checked different bytes",
    )
    with isolated_oracles(release, manifest, folder, release["image_id"]) as (
        runtime,
        command,
    ):
        linux = {"runtime": runtime, "variants": {}}
        for role in ROLES:
            binary = folder / (role + "-mainnet")
            linux["variants"][role] = replay(candidate, binary, role, command(binary))
    if a.private_decisions:
        atomic_write(a.private_decisions, (json.dumps(linux, indent=2) + "\n").encode())
    result = checked_summary(
        candidate, macos, macos_manifest, linux, release, manifest, candidate_sha
    )
    result["station_executable_at_clock_sample"] = candidate["clock"]["release"]
    if a.journal:
        result["originating_station_executable_sha256"] = origin_in_journal(
            a.journal, candidate["decision"]
        )
    atomic_write(a.output, (json.dumps(result, indent=2) + "\n").encode())
    return 0 if result["ordinary_validation_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
