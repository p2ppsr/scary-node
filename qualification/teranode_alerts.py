"""Observe normal signed notices and a spend across a stock Teranode freeze and unfreeze."""

import argparse
import contextlib
import hashlib
import json
import secrets
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "regtest"))
from prove import DisposableWallet, block, request, rpc, txid
from receiver_dar import NativeFeed
from run_cohort import session_lock
from scary_dar import ROOT, Reference, entrant, get_port, response
from sv_reference import RpcError

IMAGE = "ghcr.io/bsv-blockchain/teranode:v0.16.1@sha256:84971b2863301c7527382768dbe5a094cd8c4d6e2f426f5557d3c12ba114822a"


def wait(label, check, timeout=90):
    end = time.monotonic() + timeout
    error = None
    while time.monotonic() < end:
        try:
            result = check()
            if result:
                return result
        except (ValueError, RuntimeError, OSError) as exc:
            error = exc
        time.sleep(0.25)
    raise RuntimeError(f"timed out awaiting {label}: {error}")


def build_publisher():
    src = (ROOT / ".native/alert").resolve()
    if (
        subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=src, text=True
        ).strip()
        != "c19f9959fb979f9def944807bd4e58ecadf25717"
    ):
        raise ValueError(
            "run qualification/receiver/build.py for the pinned dependency first"
        )
    file = (ROOT / ".native/teranode_alert_publisher.go").resolve()
    shutil.copyfile(ROOT / "qualification/receiver/teranode_publisher.go.in", file)
    binary = (ROOT / ".native/teranode-alert-publisher").resolve()
    subprocess.run(
        ["go", "build", "-mod=readonly", "-o", str(binary), str(file)],
        cwd=src,
        check=True,
    )
    return binary


@contextlib.contextmanager
def stock(keys, directory, *, with_sv=False):
    name = "scary-alert-" + secrets.token_hex(5)
    compose = [
        "docker",
        "compose",
        "-p",
        name,
        "-f",
        str(ROOT / "regtest/compose.yaml"),
    ]
    services = ["tn1", "tn2", "alert-ingress"]
    if with_sv:
        for overlay_name in ("mixed.compose.yaml", "mixed-native.compose.yaml"):
            compose.extend(["-f", str(ROOT / "regtest" / overlay_name)])
        services.append("svref")
    compose.extend(["-f", str(directory / "overlay.json")])
    defaults = json.loads(
        subprocess.check_output(
            [
                "docker",
                "compose",
                "-f",
                str(ROOT / "regtest/compose.yaml"),
                "config",
                "--format",
                "json",
            ],
            text=True,
        )
    )
    if any(defaults["services"][node]["image"] != IMAGE for node in ("tn1", "tn2")):
        raise ValueError(
            "stock Teranode image differs from the pinned qualification revision"
        )
    base = defaults["services"]["tn1"]["environment"]
    peer_id = base["p2p_peer_id"]
    proxy = directory / "proxy.conf"
    proxy.write_text(
        "events {}\nstream {\n"
        + "".join(
            f"server {{ listen {p}; proxy_pass {host}:{target}; }}\n"
            for p, host, target in [
                (10001, "tn1", 9292),
                (10002, "tn1", 8090),
                (10003, "tn1", 9908),
                (10004, "tn2", 9292),
                (10005, "tn2", 8090),
            ]
        )
        + "}\n"
    )
    overlay = {
        "services": {
            "tn1": {
                "environment": {
                    "startAlert": "true",
                    "alert_genesis_keys": "|".join(keys),
                    "alert_p2p_private_key": base["p2p_private_key"],
                    "alert_topic_name": "scary_node_qualification_regtest",
                    "alert_protocol_id": "/scary-regtest/alert-system/1.0.0",
                    "alert_p2p_allow_private_ips": "true",
                    "alert_p2p_dht_mode": "server",
                    "alert_store": "sqlite:///alert",
                }
            },
            "alert-ingress": {
                "image": defaults["services"]["ingress"]["image"],
                "networks": ["regtest", "host-access"],
                "ports": [
                    f"127.0.0.1::{p}" for p in (10001, 10002, 10003, 10004, 10005)
                ],
                "volumes": [f"{proxy}:/etc/nginx/nginx.conf:ro"],
            },
        }
    }
    # Both stock Alert Services must successfully dial a bootstrap peer before
    # they join their topic. Keep the bootstrap and application traffic inside regtest.
    env = overlay["services"]["tn1"]["environment"]
    for local, other in (("tn1", "tn2"), ("tn2", "tn1")):
        own_identity = defaults["services"][local]["environment"]
        remote_id = defaults["services"][other]["environment"]["p2p_peer_id"]
        overlay["services"][local] = {
            "environment": {
                **env,
                "alert_p2p_private_key": own_identity["p2p_private_key"],
                "alert_p2p_bootstrap_peer": f"/dns4/{other}/tcp/9908/p2p/{remote_id}",
            }
        }
    (directory / "overlay.json").write_text(json.dumps(overlay))
    try:
        subprocess.run(
            [*compose, "up", "-d", "--no-build", *services],
            check=True,
            stdout=sys.stderr,
        )

        def container(*args):
            return subprocess.check_output([*compose, *args], text=True).strip()

        ports = [
            int(container("port", "alert-ingress", str(p)).rsplit(":", 1)[1])
            for p in (10001, 10002, 10003, 10004, 10005)
        ]
        node = ports[:2]
        wait(
            "Teranode RPC", lambda: rpc(node, "getblockchaininfo")["chain"] == "regtest"
        )
        wait(
            "Teranode alert subscription",
            lambda: (
                "subscribed to scary_node_qualification_regtest"
                in container("logs", "--no-color", "tn1")
            ),
        )
        yield node, ports[3:5], ports[2], peer_id, container
    finally:
        subprocess.run(
            [*compose, "down", "--volumes", "--remove-orphans"],
            check=True,
            stdout=sys.stderr,
        )


def stored_alerts(container, service, directory):
    container("stop", service)
    db = directory / f"{service}-readback.db"
    subprocess.run(
        [
            "docker",
            "cp",
            container("ps", "-a", "-q", service) + ":/app/data/alert.db",
            str(db),
        ],
        check=True,
    )
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        table = next(
            n
            for (n,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            if n.endswith("alert_messages")
        )
        cols = [row[1] for row in conn.execute(f"pragma table_info({table})")]
        if not {"sequence_number", "processed", "raw"} <= set(cols):
            raise ValueError("unexpected pinned receiver schema: " + str(cols))
        stored = [
            {
                "sequence_number": s,
                "processed": bool(ok),
                "raw": raw.hex() if isinstance(raw, bytes) else raw,
            }
            for s, ok, raw in conn.execute(
                f"select sequence_number,processed,raw from {table} where sequence_number>0 order by sequence_number"
            )
        ]
    return stored


def measure(directory, binary, publisher_binary):
    with (
        Reference().running(peer=True, fast_rpc=True) as sv,
        contextlib.ExitStack() as life,
    ):
        f = NativeFeed(sv, directory, life)
        node, other, port, peer_id, container = life.enter_context(
            stock(f.keys, directory)
        )
        wallet = DisposableWallet(directory / "payment.pem")
        history = []

        def sync(h):
            body = request(f"http://127.0.0.1:{node[1]}/api/v1/block_legacy/{h}?wire=1")
            if (
                sv.rpc("submitblock", body.hex()) is not None
                or sv.rpc("getbestblockhash") != h
            ):
                raise RuntimeError("funding history differs on SV Node")
            history.append(body.hex())

        initial = rpc(node, "generatetoaddress", 1, wallet.address)[0]
        sync(initial)
        _, txs, outputs = block(node, initial)
        source = txid(txs[0])
        value, script = outputs[0][0]
        assert script == wallet.script
        for _ in range(100):
            sync(rpc(node, "generate", 1)[0])
        api_port = get_port()
        api = life.enter_context(
            entrant(binary, sv, f, directory, api_port, get_port())
        )
        # Observe the fresh boundary before mining the funded transaction. A
        # still-starting peer can otherwise choose the funding block as its anchor.
        boundary = rpc(node, "generate", 1)[0]
        sync(boundary)
        wait(
            "entrant selected its pre-funding boundary",
            lambda: response(api + "/status")["mining"]["tip_hash"] == boundary,
        )
        funding = wallet.spend(source, value, 1000)
        fid = rpc(node, "sendrawtransaction", funding.hex())
        wait(
            "funding assembled", lambda: rpc(node, "getminingcandidate")["num_tx"] >= 1
        )
        funded = rpc(node, "generate", 1)[0]
        sync(funded)
        if funding not in block(node, funded)[1]:
            raise RuntimeError("Teranode did not mine the funding transaction")
        wait(
            "entrant validated funding body",
            lambda: any(
                row["hash"] == funded
                for row in response(api + "/status")["mining"]["validation_window"]
            ),
        )
        parent = rpc(node, "generate", 1)[0]
        sync(parent)
        height = sv.rpc("getblockcount")
        wait(
            "same funding parent",
            lambda: response(api + "/status")["mining"]["tip_hash"] == parent,
        )
        wait(
            "second Teranode funding parent",
            lambda: rpc(other, "getbestblockhash") == parent,
        )
        spend = wallet.spend(fid, value - 1000, 1000)
        with subprocess.Popen(
            [str(publisher_binary), str(port), peer_id],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        ) as publisher:

            def apply(kind, payload):
                seq = f.add(kind, payload)
                record = f.records[-1]
                publisher.stdin.write(record["raw"] + "\n")
                publisher.stdin.flush()
                if publisher.stdout.readline().strip() != "published":
                    raise ValueError("publisher failed")
                wait(
                    "stock node receipt",
                    lambda: all(
                        f"got alert type: {kind}"
                        in container("logs", "--no-color", name)
                        for name in ("tn1", "tn2")
                    ),
                )
                wait(
                    "Scary Node and maintained receiver receipt",
                    lambda: (
                        response(api + "/status")["mining"][
                            "durable_directive_sequence"
                        ]
                        == seq
                        and f.health()["unprocessed_alerts"] == 0
                    ),
                )

            try:
                text = b"isolated positive interoperability observation"
                apply(1, bytes([len(text)]) + text)

                def interval(kind, start, stop):
                    apply(
                        kind,
                        bytes.fromhex(fid) + struct.pack("<QQQB", 0, start, stop, 1),
                    )
                    reply = sv.rpc(
                        "addToConsensusBlacklist",
                        {
                            "funds": [
                                {
                                    "txOut": {"txId": fid, "vout": 0},
                                    "enforceAtHeight": [{"start": start, "stop": stop}],
                                    "policyExpiresWithConsensus": True,
                                }
                            ]
                        },
                    )
                    if reply.get("notProcessed", []):
                        raise RuntimeError("SV Node directive failed")

                interval(2, height, 2**31 - 1)
                rejections = {}
                for label, fn in [
                    ("teranode", lambda: rpc(node, "sendrawtransaction", spend.hex())),
                    ("sv_node", lambda: sv.rpc("sendrawtransaction", spend.hex())),
                    ("scary_node", lambda: response(api + "/transactions", spend)),
                ]:
                    try:
                        fn()
                    except urllib.error.HTTPError as e:
                        reason = json.loads(e.read())["error"]
                    except (RpcError, RuntimeError) as e:
                        reason = str(e)
                    else:
                        raise ValueError(label + " admitted a frozen spend")
                    if not any(
                        w in reason.lower() for w in ("frozen", "blacklist", "freeze")
                    ):
                        raise ValueError(
                            label + " returned an unrelated reason: " + reason
                        )
                    rejections[label] = reason
                interval(3, height - 2, height - 1)
                # One peer exercised the frozen RPC, which writes the tx blob before checking policy.
                # Submit the same payment to the other funded peer and check inclusion on both.
                ids = {
                    "teranode": rpc(other, "sendrawtransaction", spend.hex()),
                    "sv_node": sv.rpc("sendrawtransaction", spend.hex()),
                    "scary_node": response(api + "/transactions", spend)["txid"],
                }
                assert set(ids.values()) == {txid(spend)}
                wait(
                    "released payment assembled",
                    lambda: rpc(other, "getminingcandidate")["num_tx"] >= 1,
                )
                inclusion = rpc(other, "generate", 1)[0]
                wait(
                    "original Teranode accepts released payment block",
                    lambda: rpc(node, "getbestblockhash") == inclusion,
                )
                sync(inclusion)
                wait(
                    "three selected tips",
                    lambda: (
                        response(api + "/status")["mining"]["tip_hash"] == inclusion
                    ),
                )
                included_body = request(
                    f"http://127.0.0.1:{node[1]}/api/v1/block_legacy/{inclusion}?wire=1"
                )
                _, txs, _ = block(node, inclusion)
                assert spend in txs
                receiver = f.health()
            finally:
                publisher.stdin.close()
        active_tips = {
            label: rpc(ref, "getbestblockhash")
            for label, ref in [("original_teranode", node), ("second_teranode", other)]
        }
        active_tips["sv_node"] = sv.rpc("getbestblockhash")
        active_tips["scary_node"] = response(api + "/status")["mining"]["tip_hash"]
        assert set(active_tips.values()) == {inclusion}
        stored = stored_alerts(container, "tn1", directory)
        if [r["raw"] for r in stored] != [r["raw"] for r in f.records] or not all(
            r["processed"] for r in stored
        ):
            raise ValueError("signed durable prefix differs")
        return {
            "teranode_image": IMAGE,
            "teranode_embedded_receiver": "v0.3.3",
            "scary_node_receiver": "v0.3.5",
            "genesis_public_keys": f.keys,
            "signed_records": stored,
            "parent": {"height": height, "hash": parent},
            "history_from_height_one": history[:-1],
            "node_binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            "funding_outpoint": {"txid": fid, "vout": 0},
            "ordinary_spend_hex": spend.hex(),
            "ordinary_spend_txid": txid(spend),
            "frozen_rejections": rejections,
            "released_txids": ids,
            "active_inclusion_hash": inclusion,
            "active_tips": active_tips,
            "block_hex": included_body.hex(),
            "maintained_receiver_sequence": receiver["sequence"],
            "maintained_receiver_unprocessed": receiver["unprocessed_alerts"],
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--binary", type=Path, default=ROOT / "target/release/scary-node"
    )
    args = parser.parse_args()
    publisher = build_publisher()
    with session_lock(), tempfile.TemporaryDirectory(prefix="scary-alert-") as tmp:
        print(
            json.dumps(measure(Path(tmp), args.binary.resolve(), publisher), indent=2)
        )
