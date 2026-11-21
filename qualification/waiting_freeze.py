"""Exercise an offline signed freeze of a checkpointed native regtest offer."""

import argparse
import contextlib
import gzip
import hashlib
import json
import signal
import struct
import tempfile
from pathlib import Path

from sv_reference import IMAGE, Reference, RpcError
from verify_peer_intake import (
    decode_block,
    header,
    parse_tx,
    txid,
    verify_p2pk_signature,
)
from verify_receipt import require, single_input
from verify_teranode_alerts import REGTEST_GENESIS, require_lab_signers, verify_funding


def digest(data):
    return hashlib.sha256(data).hexdigest()


def snapshot(path):
    """Only the isolated regtest boundary and offer rows enter the public receipt."""
    import msgpack

    raw = path.read_bytes()
    require(
        raw[:8] == b"SCARYWS1" and digest(raw[8:-32]) == raw[-32:].hex(),
        "checkpoint digest",
    )
    state = msgpack.unpackb(raw[8:-32], raw=False, strict_map_key=False, use_list=False)
    require(
        state["regtest"] and state["headers"], "expected a funded regtest checkpoint"
    )
    return {
        "sha256": digest(raw),
        "height": state["height"],
        "tip_hash": txid(state["headers"][-1]),
        "eligible": len(state["pending"]),
        "directive_sequence": state["applied_sequence"],
        "waiting": [[source, tx.hex()] for source, tx in state["waiting"]],
    }


def mining(api):
    from scary_dar import response

    status = response(api + "/status")["mining"]
    return {
        k: status[k]
        for k in (
            "height",
            "tip_hash",
            "observation_window_ready",
            "job_preconditions_met",
            "pending_transactions",
            "waiting_for_successor",
            "waiting_for_successor_bytes",
            "peer_transaction_intake",
            "durable_directive_sequence",
        )
    } | {"directive_ready": status["directives"]["ready"]}


def stock_tx_bytes(stock):
    (peer,) = stock.rpc("getpeerinfo")
    return peer["bytessent_per_msg"].get("tx", 0)


def deliver(feed, stock, funding, index, kind, start, stop):
    payload = bytes.fromhex(funding) + struct.pack("<QQQB", index, start, stop, 1)
    sequence = feed.add(kind, payload)
    funds = {
        "funds": [
            {
                "txOut": {"txId": funding, "vout": index},
                "enforceAtHeight": [{"start": start, "stop": stop}],
                "policyExpiresWithConsensus": True,
            }
        ]
    }
    result = stock.rpc("addToConsensusBlacklist", funds)
    require(result == {"notProcessed": []}, "stock action failed")
    return {
        "record": dict(feed.records[-1]),
        "stock_result": result,
        "sequence": sequence,
    }


def application(action, api, journal):
    from scary_dar import until

    seq = action["sequence"]
    until(
        lambda: (
            mining(api)["durable_directive_sequence"] == seq
            and mining(api)["directive_ready"]
        ),
        f"verified and applied sequence {seq}",
    )
    require(
        (journal / f"{seq:010}.signed").read_bytes().hex() == action["record"]["raw"],
        "durable signed message differs from wire",
    )
    action["receipt"] = json.loads((journal / f"{seq:010}.receipt").read_bytes())


def run(binary, manifest, directory):
    from native_cohort import funding_transactions, stratum_job
    from peer_loss import nonce
    from run_cohort import session_lock
    from scary_dar import LabFeed, entrant, get_port, response, until
    from sv_transaction_intake import worker_without_job

    release = digest(binary.read_bytes())
    require(
        manifest["executable_sha256"] == release, "binary differs from the frozen build"
    )
    with (
        session_lock(),
        Reference().running(peer=True, fast_rpc=True) as sender,
        Reference().running(peer=True, fast_rpc=True) as receiver,
        contextlib.ExitStack() as life,
    ):
        require(
            sender.rpc("getblockhash", 0)
            == receiver.rpc("getblockhash", 0)
            == REGTEST_GENESIS,
            "regtest genesis",
        )
        sender.rpc("generate", 105)
        feed = LabFeed(sender, directory)
        life.callback(feed.server.server_close)
        life.callback(feed.server.shutdown)
        api_port, worker = get_port(), get_port()
        data = directory / "state" / "regtest"
        stopped = {}
        with entrant(
            binary,
            sender,
            feed,
            directory,
            api_port,
            worker,
            stop_signal=signal.SIGTERM,
            exit_receipt=stopped,
        ) as api:
            require(
                response(api + "/status")["release"]["executable_sha256"] == release,
                "sender release",
            )
            funding_hex, (payment,) = funding_transactions(sender, 1)
            funding = sender.rpc("sendrawtransaction", funding_hex)
            creation = sender.rpc("generate", 1)[0]
            height = sender.rpc("getblockcount")
            until(
                lambda: (
                    mining(api)["tip_hash"] == creation
                    and mining(api)["observation_window_ready"]
                ),
                "witnessed funding",
            )
            history = []
            for h in range(1, height + 1):
                body = sender.rpc("getblock", sender.rpc("getblockhash", h), 0)
                require(
                    receiver.rpc("submitblock", body) is None,
                    "receiver refused common history",
                )
                history.append(body)
            require(
                receiver.rpc("getbestblockhash") == creation
                and receiver.rpc("getrawmempool") == [],
                "clean stock receiver",
            )
            initial = mining(api)
            previous_bytes = stock_tx_bytes(sender)
            payment_id = sender.rpc("sendrawtransaction", payment.hex())
            until(lambda: mining(api)["waiting_for_successor"] == 1, "early SV payment")
            before = mining(api)
            offered_bytes = stock_tx_bytes(sender)
        closed = snapshot(data / "witness.state")
        require(
            stopped == {"signal": "SIGTERM", "exit_code": 0}, "controlled stop failed"
        )
        require(
            closed["waiting"] == [["sv", payment.hex()]],
            "signed offer missing from closed checkpoint",
        )
        index = single_input(payment)[1]
        start = height + 1
        freeze = deliver(feed, receiver, funding, index, 2, start, 2**31 - 1)
        with entrant(binary, receiver, feed, directory, api_port, worker) as api:
            require(
                response(api + "/status")["release"]["executable_sha256"] == release,
                "restarted release",
            )
            application(freeze, api, data / "alerts")
            until(
                lambda: (
                    (s := mining(api))["observation_window_ready"]
                    and s["tip_hash"] == creation
                    and s["peer_transaction_intake"]["sv"]["discarded_after_recheck"]
                    == 1
                ),
                "restored payment governed by freeze",
            )
            restored = mining(api)
            before_reoffer = {"restart": stock_tx_bytes(receiver)}
            require(
                receiver.rpc("getrawmempool") == [],
                "receiver held the unconfirmed payment at restart",
            )
            endorsed = receiver.rpc("generate", 1)[0]
            until(lambda: mining(api)["tip_hash"] == endorsed, "frozen stock successor")
            after = mining(api)
            idle = worker_without_job(worker)
            try:
                receiver.rpc("sendrawtransaction", payment.hex())
            except RpcError as e:
                stock_error = json.loads(str(e))["error"]
            else:
                raise ValueError("stock admitted a frozen input")
            before_reoffer["frozen_successor"] = stock_tx_bytes(receiver)
            require(
                receiver.rpc("getrawmempool") == [],
                "stock retained a frozen ordinary payment",
            )

            # SV Node updates the existing consensus interval on unfreeze. With
            # policy expiry enabled its stop is exclusive at the next candidate.
            unfreeze = deliver(feed, receiver, funding, index, 3, start, height + 2)
            application(unfreeze, api, data / "alerts")
            released = mining(api)
            before_reoffer["unfreeze"] = stock_tx_bytes(receiver)
            require(
                receiver.rpc("getrawmempool") == [],
                "receiver acquired an unsolicited payment",
            )
            require(
                receiver.rpc("sendrawtransaction", payment.hex()) == payment_id,
                "stock refused released payment",
            )
            until(
                lambda: mining(api)["pending_transactions"] == 1,
                "released native reoffer",
            )
            reoffered = mining(api)
            after_bytes = stock_tx_bytes(receiver)
            with stratum_job(
                endorsed, payment, worker, api_port, "released-waiting"
            ) as job:
                n = nonce(job)
                solved = txid(job.header(n))
                require(job.submit_native(n), "native Stratum submission failed")
            until(
                lambda: receiver.rpc("getbestblockhash") == solved,
                "stock selected solved payment",
            )
            selected = receiver.rpc("getblock", solved, 0)
            extension = receiver.rpc("generate", 1)[0]
            until(
                lambda: mining(api)["tip_hash"] == extension,
                "entrant followed independent stock extension",
            )
            return {
                "scope": "regtest-signed-lab-feed",
                "reference": IMAGE,
                "node_binary_sha256": release,
                "source_commit": manifest["source_commit"],
                "parent": {"height": height, "hash": creation},
                "history_from_height_one": history,
                "funding_outpoint": {"txid": funding, "vout": index},
                "funding_hex": funding_hex,
                "payment_hex": payment.hex(),
                "payment_id": payment_id,
                "sender_tx_bytes": [previous_bytes, offered_bytes],
                "before_native_offer": initial,
                "before_stop": before,
                "exit": stopped,
                "closed_checkpoint": closed,
                "genesis_public_keys": feed.keys,
                "freeze_issued_while_stopped": freeze,
                "frozen_on_restore": restored,
                "endorsing_block": receiver.rpc("getblock", endorsed, 0),
                "frozen_after_endorsement": after,
                "frozen_worker": idle,
                "stock_frozen_error": stock_error,
                "stock_mempool_while_frozen": [],
                "unfreeze": unfreeze,
                "released_before_reoffer": released,
                "new_peer_tx_bytes_before_reoffer": before_reoffer,
                "after_native_reoffer": reoffered,
                "new_peer_tx_bytes_after_reoffer": after_bytes,
                "stock_selected_block": selected,
                "stock_successor_header": receiver.rpc(
                    "getblockheader", extension, False
                ),
                "final_active_tips": [
                    receiver.rpc("getbestblockhash"),
                    mining(api)["tip_hash"],
                ],
            }


def verify(record, manifest):
    require(
        record["scope"] == "regtest-signed-lab-feed" and record["reference"] == IMAGE,
        "fixture identity",
    )
    require(
        (record["node_binary_sha256"], record["source_commit"])
        == (manifest["executable_sha256"], manifest["source_commit"]),
        "release identity",
    )
    verify_funding(record)
    h, creation = record["parent"]["height"], record["parent"]["hash"]
    funding, pay = (
        bytes.fromhex(record["funding_hex"]),
        bytes.fromhex(record["payment_hex"]),
    )
    require(
        h == 106
        and funding
        in decode_block(bytes.fromhex(record["history_from_height_one"][-1]), creation)[
            1
        ],
        "funding wire",
    )
    verify_p2pk_signature(funding, pay)
    ident, index, _ = single_input(pay)
    require(
        record["payment_id"] == txid(pay)
        and record["funding_outpoint"] == {"txid": ident, "vout": index},
        "funded payment",
    )
    require(record["exit"] == {"signal": "SIGTERM", "exit_code": 0}, "controlled exit")
    closed = record["closed_checkpoint"]
    require(
        len(bytes.fromhex(closed["sha256"])) == 32
        and (
            closed["height"],
            closed["tip_hash"],
            closed["eligible"],
            closed["directive_sequence"],
            closed["waiting"],
        )
        == (h, creation, 0, 0, [["sv", pay.hex()]]),
        "closed waiting snapshot",
    )
    before, after = record["sender_tx_bytes"]
    require(after - before == 24 + len(pay), "sender emitted exactly one tx frame")
    for seq, tag, kind, stop in [
        (1, "freeze_issued_while_stopped", 2, 2**31 - 1),
        (2, "unfreeze", 3, h + 2),
    ]:
        action = record[tag]
        wire = bytes.fromhex(action["record"]["raw"])
        payload = wire[:-195]
        require_lab_signers(payload, wire[-195:], set(record["genesis_public_keys"]))
        version, number, _, typ = struct.unpack("<IIQI", payload[:20])
        require(
            (version, number, typ) == (1, seq, kind)
            and action["record"]["sequence_number"] == action["sequence"] == seq
            and payload[20:]
            == bytes.fromhex(ident) + struct.pack("<QQQB", index, h + 1, stop, 1),
            "signed interval",
        )
        receipt = action["receipt"]
        require(
            receipt["sequence"] == seq
            and receipt["kind"] == kind
            and receipt["signed_sha256"] == digest(wire)
            and receipt["active_height"] == h + seq - 1
            and receipt["rpc_result"] == action["stock_result"] == {"notProcessed": []},
            "durable/stock application",
        )
    endorsed_raw = bytes.fromhex(record["endorsing_block"])
    endorsed = txid(endorsed_raw[:80])
    header(endorsed_raw[:80].hex(), endorsed, creation)
    require(
        len(decode_block(endorsed_raw, endorsed)[1]) == 1,
        "expected empty stock endorsement",
    )
    for key, tip, height, seq, waiting, pending, first, dropped in [
        ("before_stop", creation, h, 0, 1, 0, 1, 0),
        ("frozen_on_restore", creation, h, 1, 0, 0, 0, 1),
        ("frozen_after_endorsement", endorsed, h + 1, 1, 0, 0, 0, 1),
        ("released_before_reoffer", endorsed, h + 1, 2, 0, 0, 0, 1),
        ("after_native_reoffer", endorsed, h + 1, 2, 0, 1, 0, 1),
    ]:
        s = record[key]
        require(
            s["tip_hash"] == tip
            and s["height"] == height
            and s["observation_window_ready"]
            and s["directive_ready"],
            key + " readiness",
        )
        require(
            (
                s["durable_directive_sequence"],
                s["waiting_for_successor"],
                s["waiting_for_successor_bytes"],
                s["pending_transactions"],
                s["job_preconditions_met"],
            )
            == (seq, waiting, len(pay) * waiting, pending, bool(pending)),
            key + " coverage",
        )
        counts = s["peer_transaction_intake"]
        baseline = record["before_native_offer"] if key == "before_stop" else None
        initial = baseline["peer_transaction_intake"]["sv"] if baseline else {}
        expected = {
            "admitted": pending,
            "duplicate": 0,
            "ineligible": 0,
            "awaiting_successor": first,
            "promoted_after_successor": 0,
            "discarded_after_recheck": dropped,
        }
        require(
            not any(counts["teranode_subtree"].values())
            and counts["sv"] == {k: n + initial.get(k, 0) for k, n in expected.items()},
            key + " native intake",
        )
    error = record["stock_frozen_error"]
    require(
        error["code"] < 0
        and any(word in error["message"].lower() for word in ("frozen", "blacklist")),
        "stock policy refusal",
    )
    require(
        record["stock_mempool_while_frozen"] == []
        and record["frozen_worker"]["authorized"]
        and record["frozen_worker"]["notifications"] == 0,
        "frozen work",
    )
    require(
        record["new_peer_tx_bytes_before_reoffer"]
        == {"restart": 0, "frozen_successor": 0, "unfreeze": 0}
        and record["new_peer_tx_bytes_after_reoffer"] == 24 + len(pay),
        "empty replacement and single reoffer",
    )
    selected = bytes.fromhex(record["stock_selected_block"])
    solution = txid(selected[:80])
    header(selected[:80].hex(), solution, endorsed)
    _, txs, outputs = decode_block(selected, solution)
    require(txs[1:] == [pay], "stock-selected released bytes")
    fee = parse_tx(funding, 0)[1][index][0] - sum(v for v, _ in outputs[1])
    require(
        fee > 0
        and outputs[0] == [((5_000_000_000 >> ((h + 2) // 150)) + fee, b"\x51")],
        "released reward",
    )
    final = bytes.fromhex(record["stock_successor_header"])
    successor = txid(final)
    header(final.hex(), successor, solution)
    require(
        record["final_active_tips"] == [successor, successor],
        "independent stock extension",
    )
    return {
        "payment": txid(pay),
        "checkpoint_waiting": 1,
        "restored_frozen_discards": 1,
        "frozen_worker_notifications": 0,
        "native_reoffers_after_unfreeze": 1,
        "fee_satoshis": fee,
        "stock_selected": solution,
        "stock_successor": successor,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    run_args = sub.add_parser("run")
    run_args.add_argument("binary", type=Path)
    for group in (run_args, sub.add_parser("verify")):
        group.add_argument("manifest", type=Path)
        group.add_argument("receipt", type=Path)
    a = p.parse_args()
    manifest = json.loads(a.manifest.read_bytes())
    if a.command == "run":
        if a.receipt.exists():
            p.error("choose a fresh output")
        with tempfile.TemporaryDirectory() as name:
            record = run(a.binary.resolve(), manifest, Path(name))
        with a.receipt.open("xb") as out:
            out.write(
                gzip.compress(
                    json.dumps(record, separators=(",", ":")).encode(), mtime=0
                )
            )
        summary = verify(record, manifest)
    else:
        with gzip.open(a.receipt, "rt") as f:
            summary = verify(json.load(f), manifest)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
