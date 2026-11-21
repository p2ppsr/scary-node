"""Run a fresh native SV-P2P session with separate receivers and a full/full control."""

import argparse
import contextlib
import hashlib
import json
import random
import subprocess
import tempfile
import uuid
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from chronicle import (
    ROOT,
    IMAGE,
    Reference,
    CTransaction,
    CTxOut,
    CTxIn,
    COutPoint,
    ToHex,
    FromHex,
)
from chronicle_upgrade_tests.test_base import ChronicleHeightTestsCase
from test_framework.script import CScript, OP_CHECKSIG, SIGHASH_ALL, SIGHASH_FORKID
from scary_dar import LabFeed, entrant, get_port, response, until
from race_work import checked_job, stratum_job, REGTEST_TARGET
from prove import dsha


def now():
    return time.monotonic_ns()


def native_link(source, target):
    ip = target.docker(
        "inspect",
        "--format",
        "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
        target.container,
    )
    source.rpc("addnode", ip + ":18444", "onetry")


def funding_transactions(funder, count):
    case = ChronicleHeightTestsCase()
    key = case.make_key(b"disposable session funding")
    script = CScript([key.get_pubkey(), OP_CHECKSIG])
    amount = 100000
    coin = funder.rpc("listunspent")[0]
    f = CTransaction()
    f.vin = [CTxIn(COutPoint(int(coin["txid"], 16), coin["vout"]), b"", 0xFFFFFFFF)]
    f.vout = [CTxOut(amount, script) for _ in range(count)]
    change = bytes.fromhex(
        funder.rpc("validateaddress", funder.rpc("getnewaddress"))["scriptPubKey"]
    )
    f.vout.append(CTxOut(round(coin["amount"] * 1e8) - amount * count - 10000, change))
    result = funder.rpc("signrawtransaction", ToHex(f))
    assert result["complete"]
    f = FromHex(CTransaction(), result["hex"])
    f.rehash()
    for o in f.vout:
        o.scriptPubKey = CScript(o.scriptPubKey)
    payments = []
    for i in range(count):
        tx = case.new_transaction(
            key, [case.Input((i, f), SIGHASH_ALL | SIGHASH_FORKID)]
        )
        # A fee above the pinned full node's policy for the ~200-byte payment.
        tx.vout[0].nValue -= 1000
        from test_framework.script import SignatureHash

        mode = SIGHASH_ALL | SIGHASH_FORKID
        signature = key.sign(SignatureHash(script, tx, 0, mode, amount)) + bytes([mode])
        tx.vin[0].scriptSig = CScript([signature])
        payments.append(tx.serialize())
    return ToHex(f), payments


def full_job(node, parent, payment, label):
    start = now()
    fields = node.rpc("getminingcandidate", True)
    if fields["prevhash"] != parent or fields["merkleProof"] != [
        dsha(payment)[::-1].hex()
    ]:
        return None, "candidate lacks common payment or parent", now() - start
    cb = bytes.fromhex(fields["coinbase"])

    def submit(nonce):
        reply = node.rpc(
            "submitminingsolution",
            {
                "id": fields["id"],
                "nonce": nonce,
                "coinbase": cb.hex(),
                "time": fields["time"],
                "version": fields["version"],
            },
        )
        if reply is not True:
            raise RuntimeError(f"full producer disposition {reply!r}")
        return reply

    return (
        checked_job(
            label,
            "sv-full",
            fields["id"],
            parent,
            fields["version"],
            fields["time"],
            int(fields["nBits"], 16),
            cb,
            payment,
            submit,
        ),
        None,
        now() - start,
    )


def opportunity(schedule, nodes, api, stratum, stack, payment, parent):
    ln, rn, left_rx, right_rx = nodes
    pid = dsha(payment)[::-1].hex()
    for n in (ln, rn):
        if pid not in n.rpc("getrawmempool"):
            n.rpc("sendrawtransaction", payment.hex())
    accepted = None
    try:
        accepted = (
            response(f"http://127.0.0.1:{api}/transactions", payment)["txid"] == pid
        )
    except OSError as e:
        accepted = str(e)
    # The height and sampling delay are fixed before measuring readiness.
    time.sleep(schedule["offer_delay_ms"] / 1000)
    start = now()
    state = response(f"http://127.0.0.1:{api}/status")["mining"]
    arms = []
    jobs = []
    for i, node in enumerate((ln, rn)):
        label = (
            "witnessed"
            if i == 0 and schedule["arm"] == "witnessed/full"
            else ("full-left" if i == 0 else "full-right")
        )
        if label == "witnessed":
            reason = None
            j = None
            t = now()
            if (
                state["tip_hash"] == parent
                and state["observation_window_ready"]
                and state["pending_transactions"] == 1
            ):
                try:
                    j = stack.enter_context(
                        stratum_job(parent, payment, stratum, api, label)
                    )
                except (OSError, RuntimeError, ValueError, AssertionError) as e:
                    reason = str(e)
            else:
                reason = (
                    "; ".join(state["readiness_reasons"])
                    or "no eligible common-payment job"
                )
            latency = now() - t
        else:
            j, reason, latency = full_job(node, parent, payment, label)
        jobs.append(j)
        arms.append(
            {
                "role": label,
                "ready": j is not None,
                "readiness_reason": reason,
                "job_fetch_ns": latency,
                "hashes": 0,
                "discovery": None,
                "native_submit": None,
            }
        )
    offsets = [int(schedule["swap_nonce"]), int(not schedule["swap_nonce"])]
    for tick in range(64):
        for i, j in enumerate(jobs):
            if j is None:
                continue
            nonce = schedule["nonce"] + 2 * tick + offsets[i]
            header = j.header(nonce)
            arms[i]["hashes"] += 1
            if int.from_bytes(dsha(header), "little") <= REGTEST_TARGET:
                block = header + b"\x02" + j.coinbase + payment
                arms[i]["discovery"] = {
                    "hash": dsha(header)[::-1].hex(),
                    "nonce": nonce,
                    "wire_hex": block.hex(),
                }
        if any(a["discovery"] for a in arms) or not any(jobs):
            break

    indices = [i for i, a in enumerate(arms) if a["discovery"]]
    barrier = threading.Barrier(max(1, len(indices)))

    def publish(i):
        barrier.wait()
        t = now()
        try:
            r = jobs[i].submit_native(arms[i]["discovery"]["nonce"])
            message = {"ack": r}
        except Exception as e:
            message = {"error": str(e)}
        arms[i]["native_submit"] = {
            **message,
            "start_ns": t - start,
            "finish_ns": now() - start,
        }

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(publish, indices))
    receivers = [left_rx, right_rx]
    observed = [{}, {}]
    cutoff = time.monotonic() + 8
    while indices and time.monotonic() < cutoff:
        for i, rx in enumerate(receivers):
            tip = rx.rpc("getbestblockhash")
            if tip != parent:
                observed[i].setdefault(tip, now() - start)
        if all(observed):
            break
        time.sleep(0.02)
    follow = receivers[schedule["successor_receiver"]]
    # The receiver chosen to supply continuation work is drawn before the race.
    parent_height = follow.rpc("getblock", parent)["height"]
    remaining = parent_height + 3 - follow.rpc("getblockcount")
    assert remaining in (2, 3)
    tips = follow.rpc("generate", remaining)
    settled = tips[-1]
    until(
        lambda: all(n.rpc("getbestblockhash") == settled for n in nodes),
        "independent settlement",
    )
    end_height = follow.rpc("getblockcount")
    h = end_height - 2
    assert h == parent_height + 1
    selected = [rx.rpc("getblockhash", h) for rx in receivers]
    assert selected[0] == selected[1]
    for arm in arms:
        d = arm["discovery"]
        if d:
            d["settled"] = d["hash"] == selected[0]
    return {
        "index": schedule["index"],
        "arm": schedule["arm"],
        "height": h,
        "parent": parent,
        "payment_txid": pid,
        "scary_admission": accepted,
        "start_wall_time_ns": time.time_ns() - (now() - start),
        "producers": arms,
        "receiver_first_active_ns": observed,
        "selected_ancestry": selected,
        "settlement_tip": settled,
        "receiver_observation": "active hash polled by RPC after native submission",
        "ready_snapshot": {
            k: state[k]
            for k in (
                "tip_hash",
                "observation_window_ready",
                "pending_transactions",
                "readiness_reasons",
            )
        },
    }


def run(path, binary, seed, count, delay):
    rng = random.Random(seed)
    types = ["witnessed/full", "full/full"] * (count // 2)
    rng.shuffle(types)
    plan = [
        {
            "index": i,
            "arm": arm,
            "nonce": rng.randrange(2**31),
            "swap_nonce": bool(rng.randrange(2)),
            "successor_receiver": rng.randrange(2),
            "offer_delay_ms": delay,
        }
        for i, arm in enumerate(types)
    ]
    result = {
        "schema": 1,
        "seed": seed,
        "sv_image": IMAGE,
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "physical_swap": bool(rng.randrange(2)),
        "planned": plan,
        "rows": [],
        "complete": False,
    }

    def save():
        path.write_text(json.dumps(result, indent=2) + "\n")

    if path.exists():
        raise ValueError("session output already exists")
    save()
    with tempfile.TemporaryDirectory() as td, contextlib.ExitStack() as life:
        nodes = [Reference() for _ in range(4)]
        network = "scary-native-cohort-" + uuid.uuid4().hex
        subprocess.run(
            ["docker", "network", "create", network],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        life.callback(
            subprocess.run,
            ["docker", "network", "rm", network],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        for n in nodes:
            life.enter_context(n.running(peer=True, network=network, fast_rpc=True))
        if result["physical_swap"]:
            nodes = [nodes[1], nodes[0], nodes[3], nodes[2]]
        a, b, r1, r2 = nodes
        for x, y in ((a, r1), (b, r2), (r1, r2)):
            native_link(x, y)
        until(lambda: all(n.rpc("getconnectioncount") > 0 for n in nodes), "full peers")
        history = a.rpc("generate", 105)
        # Full references receive their common fixture history before the entrant
        # starts. Competitive discoveries are delivered exclusively by native peers.
        for h in history:
            raw = a.rpc("getblock", h, 0)
            for n in nodes[1:]:
                outcome = n.rpc("submitblock", raw)
                if outcome not in (None, "duplicate"):
                    raise RuntimeError(f"fixture ancestry: {outcome}")
        base = history[-1]
        until(
            lambda: all(n.rpc("getbestblockhash") == base for n in nodes),
            "full histories",
        )
        d = Path(td)
        feed = LabFeed(r1, d)
        life.callback(feed.server.shutdown)
        api, stratum = get_port(), get_port()
        life.enter_context(entrant(binary, r1, feed, d, api, stratum))
        url = f"http://127.0.0.1:{api}/status"
        until(
            lambda: response(url)["peer"]["connected_peer_ids_seen"]
            and response(url)["mining"]["directives"]["ready"],
            "governed entrant",
        )
        f, offers = funding_transactions(a, count)
        a.rpc("sendrawtransaction", f)
        parent = a.rpc("generate", 1)[0]
        until(
            lambda: all(n.rpc("getbestblockhash") == parent for n in nodes)
            and response(url)["mining"]["tip_hash"] == parent,
            "funding observations",
        )
        for schedule, payment in zip(plan, offers):
            with contextlib.ExitStack() as jobs:
                row = opportunity(schedule, nodes, api, stratum, jobs, payment, parent)
            result["rows"].append(row)
            save()
            parent = row["settlement_tip"]
        for row in result["rows"]:
            row["session_final_ancestry"] = [
                rx.rpc("getblockhash", row["height"]) for rx in (r1, r2)
            ]
        result["complete"] = True
        save()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("receipt", type=Path)
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--count", type=int, default=32)
    p.add_argument("--offer-delay-ms", type=int, default=1200)
    p.add_argument("--binary", type=Path, default=ROOT / "target/release/scary-node")
    args = p.parse_args()
    if args.count < 2 or args.count % 2:
        raise ValueError("positive even opportunity count required")
    run(args.receipt, args.binary, args.seed, args.count, args.offer_delay_ms)
