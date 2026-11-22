"""Inspect durable regtest block files and test normal restart, native reindex and exact-wire recovery."""

import argparse
import contextlib
import hashlib
import json
import subprocess
import time
import uuid
from pathlib import Path

from cadence_transport import MAGIC
from recover_sustained_chain import (
    FUNDING,
    HISTORY,
    confirmed_boundary,
    observed_wire_stream,
    path_at,
    read,
    selected_order,
)
from restore_witness_archive import canonical, docker, extract
from run_cohort import session_lock
from stock_wallet_archive import compare as compare_wallet
from stock_wallet_archive import fingerprint
from sustained_profiles import ENDORSEMENT
from sv_reference import IMAGE, Reference, RpcError
from verify_receipt import require
from verify_sustained_native import dsha


def identity(wire):
    return dsha(wire[:80])[::-1].hex()


def expected_chain(receipt, history, corpus):
    expected = {}
    aggregate = hashlib.sha256()

    def add(height, raw):
        h = identity(raw)
        dg = hashlib.sha256(raw).digest()
        expected[h] = {
            "height": height,
            "wire_sha256": dg.hex(),
            "wire_bytes": len(raw),
        }
        return dg

    for height in range(1, 484):
        aggregate.update(add(height, path_at(history, height).read_bytes()))
    require(aggregate.hexdigest() == HISTORY, "unexpected retained funding history")
    add(484, (corpus / "funding.block").read_bytes())
    require(FUNDING in expected, "different split payment funding")
    if receipt.get("arm") == "endorsed":
        add(485, ENDORSEMENT)
    for row in receipt["blocks"]:
        expected[row["hash"]] = {
            k: row[k] for k in ("height", "wire_sha256", "wire_bytes")
        }
    return expected


def inspect_block_file(stream, expected, found, other, framing):
    count = padding = 0
    while prefix := stream.read(8):
        if not prefix.strip(b"\x00"):
            padding += len(prefix)
            for tail in iter(lambda: stream.read(1 << 20), b""):
                require(
                    not tail.strip(b"\x00"),
                    "unexpected nonzero bytes after block-file padding",
                )
                padding += len(tail)
            break
        require(
            len(prefix) == 8 and prefix[:4] in (MAGIC, MAGIC[::-1]),
            "unexpected stock block-file framing",
        )
        framing.add(prefix[:4].hex())
        size = int.from_bytes(prefix[4:], "little")
        require(81 <= size < 2**31, "unrecognized measured data-file block size")
        raw = stream.read(size)
        require(len(raw) == size, "truncated stock block file")
        h = identity(raw)
        ex = expected.get(h)
        if ex:
            require(
                (size, hashlib.sha256(raw).hexdigest())
                == (ex["wire_bytes"], ex["wire_sha256"]),
                "durable block differs from original",
            )
            found.add(h)
        else:
            other.add(h)
        count += 1
    return count, padding


def durable_blocks(cid, expected):
    files = docker(
        "exec",
        cid,
        "sh",
        "-ec",
        'for f in /frozen/regtest/blocks/blk*.dat; do test -f "$f" && printf "%s\\n" "$f"; done',
    ).splitlines()
    require(bool(files), "no durable stock blk data files")
    found = set()
    other = set()
    details = []
    framing = set()
    for path in sorted(files):
        with subprocess.Popen(
            ["docker", "exec", cid, "cat", path], stdout=subprocess.PIPE
        ) as proc:
            count, padding = inspect_block_file(
                proc.stdout, expected, found, other, framing
            )
            require(proc.wait() == 0, "failed to read durable block file")
        details.append(
            {"file": Path(path).name, "blocks": count, "zero_padding_bytes": padding}
        )
    return {
        "files": details,
        "framing": sorted(framing),
        "exact_selected_blocks_on_disk": len(found),
        "missing_selected_heights": [
            v["height"] for k, v in expected.items() if k not in found
        ],
        "other_recorded_hashes": sorted(other),
        "selected_wire_sha256_commitment": hashlib.sha256(
            b"".join(
                bytes.fromhex(r["wire_sha256"])
                for k, r in expected.items()
                if k in found
            )
        ).hexdigest(),
    }


def inspect_tip(stock, target):
    info = stock.rpc("getblockchaininfo")
    row = {
        "height": info["blocks"],
        "hash": info["bestblockhash"],
        "headers": info["headers"],
        "work": info["chainwork"],
    }
    for method, args in (("getblockheader", (target, True)), ("getblock", (target, 1))):
        try:
            res = stock.rpc(method, *args)
            row[method] = {k: res.get(k) for k in ("height", "hash", "confirmations")}
        except RpcError:
            row[method] = None
    return row


def data_root(cid, root):
    terminal = root + "/regtest"
    names = docker(
        "exec", cid, "find", root, "-mindepth", "1", "-maxdepth", "1", "-printf", "%f\n"
    ).splitlines()
    require(
        names == ["regtest"],
        "SV archive root must contain precisely the original network directory",
    )
    data = {}
    for name in (
        "wallet.dat",
        "blocks/blk00000.dat",
        "blocks/index/CURRENT",
        "chainstate/CURRENT",
    ):
        path = terminal + "/" + name
        data[name] = (
            int(docker("exec", cid, "stat", "-c", "%s", path)),
            docker("exec", cid, "sha256sum", path).split()[0],
        )
    return names, data


def live_arguments(cid):
    cmd = docker(
        "exec",
        cid,
        "sh",
        "-ec",
        'pid=$(cat /data/regtest/bitcoind.pid); tr "\\000" "\\n" < "/proc/$pid/cmdline"',
    ).splitlines()
    wanted = {
        "-regtest",
        "-datadir=/data",
        "-listen=0",
        "-connect=0",
        "-txindex=1",
        "-genesisactivationheight=1",
        "-chronicleactivationheight=2",
    }
    require(
        wanted <= set(cmd),
        "actual stock process is not using the recovered regtest datadir",
    )
    return [a for a in cmd if a in wanted or a in ("-reindex=1", "-keypool=1")]


def normal_or_reindex(
    stock, target, frozen_layout, openings, *, reindex=False, preserve_wallet=False
):
    cid = stock.container
    docker("exec", cid, "cp", "-a", "/frozen/.", "/data/")
    copied = data_root(cid, "/data")
    require(
        copied == frozen_layout,
        "actual working data root differs from the original before stock startup",
    )
    stage = "reindex" if reindex else "normal"
    openings[stage] = {
        "network_root": "/data/regtest",
        "root_entries": copied[0],
        "byte_equal_probe_files": {k: v[0] for k, v in copied[1].items()},
    }

    args = [
        "exec",
        "-d",
        cid,
        "bitcoind",
        "-regtest",
        "-datadir=/data",
        "-listen=0",
        "-connect=0",
        "-dnsseed=0",
        "-discover=0",
        "-server=1",
        "-txindex=1",
        "-minminingtxfee=0.00000500",
        "-genesisactivationheight=1",
        "-chronicleactivationheight=2",
        "-excessiveblocksize=4000000000",
        "-maxstackmemoryusageconsensus=100000000",
        "-checkmempool=0",
        "-checkblockindex=0",
    ]
    if preserve_wallet:
        # SV Node tops up its keypool when it opens a wallet. A target of one
        # keeps an already populated regtest pool from deriving new reserve keys
        # during an exact comparison with the original paused wallet.
        args.append("-keypool=1")
    if reindex:
        args.append("-reindex=1")
    docker(*args)
    started = time.monotonic()
    last_progress = started
    deadline = started + (1500 if reindex else 240)
    timeline = []
    while time.monotonic() < deadline:
        now = time.monotonic()
        try:
            snap = inspect_tip(stock, target)
        except RpcError:
            time.sleep(1)
            continue
        if not timeline:
            openings[stage]["observed_process_flags"] = live_arguments(cid)
        if not timeline or snap != timeline[-1]["boundary"]:
            last_progress = now
        if (
            not timeline
            or snap != timeline[-1]["boundary"]
            or now - started - timeline[-1]["elapsed_s"] >= 10
        ):
            timeline.append({"elapsed_s": now - started, "boundary": snap})
        if snap["hash"] == target:
            return timeline
        if now - last_progress >= (180 if reindex else 65):
            return timeline
        time.sleep(1)
    require(timeline, "archived stock never returned a responsive regtest RPC")
    return timeline


def clean_working_copy(stock):
    cid = stock.container
    stock.rpc("stop")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        # The stock image removes its pidfile at the end of a clean shutdown.
        res = subprocess.run(
            ["docker", "exec", cid, "test", "-e", "/data/regtest/bitcoind.pid"],
            capture_output=True,
            check=False,
        )
        if res.returncode == 1:
            break
        require(res.returncode == 0, "cannot observe stock pidfile during shutdown")
        time.sleep(0.5)
    else:
        raise TimeoutError("archived stock did not exit")
    # Only this task's temporary working copy is reset. /frozen keeps the original.
    docker("exec", cid, "find", "/data", "-mindepth", "1", "-delete")


def reoffer(stock, receipt, measured, history, corpus, archive, blocks):
    beginning = stock.rpc("getblockcount")
    require(
        beginning < confirmed_boundary(receipt)["final_height"],
        "unexpected initial restored height",
    )
    previous = stock.rpc("getbestblockhash")
    actions = []

    def submit(height, wire):
        nonlocal previous
        h = identity(wire)
        if height <= beginning:
            if height == beginning:
                require(
                    h == previous,
                    "archive did not recover a prefix of the original branch",
                )
            return
        require(
            wire[4:36][::-1].hex() == previous, "retained suffix has different parent"
        )
        answer = stock.rpc("submitblock", wire.hex())
        require(
            answer in (None, "duplicate") and stock.rpc("getblockhash", height) == h,
            "stock did not select reoffered block",
        )
        previous = h
        actions.append(
            {
                "height": height,
                "hash": h,
                "response": answer,
                "bytes": len(wire),
                "wire_sha256": hashlib.sha256(wire).hexdigest(),
            }
        )

    for height in range(1, 484):
        submit(height, path_at(history, height).read_bytes())
    submit(484, (corpus / "funding.block").read_bytes())
    if receipt.get("arm") == "endorsed":
        submit(485, ENDORSEMENT)
    wires = observed_wire_stream(measured, corpus, blocks, archive)
    for row, raw in selected_order(receipt["blocks"], wires, {}):
        submit(row["height"], raw)
    return actions


def boot(
    measured,
    history,
    corpus,
    archived_data,
    archive=None,
    blocks=None,
    *,
    original_wallet=None,
    original_pending_ids=None,
    retain_private_result=None,
):
    receipt = read(measured)
    expected = expected_chain(receipt, history, corpus)
    target = confirmed_boundary(receipt)["final_hash"]
    name = "scary-archived-sv-" + uuid.uuid4().hex[:12]
    with contextlib.ExitStack() as cleanup:
        cid = docker(
            "run",
            "-d",
            "--platform",
            "linux/amd64",
            "--name",
            name,
            "--network",
            "none",
            "--pull=never",
            "--log-driver",
            "none",
            "--read-only",
            "--tmpfs",
            "/frozen:rw,size=5g",
            "--tmpfs",
            "/data:rw,size=8g",
            "--tmpfs",
            "/tmp:size=64m",
            "--entrypoint",
            "sleep",
            IMAGE,
            "infinity",
        )
        cleanup.callback(docker, "rm", "--force", cid)
        stock = Reference()
        stock.container = cid
        extract(cid, archived_data, "/frozen")
        wallet_bytes = int(
            docker("exec", cid, "stat", "-c", "%s", "/frozen/regtest/wallet.dat")
        )
        require(wallet_bytes > 0, "archive did not contain stock wallet data")
        disk = durable_blocks(cid, expected)
        frozen_layout = data_root(cid, "/frozen")
        openings = {}
        compare_original = original_wallet is not None
        stages = {
            "normal": normal_or_reindex(
                stock, target, frozen_layout, openings, preserve_wallet=compare_original
            )
        }
        method = "normal"
        if stock.rpc("getbestblockhash") != target:
            clean_working_copy(stock)
            stages["reindex"] = normal_or_reindex(
                stock,
                target,
                frozen_layout,
                openings,
                reindex=True,
                preserve_wallet=compare_original,
            )
            method = "native reindex of archived block files"
        offers = []
        if stock.rpc("getbestblockhash") != target:
            require(
                archive or blocks,
                "an exact signed-source ordinal archive or raw block directory is needed for remaining suffix",
            )
            offers = reoffer(stock, receipt, measured, history, corpus, archive, blocks)
            method = "native reindex and reoffer of exact retained suffix"
        info = inspect_tip(stock, target)
        pending = {}
        expected_pool = 0
        if original_pending_ids is not None:
            from interrupted_workload import restore_stock_mempool

            restored = restore_stock_mempool(
                stock, receipt, corpus, original_pending_ids, allow_retained=True
            )
            pending["original_pending_recovery"] = restored
            expected_pool = restored["restored_count"]
        pool = stock.rpc("getmempoolinfo")["size"]
        activity = stock.rpc("getblockchainactivity")
        wallet = stock.rpc("getwalletinfo")
        private_wallet = fingerprint(cid)
        if retain_private_result:
            retain_private_result(private_wallet)
        comparison = (
            compare_wallet(private_wallet, original_wallet) if original_wallet else None
        )
        require(
            info["hash"] == target
            and info["height"] == confirmed_boundary(receipt)["final_height"]
            and pool == expected_pool
            and stock.rpc("getconnectioncount") == activity["transactions"] == 0,
            "complete recovery or zero-peer boundary differs",
        )
        return {
            "network": "isolated regtest",
            "image": IMAGE,
            "measured_record_sha256": canonical(receipt),
            "archived_wallet_bytes": wallet_bytes,
            "verified_data_roots": openings,
            "wallet_loaded": True,
            "original_wallet_comparison": comparison,
            **pending,
            "wallet_transaction_count": wallet["txcount"],
            "wallet_version": wallet["walletversion"],
            "first_tips_and_progress": stages,
            "durable_block_files": disk,
            "reoffered_blocks": offers,
            "recovery_method": method,
            "terminal": info,
            "mempool": pool,
            "validation_activity": activity,
            "peers": 0,
        }


def verify(proof, receipt):
    final = confirmed_boundary(receipt)
    stages = proof["first_tips_and_progress"]
    disk = proof["durable_block_files"]
    require(
        proof["image"] == IMAGE
        and proof["measured_record_sha256"] == canonical(receipt)
        and proof["network"] == "isolated regtest",
        "different original or stock image",
    )
    expected_pool = 0
    if "original_pending_recovery" in proof:
        from interrupted_workload import verify as verify_interruption

        interrupted = verify_interruption(receipt)
        pool = proof["original_pending_recovery"]
        expected_pool = interrupted["final_stock_mempool_after_interruption"]
        require(
            pool["restored_count"]
            == expected_pool
            == pool["retained_initial_count"] + pool["reoffered"]
            and min(pool["retained_initial_count"], pool["reoffered"]) >= 0
            and pool["sorted_internal_txids_sha256"]
            == interrupted["sorted_internal_mempool_txids_sha256"],
            "restored stock pending payments differ from original",
        )
    require(
        proof["terminal"]["height"] == final["final_height"]
        and proof["terminal"]["hash"] == final["final_hash"]
        and proof["mempool"] == expected_pool
        and proof["peers"] == proof["validation_activity"]["transactions"] == 0,
        "final stock state differs",
    )
    require(
        len(set(disk["missing_selected_heights"]))
        + disk["exact_selected_blocks_on_disk"]
        == final["final_height"]
        and len(bytes.fromhex(disk["selected_wire_sha256_commitment"])) == 32
        and proof["wallet_loaded"]
        and proof["archived_wallet_bytes"] > 0,
        "incomplete original disk or wallet accounting",
    )
    require(
        stages["normal"]
        and all(s[-1]["elapsed_s"] >= s[0]["elapsed_s"] for s in stages.values()),
        "missing first stock RPC",
    )
    require(
        all(1 <= h <= final["final_height"] for h in disk["missing_selected_heights"]),
        "invalid missing data-file height",
    )
    known = {r["height"]: r for r in receipt["blocks"]}
    for row in proof["reoffered_blocks"]:
        if row["height"] in known:
            item = known[row["height"]]
            require(
                (row["hash"], row["wire_sha256"], row["bytes"])
                == (item["hash"], item["wire_sha256"], item["wire_bytes"]),
                "reoffered block differs from original bytes",
            )
    require(
        set(proof["verified_data_roots"]) == set(stages),
        "missing restored data-directory proof",
    )
    for key, entry in proof["verified_data_roots"].items():
        flags = set(entry["observed_process_flags"])
        require(
            entry["root_entries"] == ["regtest"]
            and entry["network_root"] == "/data/regtest"
            and entry["byte_equal_probe_files"]["wallet.dat"]
            == proof["archived_wallet_bytes"]
            and {"-regtest", "-datadir=/data", "-listen=0", "-connect=0", "-txindex=1"}
            <= flags
            and ("-reindex=1" in flags) == (key == "reindex"),
            "different original storage root or stock process",
        )
    if (matched := proof.get("original_wallet_comparison")) is not None:
        require(
            all(
                "-keypool=1" in entry["observed_process_flags"]
                for entry in proof["verified_data_roots"].values()
            ),
            "missing restored observation of the wallet keypool setting",
        )
        require(
            matched["all_three_private_commitments_equal"] is True
            and matched["complete_wallet_transaction_records_equal"] is True
            and matched["wallet_transaction_records"]
            == matched["wallet_transaction_wires"]
            == proof["wallet_transaction_count"]
            and matched["key_metadata_rows"] > 0
            and matched["private_keys_and_master_rows"]
            in (matched["key_metadata_rows"], matched["key_metadata_rows"] + 1)
            and matched["spendable_outpoints"] >= 0,
            "inconsistent private wallet comparison",
        )
    method = proof["recovery_method"]
    if method == "normal":
        require(
            len(stages) == 1
            and not proof["reoffered_blocks"]
            and stages["normal"][-1]["boundary"]["hash"] == final["final_hash"],
            "normal startup result differs",
        )
    else:
        require(stages.get("reindex"), "native reindex evidence missing")
        if proof["reoffered_blocks"]:
            start = stages["reindex"][-1]["boundary"]["height"]
            require(
                method == "native reindex and reoffer of exact retained suffix"
                and [r["height"] for r in proof["reoffered_blocks"]]
                == list(range(start + 1, final["final_height"] + 1))
                and proof["reoffered_blocks"][-1]["hash"] == final["final_hash"],
                "wrong reoffered suffix",
            )
        else:
            require(
                method == "native reindex of archived block files"
                and stages["reindex"][-1]["boundary"]["hash"] == final["final_hash"],
                "native reindex result differs",
            )
    return {
        "first_responding_height": stages["normal"][0]["boundary"]["height"],
        "durable_selected_blocks": disk["exact_selected_blocks_on_disk"],
        "method": method,
        "reoffered": len(proof["reoffered_blocks"]),
        "terminal_height": final["final_height"],
        "peers": 0,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    s = p.add_subparsers(dest="action", required=True)
    b = s.add_parser("boot")
    b.add_argument("measured", type=Path)
    b.add_argument("history", type=Path)
    b.add_argument("corpus", type=Path)
    b.add_argument("data_tar", type=Path)
    b.add_argument("proof", type=Path)
    b.add_argument("--original-pending-ids", type=Path)
    b.add_argument(
        "--private-wallet-summary",
        type=Path,
        help="optional protected original aggregate wallet summary",
    )
    a = b.add_mutually_exclusive_group()
    a.add_argument("--archive", type=Path)
    a.add_argument("--blocks", type=Path)
    v = s.add_parser("verify")
    v.add_argument("measured", type=Path)
    v.add_argument("proof", type=Path)
    a = p.parse_args()
    if a.action == "boot":
        if a.proof.exists():
            p.error("choose a new proof")
        private = read(a.private_wallet_summary) if a.private_wallet_summary else None
        if private:
            require(
                private["source_tip"]
                == confirmed_boundary(read(a.measured))["final_hash"],
                "private manifest belongs to another observed tip",
            )
        with session_lock():
            evidence = boot(
                a.measured,
                a.history,
                a.corpus,
                a.data_tar,
                a.archive,
                a.blocks,
                original_wallet=private["wallet"] if private else None,
                original_pending_ids=a.original_pending_ids,
            )
            verify(evidence, read(a.measured))
            with a.proof.open("x") as f:
                json.dump(evidence, f, indent=2)
                f.write("\n")
    print(json.dumps(verify(read(a.proof), read(a.measured)), indent=2))
