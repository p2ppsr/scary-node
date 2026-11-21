"""Request an earlier local solution from both ordinary wire interfaces after restart."""

import argparse
import contextlib
import hashlib
import json
import secrets
import socket
import struct
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from native_cohort import REGTEST_TARGET, funding_transactions, stratum_job
from run_cohort import session_lock
from scary_dar import (
    ROOT,
    LabFeed,
    dsha,
    entrant,
    get_port,
    response,
    until,
)
from sv_reference import IMAGE, Reference
from verify_retained_delivery import verify


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


class RequestingPeer:
    """A loopback SV peer that requests data but never advertises an active tip."""

    magic = bytes.fromhex("dab5bffa")

    def __init__(self, connection):
        self.sock = connection
        self.stream = connection.makefile("rb")
        self.sock.settimeout(10)

    def send(self, command, payload=b""):
        header = self.magic + command.encode().ljust(12, b"\0")
        self.sock.sendall(
            header + struct.pack("<I", len(payload)) + dsha(payload)[:4] + payload
        )

    def receive(self):
        header = self.stream.read(24)
        assert len(header) == 24 and header[:4] == self.magic
        size = int.from_bytes(header[16:20], "little")
        assert size < 1_000_000
        payload = self.stream.read(size)
        assert len(payload) == size and dsha(payload)[:4] == header[20:24]
        return header[4:16].rstrip(b"\0").decode(), payload

    def handshake(self):
        command, version = self.receive()
        assert command == "version" and len(version) >= 85
        self.send("version", version[:72] + secrets.token_bytes(8) + version[80:])
        self.send("verack")
        assert self.receive() == ("verack", b"")

    def block(self, block_id):
        inventory = b"\1" + struct.pack("<I", 2) + bytes.fromhex(block_id)[::-1]
        self.send("getdata", inventory)
        while True:
            command, payload = self.receive()
            if command == "notfound":
                assert payload == inventory
                return None
            if command == "block":
                assert dsha(payload[:80])[::-1].hex() == block_id
                return payload
            if command == "ping":
                self.send("pong", payload)


def fetch(url, data=None):
    try:
        with urllib.request.urlopen(url, data=data, timeout=6) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as e:
        return e.code, None


def run(binary, directory):
    with (
        Reference().running(peer=True, fast_rpc=True) as full,
        contextlib.ExitStack() as life,
    ):
        full.rpc("generate", 105)
        feed = LabFeed(full, directory)
        life.callback(feed.server.server_close)
        life.callback(feed.server.shutdown)
        operator, worker, assets = get_port(), get_port(), get_port()

        def start(*extra):
            return entrant(
                binary,
                full,
                feed,
                directory,
                operator,
                worker,
                extra_args=extra,
                asset_port=assets,
            )

        def snap():
            return response(f"http://127.0.0.1:{operator}/status")["mining"]

        def selected(block_id):
            until(
                lambda: (
                    snap()["tip_hash"] == block_id
                    and snap()["observation_window_ready"]
                ),
                "ready selected tip " + block_id,
            )

        funding, payments = funding_transactions(full, 2)
        with start() as api:
            full.rpc("sendrawtransaction", funding)
            selected(full.rpc("generate", 1)[0])
            parent = full.rpc("generate", 1)[0]
            selected(parent)
            local = []
            path = [parent]
            confirmations = []
            for tx in payments:
                parent = path[-1]
                assert (
                    response(api + "/transactions", tx)["txid"] == dsha(tx)[::-1].hex()
                )
                assert snap()["job_preconditions_met"]
                with stratum_job(parent, tx, worker, operator, "single-peer") as job:
                    n = next(
                        i
                        for i in range(1024)
                        if int.from_bytes(dsha(job.header(i)), "little")
                        <= REGTEST_TARGET
                    )
                    found = dsha(job.header(n))[::-1].hex()
                    assert job.submit_native(n)
                pending = snap()
                assert (
                    pending["solved_on_selected_parent"]
                    and pending["observation_window_ready"]
                )
                assert not pending["job_preconditions_met"]
                until(
                    lambda found=found: full.rpc("getbestblockhash") == found,
                    "stock SV selected solution",
                )
                # No successor is mined or announced before this independent readback.
                selected(found)

                def confirmation(found=found, parent=parent, pending=pending):
                    state = snap()
                    record = next(
                        row for row in state["solutions"] if row["hash"] == found
                    )
                    for peer_id, delivered in record["deliveries"].items():
                        if peer_id in record["peer_tip_reporters"] and all(
                            k in delivered
                            for k in (
                                "sent_body",
                                "requested_active_headers",
                                "received_body",
                            )
                        ):
                            assert record["chain_state"] == "observed_active"
                            assert record["first_settlement_at_ms"] is None
                            assert not state["solved_on_selected_parent"]
                            assert (
                                state["job_wait_reason"]
                                == "no eligible pending transactions"
                            )
                            return {
                                "solution": found,
                                "parent": parent,
                                "stock_tip": full.rpc("getbestblockhash"),
                                "entrant_tip": state["tip_hash"],
                                "peer_tip_reports": len(record["peer_tip_reporters"]),
                                "queued_at_ms": record["queued_at_ms"],
                                "sent_at_ms": delivered["sent_body"],
                                "requested_active_headers_at_ms": delivered[
                                    "requested_active_headers"
                                ],
                                "received_body_at_ms": delivered["received_body"],
                                "inclusion_at_ms": record["first_inclusion_at_ms"],
                                "before_readback": {
                                    k: pending[k]
                                    for k in (
                                        "observation_window_ready",
                                        "solved_on_selected_parent",
                                        "job_preconditions_met",
                                        "job_wait_reason",
                                    )
                                },
                                "after_readback": {
                                    k: state[k]
                                    for k in (
                                        "solved_on_selected_parent",
                                        "job_preconditions_met",
                                        "job_wait_reason",
                                    )
                                },
                                "chain_state_before_successor": record["chain_state"],
                                "settlement_before_successor": record[
                                    "first_settlement_at_ms"
                                ],
                            }

                confirmations.append(
                    until(
                        confirmation,
                        "same-peer selected header and independently retrieved body",
                    )
                )
                local.append(found)
                path.append(found)
            for _ in range(2):
                tip = full.rpc("generate", 1)[0]
                selected(tip)
                path.append(tip)

        first, second = local
        reference = bytes.fromhex(full.rpc("getblock", first, 0))
        assert full.rpc("getblock", first)["tx"][1:] == [dsha(payments[0])[::-1].hex()]
        headers = [full.rpc("getblockheader", h, False) for h in path]
        for h, prev in zip(headers[1:], path):
            assert bytes.fromhex(h)[4:36][::-1].hex() == prev
        base = f"http://127.0.0.1:{assets}/api/v1/"
        leaves = b"\xff" * 32 + dsha(payments[0])
        tree = dsha(leaves)[::-1].hex()
        legacy = "block_legacy/" + first + "?wire=1"

        with socket.create_server(("127.0.0.1", 0)) as listener:
            listener.settimeout(15)
            endpoint = "127.0.0.1:" + str(listener.getsockname()[1])
            with start("--sv-peer", endpoint), listener.accept()[0] as connection:
                peer = RequestingPeer(connection)
                life.callback(peer.stream.close)
                peer.handshake()
                selected(tip)
                # The very first Asset request contains only a subtree hash.
                assert fetch(base + "subtree/" + tree) == (200, leaves)
                assert fetch(base + "subtree_data/" + tree) == (200, payments[0])
                assert fetch(base + "subtree/" + tree + "/txs", dsha(payments[0])) == (
                    200,
                    payments[0],
                )
                code, asset_raw = fetch(base + legacy)
                assert code == 200 and asset_raw == reference
                sv_raw = peer.block(first)
                assert sv_raw == reference

                def delivery():
                    record = next(r for r in snap()["solutions"] if r["hash"] == first)
                    sent_at = (
                        record["deliveries"]
                        .get("sv:" + endpoint, {})
                        .get("sent_body", 0)
                    )
                    assert "sv:" + endpoint not in record["peer_tip_reporters"]
                    return sent_at if record["chain_state"] == "settled" else 0

                sent_at = until(
                    delivery, "attributed delayed body without peer-tip claim"
                )
                assert peer.block(tip) is None
                assert fetch(base + "block/" + tip)[0] == 404
                reason = b"retained body regtest"
                sequence = feed.add(
                    7, bytes.fromhex(first)[::-1] + bytes([len(reason)]) + reason
                )
                until(
                    lambda: snap()["directives"]["last_applied"] == sequence,
                    "durable signed invalidation",
                )
                withheld = {}
                for uri in [legacy, "subtree/" + tree, "block/" + second]:
                    withheld[uri] = fetch(base + uri)[0]
                assert all(v == 404 for v in withheld.values())
                assert peer.block(first) is None and peer.block(second) is None
                return {
                    "reference_image": IMAGE,
                    "binary_sha256": digest(binary.read_bytes()),
                    "ordered_header_hex": headers,
                    "ordered_block_ids": path,
                    "local_solutions": local,
                    "single_sv_readbacks": confirmations,
                    "reference_block_hex": reference.hex(),
                    "payments_hex": [p.hex() for p in payments],
                    "subtree_root": tree,
                    "subtree_leaves_hex": leaves.hex(),
                    "reference_bytes": len(reference),
                    "reference_sha256": digest(reference),
                    "sv_getdata_after_restart_sha256": digest(sv_raw),
                    "asset_wire_after_subtree_first_sha256": digest(asset_raw),
                    "retrieving_peer_sent_body_at_ms": sent_at,
                    "retrieving_peer_reported_tip": False,
                    "prior_chain_state": "settled",
                    "foreign_successor_sv_reply": "notfound",
                    "foreign_successor_asset_status": 404,
                    "invalidation_sequence": sequence,
                    "after_invalidation_asset_status": withheld,
                    "after_invalidation_sv_replies": ["notfound", "notfound"],
                }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("receipt", type=Path)
    p.add_argument("--binary", type=Path, default=ROOT / "target/release/scary-node")
    args = p.parse_args()
    with session_lock(), tempfile.TemporaryDirectory() as work:
        result = run(args.binary.resolve(), Path(work))
    verify(result)
    args.receipt.write_text(json.dumps(result, indent=2) + "\n")
