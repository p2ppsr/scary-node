"""Pause the completed direct-wallet regtest and record its stock queue at the cohort handoff."""

import argparse
import gzip
import hashlib
import json
import subprocess
import time
from pathlib import Path

from sustained_stock import snapshot
from verify_sustained_native import REGISTRATION, REPLICATION, stock_activity, verify

PROJECT = "scary-node-throughput"
PROFILES = {
    REGISTRATION: (PROJECT, 2000),
    REPLICATION: ("scary-node-sustained-production", 3000),
}
SERVICES = {
    "svref",
    "tn1",
    "tn2",
    "scary-node",
    "as1",
    "as2",
    "ingress",
    "wallet-ingress",
}


def quiesce(directory):
    from prove import request, rpc

    def docker(*args):
        return subprocess.check_output(["docker", *args], text=True).strip()

    output = directory / "quiesced.json"
    if output.exists():
        raise ValueError("handoff already captured")
    receipt = directory / "receipt.json.gz"
    while not receipt.exists():
        time.sleep(0.2)
    first = time.time_ns()
    with gzip.open(receipt, "rt") as f:
        result = json.load(f)
    if result["registration"] not in PROFILES:
        raise ValueError("unknown sustained trial")
    project, shift = PROFILES[result["registration"]]
    summary = verify(result)
    if not result["completion"]["primary_complete"] and not (
        result["completion"]["resource_stop"] or result["completion"]["audit_failure"]
    ):
        raise ValueError(
            "direct-wallet cohort is still incomplete without terminal closure"
        )
    services = docker(
        "ps",
        "--filter",
        f"label=com.docker.compose.project={project}",
        "--format",
        '{{.Label "com.docker.compose.service"}} {{.ID}}',
    )
    enrolled = dict(line.split() for line in services.splitlines())
    if set(enrolled) != SERVICES:
        raise ValueError(
            "expected precisely the eight independent direct-wallet containers"
        )
    bindings = json.loads(docker("inspect", enrolled["ingress"]))[0]["NetworkSettings"][
        "Ports"
    ]
    query_ports = {
        role: port + shift
        for role, port in (("tn1", 19441), ("tn2", 19442), ("scary", 19480))
    }
    for internal, external in (
        (19441, query_ports["tn1"]),
        (19442, query_ports["tn2"]),
        (19480, query_ports["scary"]),
    ):
        if bindings.get(f"{internal}/tcp") != [
            {"HostIp": "127.0.0.1", "HostPort": str(external)}
        ]:
            raise ValueError("loopback port is outside the enrolled project's ingress")

    def sv(method):
        return json.loads(
            docker(
                "exec",
                enrolled["svref"],
                "bitcoin-cli",
                "-regtest",
                "-datadir=/data",
                method,
            )
        )

    final = snapshot(sv)
    tips = {}
    for name, observe in [
        ("sv", lambda: sv("getblockchaininfo")),
        ("tn1", lambda: rpc((query_ports["tn1"], 19451 + shift), "getblockchaininfo")),
        ("tn2", lambda: rpc((query_ports["tn2"], 19452 + shift), "getblockchaininfo")),
    ]:
        info = observe()
        tips[name] = {
            "height": info["blocks"],
            "hash": info["bestblockhash"],
            "wall_ns": time.time_ns(),
        }
    mining = json.loads(request(f"http://127.0.0.1:{query_ports['scary']}/status"))[
        "mining"
    ]
    tips["scary"] = {
        "height": mining["height"],
        "hash": mining["tip_hash"],
        "wall_ns": time.time_ns(),
    }
    suffix = [
        {
            k: obs[k]
            for k in (
                "height",
                "hash",
                "noncoinbase_transactions",
                "known_inputs",
                "unknown_inputs",
                "script_checks_passed",
            )
        }
        for obs in mining["validation_window"]
        if obs["height"] > result["completion"]["final_height"]
    ]
    started = time.time_ns()
    subprocess.run(
        ["docker", "pause", *enrolled.values()], check=True, stdout=subprocess.DEVNULL
    )
    paused = {
        name: docker("inspect", "--format", "{{.State.Paused}}", container) == "true"
        for name, container in enrolled.items()
    }
    if not all(paused.values()):
        raise RuntimeError("incomplete measured-regtest quiescence")
    record = {
        "registration": result["registration"],
        "project": project,
        "query_ports_verified_against_enrolled_ingress": query_ports,
        "final_measured_sample": {
            role: {
                key: result["samples"][-1][role].get(key)
                for key in ("height", "hash", "read_ns")
            }
            for role in ("sv", "tn1", "tn2", "scary")
        },
        "receipt_seen_wall_ns": first,
        "first_scheduled_offer_wall_ns": result["start_wall_ns"],
        "observation_duration_ns": result["duration_ns"],
        "audited_blocks": len(result["blocks"]),
        "confirmed_payments": summary["distinct_confirmed"],
        "post_window_stock_observation": final,
        "post_window_selected_tips": tips,
        "post_window_entrant_observed_above_audited_prefix": suffix,
        "pause_started_wall_ns": started,
        "pause_verified_wall_ns": time.time_ns(),
        "paused_services": paused,
    }
    tmp = directory / "quiesced.next"
    tmp.write_text(json.dumps(record, indent=2) + "\n")
    tmp.replace(output)
    return record


def verify_frozen(path, record, receipt, handoff, marker):
    frozen = json.loads(path.read_text())
    for source, key in (
        (receipt, "direct_wallet_receipt_sha256"),
        (handoff, "original_quiescence_sha256"),
    ):
        with source.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != frozen[key]:
                raise ValueError(
                    "frozen-store supplement names different evidence bytes"
                )
    expected = (
        record["completion"]["final_height"],
        record["completion"]["final_hash"],
    )
    sample = record["samples"][-1]
    if (
        frozen["enrolled_project"] != PROJECT
        or frozen["registration"] != record["registration"]
        or frozen["sampler_last_clock_ns"] != sample["elapsed_ns"]
        or frozen["stock_post_window_tip"] != marker["post_window_selected_tips"]["sv"]
    ):
        raise ValueError(
            "frozen-store supplement differs from the enrolled project and final stock sample"
        )
    for name in ("sv", "tn1", "tn2", "scary"):
        view = frozen["last_scheduled_sample"][name]
        if (
            view != {k: sample[name][k] for k in ("height", "hash", "read_ns")}
            or (view["height"], view["hash"]) != expected
        ):
            raise ValueError(
                "independent final native sample differs from the audited chain"
            )
    for name, volume in (
        ("tn1", "tn1-data"),
        ("tn2", "tn2-data"),
        ("scary", "scary-data"),
    ):
        v = frozen["frozen_stores"][name]
        if (
            (v["height"], v["hash"]) != expected
            or v["volume"] != PROJECT + "_" + volume
            or v.get("invalid", False)
        ):
            raise ValueError("persisted store did not corroborate the final chain")
    node = frozen["frozen_stores"]["scary"]
    if (
        node["header_hex"] != record["blocks"][-1]["header_hex"]
        or len(bytes.fromhex(node["verified_checkpoint_payload_sha256"])) != 32
    ):
        raise ValueError(
            "frozen entrant checkpoint lacks the canonical header or digest"
        )
    return {
        "height": expected[0],
        "hash": expected[1],
        "corroborating_frozen_volumes": 3,
        "excluded_original_fields": frozen["excluded_optional_fields"],
    }


def verify_handoff(receipt, stock_log, handoff, frozen=None):
    def read(path):
        return gzip.open(path, "rt") if path.suffix == ".gz" else path.open()

    with read(receipt) as f:
        result = json.load(f)
    with read(handoff) as f:
        marker = json.load(f)
    if stock_log:
        with read(stock_log) as f:
            rows = [json.loads(line) for line in f]
    else:
        rows = result.get("stock", [])
    summary = verify(result)
    start = result["start_wall_ns"]
    final = marker["post_window_stock_observation"]
    times = [
        start + result["duration_ns"],
        marker["receipt_seen_wall_ns"],
        final["wall_ns"],
        final["finished_wall_ns"],
        marker["pause_started_wall_ns"],
        marker["pause_verified_wall_ns"],
    ]
    if times != sorted(times):
        raise ValueError("handoff precedes the primary completion boundary")
    if not (
        marker["registration"] == result["registration"]
        and result["registration"] in PROFILES
        and marker["project"] == PROFILES[result["registration"]][0]
        and marker["paused_services"] == dict.fromkeys(SERVICES, True)
        and marker["confirmed_payments"] == summary["distinct_confirmed"]
        and marker["audited_blocks"] == len(result["blocks"])
        and marker["observation_duration_ns"] == result["duration_ns"]
        and marker["first_scheduled_offer_wall_ns"] == start
    ):
        raise ValueError("queued stock work or closed primary identity differs")
    if not rows or any(b["wall_ns"] < a["wall_ns"] for a, b in zip(rows, rows[1:])):
        raise ValueError("stock gauges missing or out of order")
    evidence = {
        "stock_samples": len(rows),
        "sampled_stock": stock_activity(rows, start),
        "final_stock_before_pause": stock_activity(
            [{**final, "elapsed_ns": final["wall_ns"] - start}], start
        ),
        "duration_through_primary_closure_s": result["duration_ns"] / 1e9,
        "seconds_between_closure_and_verified_pause": (times[-1] - times[0]) / 1e9,
        "paused_services": sorted(SERVICES),
    }

    if result["registration"] == REPLICATION:
        roles = ("sv", "tn1", "tn2", "scary")
        last = {
            role: {
                k: result["samples"][-1][role].get(k)
                for k in ("height", "hash", "read_ns")
            }
            for role in roles
        }
        if marker["final_measured_sample"] != last or marker[
            "query_ports_verified_against_enrolled_ingress"
        ] != {"tn1": 22441, "tn2": 22442, "scary": 22480}:
            raise ValueError(
                "inventory handoff does not match its measured sample or enrolled ports"
            )
        expected = (
            result["completion"]["final_height"],
            result["completion"]["final_hash"],
        )
        tips = marker["post_window_selected_tips"]
        if any(
            not (
                final["finished_wall_ns"]
                <= tips[role]["wall_ns"]
                <= marker["pause_started_wall_ns"]
            )
            for role in roles
        ):
            raise ValueError("final receiver read outside the post-window handoff")
        evidence["post_window_matches_audited_tip"] = {
            role: (tips[role]["height"], tips[role]["hash"]) == expected
            for role in roles
        }
    if frozen:
        evidence["supplement"] = verify_frozen(frozen, result, receipt, handoff, marker)
    return evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        type=Path,
        help="live run directory, or a closed receipt for offline verification",
    )
    parser.add_argument(
        "--stock-log",
        type=Path,
        help="independent stock JSONL or gzip for offline verification",
    )
    parser.add_argument(
        "--handoff", type=Path, help="closed handoff JSON for offline verification"
    )
    parser.add_argument(
        "--frozen",
        type=Path,
        help="independent final-store correction for the published direct-wallet handoff",
    )
    args = parser.parse_args()
    if args.stock_log or args.handoff or args.frozen:
        if not args.handoff:
            parser.error("offline verification requires --handoff")
        result = verify_handoff(args.path, args.stock_log, args.handoff, args.frozen)
    else:
        result = quiesce(args.path)
    print(json.dumps(result, indent=2))
