"""Boot an archived regtest Teranode and its Aerospike store on an isolated bridge."""

import argparse
import contextlib
import hashlib
import json
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from recover_sustained_chain import GIB, confirmed_boundary, read
from recover_teranode_chain import AEROSPIKE_IMAGE, TERANODE_IMAGE
from restore_witness_archive import HELPER, canonical, docker, extract
from run_cohort import ROOT, session_lock
from verify_receipt import require

EXPECTED = {
    "tn1": TERANODE_IMAGE,
    "tn2": TERANODE_IMAGE,
    "as1": AEROSPIKE_IMAGE,
    "as2": AEROSPIKE_IMAGE,
}


def isolated_container(network, role, config, files, command):
    settings = config["Config"]
    require(
        settings["Image"] == EXPECTED[role],
        "different stock Teranode or Aerospike version",
    )
    require(
        config["Image"]
        == docker("image", "inspect", "--format", "{{.Id}}", settings["Image"]),
        "different local image",
    )
    args = [
        "create",
        "--name",
        network + "-" + role,
        "--pull=never",
        "--network",
        network,
        "--network-alias",
        role,
        "--log-driver",
        "none",
        "--tmpfs",
        "/run/archive:rw,size=64m",
        "--tmpfs",
        "/tmp:rw,size=96m",
    ]
    if role.startswith("as"):
        args += [
            "--ulimit",
            "nofile=15000:15000",
            "--mount",
            "type=bind,src="
            + str(ROOT / "regtest/aerospike-qualification.conf")
            + ",dst=/etc/aerospike/qualification.conf,readonly",
        ]
    for dest, size in files.items():
        args += ["--tmpfs", dest + ":rw,size=" + size]
    with tempfile.NamedTemporaryFile(mode="w", prefix="scary-regtest-env-") as env:
        for setting in settings["Env"]:
            require("\n" not in setting, "unexpected multiline container setting")
            env.write(setting + "\n")
        env.flush()
        args += ["--env-file", env.name]
        if role.startswith("tn"):
            # Preserve the original keys, native settings and stores. This pair
            # must recover before any other miner or legacy peer can supply data.
            args += ["--env", "p2p_static_peers=", "--env", "startLegacy=false"]
        args += [
            "--entrypoint",
            "/bin/sh",
            settings["Image"],
            "-ec",
            'while [ ! -f /run/archive/start ]; do sleep .1; done; "$@" >/run/archive/service.log 2>&1 || echo "$?" >/run/archive/failed; sleep infinity',
            "archive-start",
            *command,
        ]
        return docker(*args)


def wait_ready(cid, query, timeout):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        if (
            subprocess.run(
                ["docker", "exec", cid, "test", "-f", "/run/archive/failed"],
                capture_output=True,
                check=False,
            ).returncode
            == 0
        ):
            raise RuntimeError(
                "archived service exited during boot: "
                + docker("exec", cid, "tail", "-n", "4", "/run/archive/service.log")[
                    -1000:
                ]
            )
        try:
            if answer := query():
                return answer
        except (ValueError, subprocess.CalledProcessError) as ex:
            last = type(ex).__name__
        time.sleep(0.35)
    raise TimeoutError("archived reference service did not recover: " + str(last))


def native_rpc(cid, method, *params):
    # This credential is the public literal from the isolated regtest Compose file.
    raw = docker(
        "exec",
        cid,
        "curl",
        "--fail",
        "--silent",
        "--show-error",
        "--max-time",
        "6",
        "-u",
        "regtest:disposable-regtest-rpc",
        "-H",
        "Content-Type: application/json",
        "--data-binary",
        json.dumps({"id": 1, "method": method, "params": list(params)}),
        "http://127.0.0.1:9292/",
        stderr=subprocess.DEVNULL,
    )
    msg = json.loads(raw)
    if msg.get("error") is not None:
        raise ValueError("archived stock RPC unavailable")
    return msg["result"]


def connected_peers(cid):
    metrics = docker(
        "exec",
        cid,
        "curl",
        "-fsS",
        "--max-time",
        "10",
        "http://127.0.0.1:9091/metrics",
    )
    name = "teranode_p2p_connected_peers"
    counts = [
        row.split()[1] for row in metrics.splitlines() if row.startswith(name + " ")
    ]
    require(counts == ["0"], "native registry does not report zero connected peers")
    return int(counts[0])


def readback(cid, row):
    url = "http://127.0.0.1:8090/api/v1/block_legacy/" + row["hash"] + "?wire=1"
    n = 0
    sha = hashlib.sha256()
    with subprocess.Popen(
        ["docker", "exec", cid, "curl", "-fsS", "--max-time", "180", url],
        stdout=subprocess.PIPE,
    ) as p:
        for buf in iter(lambda: p.stdout.read(1 << 20), b""):
            sha.update(buf)
            n += len(buf)
        require(
            p.wait() == 0,
            "restored native Asset service did not serve the complete block",
        )
    require(
        n == row["wire_bytes"] and sha.hexdigest() == row["wire_sha256"],
        "archived Teranode body differs",
    )
    return {
        "height": row["height"],
        "hash": row["hash"],
        "bytes": n,
        "wire_sha256": sha.hexdigest(),
    }


def compare_pending(tnid, candidate, original_wire):
    from prove import decode_block, dsha, txid

    original = original_wire.read_bytes()
    url = (
        "http://127.0.0.1:8090/api/v1/block_legacy/"
        + candidate["id"]
        + "?type=miningcandidate&wire=1"
    )
    current = subprocess.check_output(
        ["docker", "exec", tnid, "curl", "-fsS", "--max-time", "180", url]
    )
    require(
        original[4:36] == current[4:36]
        and current[4:36][::-1].hex() == candidate["prevhash"],
        "unpublished candidate parent differs",
    )

    def membership(raw):
        _, txs, _ = decode_block(raw, txid(raw[:80]))
        ordinary = txs[1:]
        ids = [dsha(tx) for tx in ordinary]
        require(len(set(ids)) == len(ids), "duplicate candidate payment")
        return {
            "payments": len(ids),
            "candidate_wire_sha256": hashlib.sha256(raw).hexdigest(),
            "ordinary_wire_sha256": hashlib.sha256(b"".join(ordinary)).hexdigest(),
            "ordered_internal_txids_sha256": hashlib.sha256(b"".join(ids)).hexdigest(),
            "sorted_internal_txids_sha256": hashlib.sha256(
                b"".join(sorted(ids))
            ).hexdigest(),
        }

    before = membership(original)
    after = membership(current)
    require(
        after["payments"] == candidate["num_tx"],
        "native RPC and candidate asset differ",
    )
    return {
        "original": before,
        "after_isolated_restart": after,
        "same_payment_membership": before["sorted_internal_txids_sha256"]
        == after["sorted_internal_txids_sha256"],
        "same_ordered_payment_bytes": before["ordinary_wire_sha256"]
        == after["ordinary_wire_sha256"],
    }


def boot(
    measured_path,
    configuration,
    role,
    node_data,
    store_data,
    store_smd,
    *,
    original_candidate=None,
):
    receipt = read(measured_path)
    original = {
        c["Config"]["Labels"]["com.docker.compose.service"]: c for c in configuration
    }
    project = original[role]["Config"]["Labels"]["com.docker.compose.project"]
    require(
        project == receipt["setup"]["project"],
        "different measured project",
    )
    db = "as" + role[-1]
    for service in (role, db):
        require(
            original[service]["Config"]["Labels"]["com.docker.compose.project"]
            == project
            and original[service]["Image"]
            == receipt["setup"]["topology"][service]["image_id"],
            "original enrollment differs",
        )
    guest = docker(
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--log-driver",
        "none",
        "--pull=never",
        "--entrypoint",
        "cat",
        HELPER,
        "/proc/meminfo",
    )
    available = (
        int(
            next(
                line for line in guest.splitlines() if line.startswith("MemAvailable:")
            ).split()[1]
        )
        * 1024
    )
    require(
        available > 30 * GIB, "original archived pair needs 30 GiB available guest RAM"
    )
    network = "scary-db-restore-" + uuid.uuid4().hex[:12]
    started = time.monotonic()
    with contextlib.ExitStack() as clean:
        docker("network", "create", "--internal", network)
        clean.callback(docker, "network", "rm", network)
        asid = isolated_container(
            network,
            db,
            original[db],
            {"/opt/aerospike/data": "11g", "/opt/aerospike/smd": "32m"},
            [
                "asd",
                "--foreground",
                "--config-file",
                "/etc/aerospike/qualification.conf",
            ],
        )
        clean.callback(docker, "rm", "--force", asid)
        tnid = isolated_container(
            network,
            role,
            original[role],
            {"/app/data": "10g"},
            ["./teranode.run", "-localTestStartFromState=RUNNING"],
        )
        clean.callback(docker, "rm", "--force", tnid)
        docker("start", asid, tnid)
        extract(asid, store_data, "/opt/aerospike/data")
        extract(asid, store_smd, "/opt/aerospike/smd")
        extract(tnid, node_data, "/app/data")
        docker("exec", asid, "touch", "/run/archive/start")
        wait_ready(
            asid,
            lambda: docker(
                "exec",
                asid,
                "sh",
                "-c",
                "grep -Fi 'service ready' /run/archive/service.log | tail -1",
            ),
            240,
        )
        docker("exec", tnid, "touch", "/run/archive/start")
        target = confirmed_boundary(receipt)

        def ready():
            msg = native_rpc(tnid, "getblockchaininfo")
            return (
                msg
                if msg["bestblockhash"] == target["final_hash"]
                and msg["blocks"] == target["final_height"]
                else None
            )

        info = wait_ready(tnid, ready, 240)

        def current_candidate():
            args = (True,) if original_candidate is not None else ()
            value = native_rpc(tnid, "getminingcandidate", *args)
            return value if value["prevhash"] == info["bestblockhash"] else None

        candidate = wait_ready(tnid, current_candidate, 240)

        def isolation():
            net = json.loads(docker("network", "inspect", network))[0]
            require(
                net["Internal"] and set(net["Containers"]) == {tnid, asid},
                "restore network is not an isolated node/database pair",
            )
            for member in (tnid, asid):
                networks = json.loads(docker("inspect", member))[0]["NetworkSettings"][
                    "Networks"
                ]
                require(
                    set(networks) == {network}, "restore service has another network"
                )
            return connected_peers(tnid)

        before = isolation()
        pending = (
            {"pending_recovery": compare_pending(tnid, candidate, original_candidate)}
            if original_candidate is not None
            else {}
        )
        rows = [readback(tnid, row) for row in receipt["blocks"]]
        if original_candidate is not None:
            # A restarted block assembler can lag its service's selected header.
            pending["first_pending_recovery"] = pending["pending_recovery"]
            candidate = wait_ready(tnid, current_candidate, 240)
            pending["pending_recovery"] = compare_pending(
                tnid, candidate, original_candidate
            )
        after = isolation()
        return {
            "network": "isolated regtest",
            "role": role,
            "database_role": db,
            **pending,
            "teranode_image": EXPECTED[role],
            "aerospike_image": EXPECTED[db],
            "teranode_image_id": original[role]["Image"],
            "aerospike_image_id": original[db]["Image"],
            "measured_record_sha256": canonical(receipt),
            "initial_chain_source": "archived service volumes only",
            "native_peers": 0,
            "isolated_network_members": [role, db],
            "connected_peer_gauge_before_after_readback": [before, after],
            "height": info["blocks"],
            "selected_hash": info["bestblockhash"],
            "candidate_transactions": candidate["num_tx"],
            "candidate_parent": candidate["prevhash"],
            "restored_readbacks": rows,
            "available_guest_bytes_before": available,
            "elapsed_s": time.monotonic() - started,
        }


def verify(proof, receipt):
    role = proof["role"]
    db = "as" + role[-1]
    require(
        role in ("tn1", "tn2")
        and proof["database_role"] == db
        and proof["teranode_image"] == EXPECTED[role]
        and proof["aerospike_image"] == EXPECTED[db]
        and proof["native_peers"] == 0
        and proof["isolated_network_members"] == [role, db]
        and proof["connected_peer_gauge_before_after_readback"] == [0, 0]
        and proof["measured_record_sha256"] == canonical(receipt),
        "archived role or original receipt differs",
    )
    require(
        proof["teranode_image_id"] == receipt["setup"]["topology"][role]["image_id"]
        and proof["aerospike_image_id"] == receipt["setup"]["topology"][db]["image_id"],
        "service image differs",
    )
    expected = [
        {
            "height": r["height"],
            "hash": r["hash"],
            "bytes": r["wire_bytes"],
            "wire_sha256": r["wire_sha256"],
        }
        for r in receipt["blocks"]
    ]
    target = confirmed_boundary(receipt)
    require(
        expected == proof["restored_readbacks"]
        and proof["selected_hash"] == proof["candidate_parent"] == target["final_hash"]
        and proof["height"] == target["final_height"],
        "retained blocks or boundary differ",
    )
    if "pending_recovery" in proof:
        original = proof["pending_recovery"]["original"]
        restored = proof["pending_recovery"]["after_isolated_restart"]
        require(
            restored["payments"] == proof["candidate_transactions"]
            and proof["pending_recovery"]["same_payment_membership"]
            == (
                restored["sorted_internal_txids_sha256"]
                == original["sorted_internal_txids_sha256"]
            )
            and proof["pending_recovery"]["same_ordered_payment_bytes"]
            == (restored["ordinary_wire_sha256"] == original["ordinary_wire_sha256"]),
            "pending candidate comparison differs",
        )
    return {
        "role": role,
        "height": proof["height"],
        "readbacks": len(expected),
        "bytes": sum(r["bytes"] for r in expected),
        "native_peers": 0,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    a = p.add_subparsers(dest="action", required=True)
    b = a.add_parser("boot")
    b.add_argument("measured", type=Path)
    b.add_argument("private_inspect", type=Path)
    b.add_argument("role", choices=("tn1", "tn2"))
    b.add_argument("--original-candidate", type=Path)
    for k in ("node_data", "store_data", "store_smd", "proof"):
        b.add_argument(k, type=Path)
    v = a.add_parser("verify")
    v.add_argument("measured", type=Path)
    v.add_argument("proof", type=Path)
    args = p.parse_args()
    if args.action == "boot":
        if args.proof.exists():
            p.error("choose a fresh proof path")
        with session_lock():
            result = boot(
                args.measured,
                read(args.private_inspect),
                args.role,
                args.node_data,
                args.store_data,
                args.store_smd,
                original_candidate=args.original_candidate,
            )
            verify(result, read(args.measured))
            with args.proof.open("x") as f:
                json.dump(result, f, indent=2)
                f.write("\n")
    print(json.dumps(verify(read(args.proof), read(args.measured)), indent=2))
