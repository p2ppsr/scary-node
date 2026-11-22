"""Restore the reordered mining checkpoint in a released Linux node, restart and settle native work."""

import argparse
import contextlib
import hashlib
import io
import ipaddress
import json
import os
import struct
import subprocess
import tempfile
import time
import uuid
from decimal import Decimal
from pathlib import Path

from shadow_native import inputs
from sv_reference import IMAGE, Reference, RpcError
from verify_native_workers import REGTEST_TARGET, build_candidate, dsha, require
from verify_peer_intake import verify_p2pk_signature

SOURCE = "a69433bf4d43202b6155561ec4ec0b18aa4d6ce3"
ARCHIVE = "60f769062ff7c6969bc41f1edad1cfed5e842a550255c1fca736743e440a752e"
PARENT = "5754c4a0292ba4c9ea34bb59f721b7b28e39d5806300681823f305890e8783b4"
PROBE = (
    "busybox@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662"
)
PAYOUT = "76a914751e76e8199196d454941c45d1b3a323f1433bd688ac"
STATUS = (
    "height",
    "tip_hash",
    "headers",
    "witnessed_outputs",
    "waiting_for_successor",
    "pending_transactions",
    "solved_blocks",
    "durable_directive_sequence",
    "recoverable_blocks",
    "connected_data_peers",
    "job_preconditions_met",
    "tip_confirmed_since_disconnect",
    "storage_failure",
)


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


def isolated_network(name):
    ids = docker("network", "ls", "-q").splitlines()
    used = [
        ipaddress.ip_network(cfg["Subnet"], strict=False)
        for net in json.loads(docker("network", "inspect", *ids))
        for cfg in net["IPAM"].get("Config") or []
        if cfg.get("Subnet")
    ]
    for subnet in ipaddress.ip_network("198.18.0.0/15").subnets(new_prefix=28):
        if not any(n.version == 4 and n.overlaps(subnet) for n in used):
            docker("network", "create", "--internal", "--subnet", str(subnet), name)
            return
    raise RuntimeError("no unused lab subnet")


def select_payment(source):
    import msgpack
    from sustained_corpus import locking, owner, signed

    keys = {locking(owner(i)): owner(i) for i in range(8)}
    with source.open("rb") as f:
        f.seek(8)
        unpack = msgpack.Unpacker(
            f, use_list=False, strict_map_key=False, max_buffer_size=1 << 30
        )
        for _ in range(unpack.read_map_header()):
            if unpack.unpack() != "coins":
                unpack.skip()
                continue
            for _ in range(unpack.read_map_header()):
                (h, index), coin = unpack.unpack(), unpack.unpack()
                script = bytes(coin["script"])
                if (
                    script in keys
                    and coin["value"] > 2000
                    and not coin["coinbase"]
                    and not coin["confiscation"]
                    and coin["height"] < 580
                ):
                    prev = bytes(h)
                    raw = signed(
                        prev + struct.pack("<I", index),
                        coin["value"],
                        script,
                        keys[script],
                        [(coin["value"] - 1000, script)],
                    )
                    return {
                        "txid": prev[::-1].hex(),
                        "index": index,
                        "value": coin["value"],
                        "height": coin["height"],
                        "script": script.hex(),
                    }, raw
            break
    raise ValueError("archived entrant lacks a witnessed funded lab output")


def thaw_stock(stock, frozen, network, releases, workload, blocks):
    stock.container = releases + "-sv"
    # Rebuild the independent full-state database on RAM-backed scratch. The enrolled
    # volume supplies immutable blk files and stays read-only for the whole run.
    docker(
        "run",
        "-d",
        "--name",
        stock.container,
        "--platform",
        "linux/amd64",
        "--network",
        network,
        "--tmpfs",
        "/data:size=8589934592",
        "--mount",
        f"type=volume,source={frozen},target=/frozen,readonly",
        "--entrypoint",
        "sh",
        IMAGE,
        "-ec",
        'mkdir -p /data/regtest/blocks; cp /frozen/regtest/blocks/blk*.dat /data/regtest/blocks/; exec bitcoind "$@"',
        "stock",
        "-regtest",
        "-datadir=/data",
        "-reindex=1",
        "-listen=1",
        "-connect=0",
        "-discover=0",
        "-dnsseed=0",
        "-server=1",
        "-txindex=1",
        "-minminingtxfee=0.00000500",
        "-genesisactivationheight=1",
        "-chronicleactivationheight=2",
        "-excessiveblocksize=4000000000",
        "-maxstackmemoryusageconsensus=100000000",
        "-checkmempool=0",
        "-checkblockindex=0",
    )
    start = time.monotonic()
    tip = None
    last = start
    while time.monotonic() - start < 1200:
        try:
            current = stock.rpc("getbestblockhash")
            height = stock.rpc("getblockcount")
        except RpcError:
            time.sleep(1)
            continue
        if current == PARENT:
            return {"method": "stock reindex of original block files", "reoffered": 0}
        if current != tip:
            tip = current
            last = time.monotonic()
        if height >= 484 and time.monotonic() - last > 40:
            count = 0
            for b in workload["blocks"]:
                if b["height"] <= height:
                    continue
                raw = (blocks / (b["hash"] + ".block")).read_bytes()
                require(
                    dsha(raw[:80])[::-1].hex() == b["hash"]
                    and hashlib.sha256(raw).hexdigest() == b["wire_sha256"]
                    and raw[4:36][::-1].hex() == current,
                    "stock suffix provenance",
                )
                require(
                    stock.rpc("submitblock", raw.hex()) is None
                    and stock.rpc("getbestblockhash") == b["hash"],
                    "stock suffix acceptance",
                )
                current = b["hash"]
                count += 1
            require(current == PARENT, "wrong reconstructed stock parent")
            return {
                "method": "stock reindex plus exact original selected suffix",
                "reoffered": count,
            }
        time.sleep(1)
    raise RuntimeError("stock did not restore the original selected parent")


def volume_digest(image, volume):
    return docker(
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--mount",
        f"type=volume,source={volume},target=/saved,readonly",
        "--entrypoint",
        "sha256sum",
        image,
        "/saved/regtest/witness.state",
    ).split()[0]


def check_frozen_owners(volumes):
    owners = set()
    for volume in volumes:
        ids = docker("ps", "-aq", "--filter", f"volume={volume}").splitlines()
        writable = []
        for cid in ids:
            info = json.loads(docker("inspect", cid))[0]
            if any(m.get("Name") == volume and m["RW"] for m in info["Mounts"]):
                writable.append(cid)
                require(
                    not info["State"]["Running"] or info["State"]["Paused"],
                    "a source volume has a live writer",
                )
        require(writable, "source volume has no identifiable enrolled owner")
        owners.update(writable)
    return len(owners)


def copy_volume(image, source, dest, cleanup):
    require(
        source in docker("volume", "ls", "-q").splitlines(), "missing source volume"
    )
    require(
        dest not in docker("volume", "ls", "-q").splitlines(), "scratch already exists"
    )
    docker(
        "volume",
        "create",
        "--label",
        "scary-node.qualification=checkpoint-upgrade",
        dest,
    )
    cleanup.callback(
        subprocess.run, ["docker", "volume", "rm", dest], capture_output=True
    )
    docker(
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--mount",
        f"type=volume,source={source},target=/original,readonly",
        "--mount",
        f"type=volume,source={dest},target=/copy",
        "--entrypoint",
        "cp",
        image,
        "-a",
        "/original/.",
        "/copy/",
    )


def trace_rows(cid):
    return [json.loads(s) for s in docker("logs", cid).splitlines()]


def trace_interval(rows, before):
    events = rows[before:]
    pattern = [
        ("create", "witness.next"),
        ("rename", "witness.state"),
        ("close_write", "witness.state"),
    ]
    require(
        [(v["event"], v["name"]) for v in events] == pattern,
        "incomplete or overlapping checkpoint events",
    )
    times = [v["elapsed_ns"] for v in events]
    require(times[0] < times[1] <= times[2], "non-monotonic filesystem observations")
    return {
        "events": events,
        "create_to_rename_ns": times[1] - times[0],
        "create_to_close_ns": times[2] - times[0],
    }


def run(a):
    from checkpoint_profile import identity
    from native_workers import Worker
    from recover_sustained_chain import read
    from scary_dar import until

    manifest = read(a.release)
    release = {
        "source": manifest["source_commit"],
        "image": manifest["image_id"],
        "linux": manifest["release"]["executable_sha256"],
    }
    require(release["source"] == SOURCE, "unexpected release")
    new = release["image"]
    require(
        docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "sha256sum",
            new,
            "/usr/local/bin/scary-node",
        ).split()[0]
        == release["linux"],
        "wrong released executable",
    )
    source = identity(a.checkpoint.read_bytes())
    require(source["file_sha256"] == ARCHIVE, "unexpected archived checkpoint")
    prev, payment = select_payment(a.checkpoint)
    tx = dsha(payment)[::-1].hex()
    name = "scary-checkpoint-upgrade-" + uuid.uuid4().hex[:10]
    network = name + "-net"
    node = name + "-node"
    probe = name + "-worker"
    stock = Reference()
    data = name + "-data"
    leases = name + "-leases"
    a.output.mkdir(mode=0o700)

    def stage(label, **fields):
        with (a.output / "stages.jsonl").open("a") as f:
            f.write(
                json.dumps({"stage": label, "unix_ns": time.time_ns(), **fields}) + "\n"
            )

    def status():
        try:
            snapshot = subprocess.check_output(
                [
                    "docker",
                    "exec",
                    probe,
                    "wget",
                    "-q",
                    "-T",
                    "6",
                    "-O",
                    "-",
                    f"http://{node}:8080/status",
                ],
                stderr=subprocess.PIPE,
            )
        except subprocess.CalledProcessError as exc:
            if docker("inspect", node, "--format", "{{.State.Status}}") == "exited":
                raise RuntimeError(
                    "entrant exited before status became available"
                ) from exc
            raise OSError("status endpoint unavailable") from exc
        info = json.loads(snapshot)
        require(
            info["network"] == "regtest"
            and info["release"]["executable_sha256"] == release["linux"],
            "different running release",
        )
        m = info["mining"]
        return {k: m[k] for k in STATUS}

    def at(h, **requirements):
        s = status()
        return (
            s
            if s["height"] == h and all(s[k] == v for k, v in requirements.items())
            else False
        )

    with contextlib.ExitStack() as stack:
        originals = (a.stock_volume, a.entrant_volume, a.lease_volume)
        existing = docker("volume", "ls", "-q").splitlines()
        require(all(v in existing for v in originals), "an original volume is missing")
        owner_count = check_frozen_owners(originals)
        copy_volume(new, a.entrant_volume, data, stack)
        copy_volume(new, a.lease_volume, leases, stack)
        stage("copies", scratch_data=data, scratch_leases=leases)
        require(volume_digest(new, data) == ARCHIVE, "copied entrant differs")
        isolated_network(network)
        stack.callback(
            subprocess.run, ["docker", "network", "rm", network], capture_output=True
        )
        stack.callback(
            subprocess.run,
            ["docker", "rm", "-f", "-v", name + "-sv"],
            capture_output=True,
        )
        docker(
            "run", "-d", "--name", probe, "--network", network, PROBE, "sleep", "7200"
        )
        stack.callback(
            subprocess.run, ["docker", "rm", "-f", probe], capture_output=True
        )

        def retain_log(container, filename):
            with (a.output / filename).open("wb") as log:
                subprocess.run(
                    ["docker", "logs", container],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    check=False,
                )

        stack.callback(retain_log, name + "-sv", "stock.log")
        # The Go watcher only reads directory notifications from the scratch volume.
        # This also counts the final close after the writer has synced the directory.
        work = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        observer = work / "observe"
        subprocess.run(
            [
                "go",
                "build",
                "-trimpath",
                "-o",
                observer,
                Path(__file__).with_name("checkpoint_io_observer.go"),
            ],
            check=True,
            env={
                **os.environ,
                "GOOS": "linux",
                "GOARCH": "arm64",
                "CGO_ENABLED": "0",
                "GOMAXPROCS": "2",
            },
        )
        trace = name + "-io"
        docker(
            "run",
            "-d",
            "--name",
            trace,
            "--network",
            "none",
            "--read-only",
            "--mount",
            f"type=volume,source={data},target=/saved,readonly",
            "--mount",
            f"type=bind,source={observer},target=/observe,readonly",
            "--entrypoint",
            "/observe",
            PROBE,
            "/saved/regtest",
        )
        stack.callback(
            subprocess.run, ["docker", "rm", "-f", trace], capture_output=True
        )
        stack.callback(retain_log, trace, "file-events.log")
        until(
            lambda: trace_rows(trace) and trace_rows(trace)[0]["event"] == "ready",
            "read-only checkpoint observer",
            10,
        )
        stock_method = thaw_stock(
            stock, a.stock_volume, network, name, read(a.workload), a.blocks
        )
        require(
            stock.rpc("getblockcount") == 580 and stock.rpc("getconnectioncount") == 0,
            "independent initial stock boundary",
        )
        funding = stock.rpc("getrawtransaction", prev["txid"])
        verify_p2pk_signature(bytes.fromhex(funding), payment)
        coin = stock.rpc("gettxout", prev["txid"], prev["index"])
        require(
            coin["scriptPubKey"]["hex"] == prev["script"]
            and int(Decimal(str(coin["value"])) * 100000000) == prev["value"]
            and coin["confirmations"] == 581 - prev["height"],
            "stock and entrant source output differ",
        )
        require(stock.rpc("setnetworkactive", False) is False, "stock was not detached")
        old_ip = docker(
            "inspect",
            stock.container,
            "--format",
            "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
        )
        stage("stock_ready", **stock_method)
        stack.callback(
            subprocess.run, ["docker", "rm", "-f", "-v", node], capture_output=True
        )
        stack.callback(retain_log, node, "node.log")
        begin = time.monotonic_ns()
        docker(
            "run",
            "-d",
            "--name",
            node,
            "--network",
            network,
            "--mount",
            f"type=volume,source={data},target=/data",
            "--mount",
            f"type=volume,source={leases},target=/var/lib/scary-state",
            "--env",
            "XDG_STATE_HOME=/var/lib/scary-state",
            new,
            "--network",
            "regtest",
            "--sv-peer",
            old_ip + ":18444",
            "--sv-bodies-only",
            "--observe-bodies",
            "--payout-script",
            PAYOUT,
            "--data-dir",
            "/data",
            "--api-bind",
            "0.0.0.0:8080",
            "--stratum-bind",
            "0.0.0.0:3333",
            "--asset-bind",
            "0.0.0.0:9090",
            "--asset-url",
            f"http://{node}:9090/api/v1",
        )
        first = until(
            lambda: at(580, connected_data_peers=0), "initial peerless restore", 150
        )
        restore_ns = time.monotonic_ns() - begin
        stage("initial_restore", **first)
        require(
            stock.rpc("setnetworkactive", True) is True,
            "stock failed to enable peering",
        )
        until(
            lambda: at(580, tip_confirmed_since_disconnect=True),
            "stock reconfirmed archive",
            150,
        )
        require(
            stock.rpc("sendrawtransaction", payment.hex()) == tx,
            "authenticated payment not accepted by stock",
        )
        offered = until(
            lambda: at(580, pending_transactions=1, job_preconditions_met=True),
            "native payment eligible",
            100,
        )
        require(
            stock.rpc("setnetworkactive", False) is False,
            "could not detach stock for restart",
        )
        until(
            lambda: at(580, connected_data_peers=0),
            "disconnect before durable shutdown",
            80,
        )
        prior_events = len(trace_rows(trace))
        begin = time.monotonic_ns()
        docker("stop", "--time", "150", node)
        stop_ns = time.monotonic_ns() - begin
        require(
            docker("inspect", node, "--format", "{{.State.ExitCode}}") == "0",
            "unclean ordinary stop",
        )
        trace_proof = trace_interval(trace_rows(trace), prior_events)
        new_sha = volume_digest(new, data)
        stage("shutdown", checkpoint_sha256=new_sha)
        require(new_sha != ARCHIVE, "successor writer did not save admitted payment")
        begin = time.monotonic_ns()
        docker("start", node)
        restored = until(
            lambda: at(580, connected_data_peers=0, pending_transactions=1),
            "peerless new-writer restart",
            150,
        )
        restart_ns = time.monotonic_ns() - begin
        require(
            stock.rpc("getconnectioncount") == 0,
            "stock connected before restart observation",
        )
        stage("second_restore", **restored)
        require(
            stock.rpc("setnetworkactive", True) is True, "stock peering did not resume"
        )
        until(
            lambda: at(
                580, tip_confirmed_since_disconnect=True, job_preconditions_met=True
            ),
            "reconfirmed restored job",
            160,
        )
        with (
            subprocess.Popen(
                ["docker", "exec", "-i", probe, "nc", "-w", "20", node, "3333"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                bufsize=0,
            ) as tunnel,
            io.BufferedRWPair(tunnel.stdout, tunnel.stdin) as stream,
        ):
            worker = Worker(stream, "checkpoint-upgrade")
            setup = worker.setup("")
            job = setup["first_job"]
            x2 = "00" * setup["extranonce2_bytes"]
            prefix, body = build_candidate(job, setup["extranonce1"], x2, [payment])
            nonce = next(
                n
                for n in range(8192)
                if int.from_bytes(dsha(prefix + struct.pack("<I", n)), "little")
                <= REGTEST_TARGET
            )
            header = prefix + struct.pack("<I", nonce)
            win = dsha(header)[::-1].hex()
            before_submit = time.monotonic_ns()
            ack = worker.submit(70, job, x2, nonce)
            worker_elapsed = time.monotonic_ns() - before_submit
            require(ack == {"result": True, "error": None}, "worker submission failed")
        until(
            lambda: stock.rpc("getbestblockhash") == win,
            "stock selected exact solution",
            150,
        )
        stock_observed = time.monotonic_ns() - before_submit
        wire = bytes.fromhex(stock.rpc("getblock", win, 0))
        require(wire == header + body, "independent stock bytes differ")
        successor = stock.rpc("generate", 1)[0]
        settled = until(
            lambda: at(582, tip_hash=successor),
            "new release followed the stock successor",
            150,
        )
        desc = stock.rpc("getblockheader", successor, False)
        docker("stop", "--time", "150", node)
        end_sha = volume_digest(new, data)
        require(
            check_frozen_owners(originals) == owner_count
            and volume_digest(new, a.entrant_volume) == ARCHIVE,
            "original custody changed",
        )
        result = {
            "source_writer_count": owner_count,
            "schema": "scary-checkpoint-upgrade-v1",
            "source_commit": SOURCE,
            "release_binary_sha256": release["linux"],
            "image": new,
            "stock_image": IMAGE,
            "worker_transport_image": PROBE,
            "stock_restore": stock_method,
            "original": source,
            "initial": first,
            "offered": offered,
            "reopened": restored,
            "settled": settled,
            "initial_restore_ns": restore_ns,
            "clean_shutdown_ns": stop_ns,
            "shutdown_file_events": trace_proof,
            "peerless_restart_ns": restart_ns,
            "worker_roundtrip_ns": worker_elapsed,
            "stock_selected_by_ns_from_submit": stock_observed,
            "original_parent": PARENT,
            "after_stop_sha256": new_sha,
            "final_checkpoint_sha256": end_sha,
            "payment_input": prev,
            "funding_hex": funding,
            "payment_hex": payment.hex(),
            "payment_txid": tx,
            "worker": setup,
            "extranonce2": x2,
            "nonce": nonce,
            "worker_ack": ack,
            "selected_wire": wire.hex(),
            "stock_successor_header": desc,
        }
        verify(result)
        (a.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        stage("complete")


def verify(r):
    require(__debug__, "run the signature verifier with Python assertions enabled")
    evidence = Path(__file__).resolve().parents[1] / "docs/evidence"
    build = json.loads((evidence / "chronicle-buffered-linux-build.json").read_text())
    require(
        r["schema"] == "scary-checkpoint-upgrade-v1"
        and r["source_commit"] == SOURCE
        and r["original"]["file_sha256"] == ARCHIVE
        and r["original_parent"] == PARENT
        and r["release_binary_sha256"] == build["release"]["executable_sha256"]
        and r["image"] == build["image_id"]
        and build["source_commit"] == SOURCE
        and r["stock_image"] == IMAGE
        and r["worker_transport_image"] == PROBE
        and r["original"]["size"] == 316_588_547
        and r["original"]["payload_sha256"]
        == "a46c75038049c8194ce235e030bd0c515f955aa8036d88b62256232a01a72a82",
        "unexpected build, archive or original boundary",
    )
    observed = r["shutdown_file_events"]
    require(
        observed == trace_interval(observed["events"], 0)
        and observed["create_to_close_ns"] <= r["clean_shutdown_ns"],
        "filesystem and stop timings disagree",
    )
    parent = r["initial"]
    restored = r["reopened"]
    offer = r["offered"]
    settled = r["settled"]
    require(
        all(
            s["height"] == 580 and s["tip_hash"] == PARENT
            for s in (parent, restored, offer)
        )
        and parent["pending_transactions"] == 0
        and parent["solved_blocks"] == 56
        and parent["connected_data_peers"] == restored["connected_data_peers"] == 0
        and not parent["job_preconditions_met"]
        and not restored["job_preconditions_met"]
        and not parent["tip_confirmed_since_disconnect"]
        and not restored["tip_confirmed_since_disconnect"]
        and restored["pending_transactions"] == 1
        and offer["pending_transactions"] == 1
        and offer["connected_data_peers"] > 0
        and offer["tip_confirmed_since_disconnect"]
        and offer["job_preconditions_met"],
        "wrong original or peerless admission boundary",
    )
    require(
        all(s["storage_failure"] is None for s in (parent, restored, offer, settled))
        and len(
            {
                s["durable_directive_sequence"]
                for s in (parent, restored, offer, settled)
            }
        )
        == 1
        and parent["witnessed_outputs"] == restored["witnessed_outputs"] == 69_764
        and parent["headers"] == restored["headers"] == 131
        and parent["recoverable_blocks"] == restored["recoverable_blocks"] == 8
        and parent["solved_blocks"] == restored["solved_blocks"],
        "restored state differs",
    )
    original = bytes.fromhex(r["payment_hex"])
    require(dsha(original)[::-1].hex() == r["payment_txid"], "payment id")
    prev = r["payment_input"]
    funding = bytes.fromhex(r["funding_hex"])
    points, outs = inputs(original)
    _, source_outputs = inputs(funding)
    require(
        points == [(prev["txid"], prev["index"])]
        and dsha(funding)[::-1].hex() == prev["txid"]
        and source_outputs[prev["index"]]
        == (prev["value"], bytes.fromhex(prev["script"]))
        and outs == [(prev["value"] - 1000, bytes.fromhex(prev["script"]))],
        "wrong witnessed output or payment fee",
    )
    verify_p2pk_signature(funding, original)
    job = r["worker"]["first_job"]
    prefix, body = build_candidate(
        job, r["worker"]["extranonce1"], r["extranonce2"], [original]
    )
    wire = bytes.fromhex(r["selected_wire"])
    header = prefix + struct.pack("<I", r["nonce"])
    child = bytes.fromhex(r["stock_successor_header"])
    require(
        wire == header + body
        and header[4:36][::-1].hex() == PARENT
        and r["worker_ack"] == {"result": True, "error": None}
        and r["worker"]["authorized"] is True
        and len(child) == 80
        and child[4:36] == dsha(header)
        and all(
            int.from_bytes(dsha(h), "little") <= REGTEST_TARGET
            and h[72:76] == bytes.fromhex("ffff7f20")
            for h in (header, child)
        ),
        "stock selection and PoW ancestry",
    )
    require(
        settled["height"] == 582
        and settled["tip_hash"] == dsha(child)[::-1].hex()
        and settled["solved_blocks"] == parent["solved_blocks"] + 1
        and settled["pending_transactions"] == 0
        and len({r["after_stop_sha256"], r["final_checkpoint_sha256"], ARCHIVE}) == 3,
        "successor or durable state missing",
    )
    return {
        "original_height": 580,
        "selected_solution": dsha(header)[::-1].hex(),
        "stock_successor": settled["tip_hash"],
        "payment_txid": r["payment_txid"],
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    subs = p.add_subparsers(dest="command", required=True)
    v = subs.add_parser("verify")
    v.add_argument("receipt", type=Path)
    u = subs.add_parser("run")
    for x in ["release", "checkpoint", "workload", "blocks", "output"]:
        u.add_argument(x, type=Path)
    for x in ["stock-volume", "entrant-volume", "lease-volume"]:
        u.add_argument("--" + x, required=True)
    a = p.parse_args()
    if a.command == "verify":
        print(json.dumps(verify(json.loads(a.receipt.read_text())), indent=2))
    else:
        from run_cohort import session_lock

        with session_lock():
            run(a)
