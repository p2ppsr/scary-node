"""Restore the large-block witness checkpoint and replay the published chain on a fresh stock node."""

import argparse
import gzip
import hashlib
import json
from pathlib import Path

from live_cadence_run import (
    ELF,
    FUNDING,
    NODES,
    PROJECT,
    compose,
    docker,
    plain_tips,
    session_lock,
    status,
    sv,
)

# Configure the isolated project before importing clients that freeze their ports.
# isort: split
from cadence_transport import offer, read_stock
from live_cadence_body import COUNT, HEIGHT, check_header, dsha, require
from mixed_corpus import path_at
from prove import decode_block, wait
from sv_reference import IMAGE, Reference
from verify_live_cadence import EVIDENCE, check, read


def hash_header(raw):
    return dsha(raw[:80])[::-1].hex()


def capture(directory):
    require(NODES == [(23441, 23451), (23442, 23452)], "unexpected cadence endpoints")
    result = json.loads((directory / "result.json").read_bytes())
    require(
        result["completed"] and result["executable_sha256"] == ELF,
        "large-block measurement incomplete",
    )
    selected = hash_header(bytes.fromhex(result["successors"][-1]))
    containers = json.loads(
        "["
        + ",".join(
            docker(
                "ps",
                "-a",
                "--filter",
                "label=com.docker.compose.project=" + PROJECT,
                "--format",
                "{{json .}}",
            ).splitlines()
        )
        + "]"
    )
    require(
        len(containers) == 8 and all("(Paused)" in c["Status"] for c in containers),
        "expected the eight paused services of the closed cadence run",
    )

    def snapshot():
        m = status()["mining"]
        return {
            k: m[k]
            for k in (
                "height",
                "tip_hash",
                "coverage_start",
                "coverage_end",
                "witnessed_outputs",
                "window_chainwork",
                "durable_directive_sequence",
                "validation_window",
            )
        }

    compose("unpause")
    try:
        wait(
            "closed four-node settlement",
            lambda: set(plain_tips().values()) == {selected},
        )
        before = snapshot()
        wires = []
        previous = result["fixture"]["block_hash"]
        for h in result["successors"]:
            header = bytes.fromhex(h)
            ident = hash_header(header)
            check_header(header, ident, previous)
            raw = bytes.fromhex(sv("getblock", ident, 0))
            require(raw[:80] == header, "stock returned a different successor")
            decode_block(raw, ident)
            wires.append(raw.hex())
            previous = ident
        require(
            int(sv("getmempoolinfo")["size"]) == 0,
            "stock has outstanding wallet transactions",
        )
        service = compose("ps", "-q", "scary-node")
        started_before = docker("inspect", "--format", "{{.State.StartedAt}}", service)
        compose("restart", "--timeout", "45", "scary-node")
        wait(
            "checkpoint restore and stock reconnection",
            lambda: (
                status()["mining"]["tip_hash"] == selected
                and status()["mining"]["connected_data_peers"] > 0
            ),
            seconds=240,
        )
        started_after = docker("inspect", "--format", "{{.State.StartedAt}}", service)
        after = snapshot()
        require(
            started_before != started_after and before == after,
            "release or witnessed boundary changed on restore",
        )
        require(
            status()["release"]["executable_sha256"] == ELF
            and set(plain_tips().values()) == {selected},
            "restored project or release differs",
        )
        require(
            any(
                c["hash"] == result["fixture"]["block_hash"]
                and c["script_checks_passed"] == COUNT
                for c in after["validation_window"]
            ),
            "restored block validation record is missing",
        )
        return {
            "project": PROJECT,
            "release": ELF,
            "fixture_hash": result["fixture"]["block_hash"],
            "successor_hex": wires,
            "reference_mempool_transactions": 0,
            "before": before,
            "restored": after,
            "container_started_before": started_before,
            "container_started_after": started_after,
        }
    finally:
        compose("pause")


def replay(history, corpus, body, receipt):
    source = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "docs/evidence/chronicle-live-cadence-body.json"
        ).read_bytes()
    )
    require(
        receipt["release"] == ELF and receipt["fixture_hash"] == source["block_hash"],
        "unexpected recovery receipt",
    )
    with body.open("rb") as stream:
        require(
            hashlib.file_digest(stream, "sha256").hexdigest() == source["wire_sha256"],
            "different large body",
        )
    # The temporary independent store is sized for this single replay. The timed
    # four-store trial used two disk-backed Aerospike namespaces.
    with Reference().running(
        peer=True, fast_rpc=True, block_bytes=4_000_000_000, tmpfs_bytes=4 << 30
    ) as node:
        for h in range(1, HEIGHT):
            path = path_at(history, h) if h < HEIGHT - 1 else corpus / "funding.block"
            require(
                node.rpc("submitblock", path.read_bytes().hex()) is None,
                f"stock rejected funding history at {h}",
            )
        require(
            node.rpc("getbestblockhash") == FUNDING,
            "incorrect independently replayed parent",
        )
        port = int(
            node.docker("port", node.container, "18444/tcp")
            .splitlines()[0]
            .rsplit(":", 1)[1]
        )
        events = []
        offer(body, source["wire_sha256"], port, HEIGHT, events.append)
        wait(
            "independent stock selection",
            lambda: node.rpc("getbestblockhash") == source["block_hash"],
            seconds=600,
        )
        digest = read_stock(
            port,
            HEIGHT,
            source["block_hash"],
            source["wire_bytes"],
            source["wire_sha256"],
        )
        previous = source["block_hash"]
        for item in receipt["successor_hex"]:
            raw = bytes.fromhex(item)
            ident = hash_header(raw)
            check_header(raw[:80], ident, previous)
            decode_block(raw, ident)
            require(
                node.rpc("submitblock", item) is None
                and node.rpc("getbestblockhash") == ident,
                "independent reference did not extend the fixture",
            )
            previous = ident
        require(
            previous == receipt["restored"]["tip_hash"]
            and node.rpc("getblockcount") == HEIGHT + 2,
            "fresh stock replay and restored entrant disagree",
        )
        return {
            "reference": IMAGE,
            "history_blocks_before_fixture": HEIGHT - 1,
            "selected_fixture": source["block_hash"],
            "native_read_sha256": digest,
            "native_events": events,
            "final_height": HEIGHT + 2,
            "final_hash": previous,
        }


def verify(checkpoint, fresh, measured):
    result = measured["result"]
    require(
        check(result, measured["measurement_events"])["cadence_target_met"],
        "original four-node measurement differs",
    )
    boundary = checkpoint["restored"]
    require(
        checkpoint["before"] == boundary
        and checkpoint["container_started_before"]
        < checkpoint["container_started_after"]
        and checkpoint["project"] == result["project"]
        and checkpoint["release"] == result["executable_sha256"]
        and checkpoint["reference_mempool_transactions"] == 0,
        "process or checkpoint recovery differs",
    )
    fixture = result["fixture"]
    require(
        checkpoint["fixture_hash"] == fresh["selected_fixture"] == fixture["block_hash"]
        and fresh["reference"] == IMAGE
        and fresh["native_read_sha256"] == fixture["wire_sha256"]
        and fresh["history_blocks_before_fixture"] == HEIGHT - 1
        and fresh["native_events"]
        == ["ready_to_announce", "stock_requested", "body_written", "stock_pong"],
        "independent stock replay differs",
    )
    previous = fixture["block_hash"]
    require(
        len(checkpoint["successor_hex"]) == len(result["successors"]) == 2,
        "missing original successor",
    )
    for wire, h in zip(checkpoint["successor_hex"], result["successors"], strict=True):
        raw = bytes.fromhex(wire)
        ident = hash_header(raw)
        require(raw[:80].hex() == h, "successor header differs")
        check_header(raw[:80], ident, previous)
        decode_block(raw, ident)
        previous = ident
    require(
        fresh["final_hash"] == previous == boundary["tip_hash"]
        and fresh["final_height"]
        == boundary["height"]
        == boundary["coverage_end"]
        == HEIGHT + 2,
        "independent final tips differ",
    )
    window = boundary["validation_window"]
    require(
        [row["height"] for row in window]
        == list(range(boundary["coverage_start"], HEIGHT + 3))
        and result["witnessed_validation"] in window
        and boundary["witnessed_outputs"] > 0,
        "restored witnessed coverage differs",
    )
    return {
        "restored_outputs": boundary["witnessed_outputs"],
        "coverage_start": boundary["coverage_start"],
        "coverage_end": boundary["coverage_end"],
        "independent_final_hash": previous,
        "stock_body_sha256": fresh["native_read_sha256"],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="action", required=True)
    a = subs.add_parser("capture")
    a.add_argument("directory", type=Path)
    b = subs.add_parser("replay")
    for name in ("history", "corpus", "body", "checkpoint"):
        b.add_argument(name, type=Path)
    for s in (a, b):
        s.add_argument("output", type=Path)
    v = subs.add_parser("verify")
    v.add_argument(
        "checkpoint", type=Path, help="checkpoint or exported combined receipt"
    )
    v.add_argument("fresh_stock", type=Path, nargs="?")
    v.add_argument(
        "--measured",
        type=Path,
        default=EVIDENCE / "chronicle-live-cadence-result.json.gz",
    )
    v.add_argument("--export", type=Path, help="a fresh combined .json.gz receipt")
    args = parser.parse_args()
    if args.action == "verify":
        recorded = read(args.checkpoint)
        bundle = (
            {"checkpoint": recorded, "stock": read(args.fresh_stock)}
            if args.fresh_stock
            else recorded
        )
        verified = verify(bundle["checkpoint"], bundle["stock"], read(args.measured))
        if args.export:
            with args.export.open("xb") as out:
                out.write(
                    gzip.compress(
                        json.dumps(
                            bundle, separators=(",", ":"), allow_nan=False
                        ).encode(),
                        mtime=0,
                    )
                )
        print(json.dumps(verified, indent=2))
        raise SystemExit(0)
    require(not args.output.exists(), "choose a fresh output")
    with session_lock():
        evidence = (
            capture(args.directory)
            if args.action == "capture"
            else replay(
                args.history,
                args.corpus,
                args.body,
                json.loads(args.checkpoint.read_bytes()),
            )
        )
    with args.output.open("x") as out:
        json.dump(evidence, out, indent=2)
        out.write("\n")
