"""Replay the selected funding chain and its original signed freeze on two stock Teranodes."""

import argparse
import gzip
import hashlib
import json
import struct
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError

from run_cohort import session_lock
from sv_reference import IMAGE as SV_IMAGE
from sv_reference import Reference
from teranode_alerts import (
    IMAGE,
    build_publisher,
    request,
    rpc,
    stock,
    stored_alerts,
    txid,
    wait,
)
from verify_dar_history import (
    REGTEST_GENESIS,
    decode_chain,
    require_lab_signers,
    spends_of,
)
from verify_receipt import require


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def prefix(path):
    wire = path.read_bytes()
    archive = json.loads(gzip.decompress(wire))
    signed = archive["signed_records"][0]
    raw = bytes.fromhex(signed["raw"])
    payload = raw[:-195]
    version, number, _, kind = struct.unpack("<IIQI", payload[:20])
    require(
        (version, number, kind, signed["sequence_number"]) == (1, 1, 2, 1),
        "expected the first signed freeze",
    )
    keys = archive["genesis_keys"]
    require_lab_signers(payload, raw[-195:], set(keys))
    ident = payload[20:52].hex()
    index, start, stop, expires = struct.unpack("<QQQB", payload[52:])
    require(
        stop == 2**31 - 1 and expires == 0 and start > 1, "unexpected freeze interval"
    )
    bodies = archive["active_history_from_height_one"][: start - 1]
    chain = decode_chain(bodies, REGTEST_GENESIS, 1)
    require(
        len(chain) == start - 1
        and chain[start - 1][0] == archive["confiscation_parent"],
        "wrong funding boundary",
    )
    found = [
        (height, outs[index])
        for height, (_, txs, outputs) in chain.items()
        for tx, outs in zip(txs, outputs, strict=True)
        if txid(tx) == ident
    ]
    require(
        len(found) == 1 and not spends_of(chain, (ident, index)),
        "funding outpoint missing, duplicated or spent",
    )
    height, (amount, script) = found[0]
    return (
        keys,
        signed,
        bodies,
        {
            "source_archive_sha256": sha(wire),
            "funding_height": height,
            "txid": ident,
            "vout": index,
            "satoshis": amount,
            "locking_script_hex": script.hex(),
            "parent_height": len(bodies),
            "parent_hash": chain[len(bodies)][0],
            "freeze_start": start,
            "freeze_stop": stop,
            "policy_expires_with_consensus": bool(expires),
            "signed_message_sha256": sha(raw),
            "signed_sequence": number,
        },
    )


def freeze_interval(funding):
    return {
        "txOut": {"txId": funding["txid"], "vout": funding["vout"]},
        "enforceAtHeight": [
            {"start": funding["freeze_start"], "stop": funding["freeze_stop"]}
        ],
        "policyExpiresWithConsensus": funding["policy_expires_with_consensus"],
    }


def body_record(body):
    return {"hash": txid(body[:80]), "wire_bytes": len(body), "wire_sha256": sha(body)}


def stock_body(url):
    # A default stock Asset server permits ten heavy requests per second.
    # Pace archival reads and honor the receiver's advertised retry interval.
    time.sleep(0.125)
    for attempt in range(4):
        try:
            return request(url)
        except RuntimeError as error:
            response = error.__cause__
            if (
                not isinstance(response, HTTPError)
                or response.code != 429
                or attempt == 3
            ):
                raise
            time.sleep(max(1, int(response.headers.get("Retry-After", "1"))))


def verify(path, result):
    _, signed, bodies, funding = prefix(path)
    require(
        funding == result["funding_boundary"],
        "different source, signed freeze or funding boundary",
    )
    require(
        result["teranode_image"] == IMAGE and result["sv_image"] == SV_IMAGE,
        "wrong stock images",
    )
    expected = [body_record(bytes.fromhex(body)) for body in bodies]
    parent = funding["parent_hash"]
    sv = result["sv"]
    require(
        sv["selected_parent"] == parent and sv["funding_output"]["bestblock"] == parent,
        "stock SV parent differs",
    )
    require(
        sv["before_freeze"] == {"funds": []}
        and sv["apply_freeze"] == {"notProcessed": []},
        "unexpected stock SV initial state or freeze response",
    )
    require(
        sv["after_freeze"]
        == {
            "funds": [
                {**freeze_interval(funding), "blacklist": ["policy", "consensus"]}
            ]
        },
        "stock SV reported a different freeze interval or outpoint",
    )
    require(sv["sync_redial_heights"] in ([], [12]), "unexpected legacy sync redial")
    coin = sv["funding_output"]
    require(
        round(coin["value"] * 100_000_000) == funding["satoshis"]
        and coin["scriptPubKey"]["hex"] == funding["locking_script_hex"]
        and coin["confirmations"]
        == funding["parent_height"] - funding["funding_height"] + 1
        and coin["coinbase"] is False
        and coin["confiscation"] is False,
        "stock SV funding output differs",
    )
    require(
        set(result["teranodes"]) == {"tn1", "tn2"},
        "expected two independent references",
    )
    for node in result["teranodes"].values():
        require(
            node["asset_readbacks"] == expected and node["selected_parent"] == parent,
            "stock Teranode chain readbacks differ",
        )
        require(
            node["before_freeze"] == {"status": 0}
            and node["after_freeze"]
            == {
                "status": 5,
                "spendingData": {"txId": "ff" * 32, "vin": funding["vout"]},
            },
            "wrong funded output states",
        )
        require(
            node["processed_records"] == [{**signed, "processed": True}],
            "wrong stored signed prefix",
        )
    return {
        "common_height": len(bodies),
        "common_parent": parent,
        "asset_readbacks": 2 * len(bodies),
        "funding_txid": funding["txid"],
        "vout": funding["vout"],
        "satoshis": funding["satoshis"],
        "freeze_sequence": signed["sequence_number"],
        "frozen_teranodes": 2,
    }


def measure(path, directory):
    keys, signed, bodies, funding = prefix(path)
    publisher = build_publisher()
    with stock(keys, directory, with_sv=True) as (one, two, port, peer_id, container):
        sv = Reference()
        sv.container = container("ps", "-q", "svref")
        sv.ready()
        require(
            sv.rpc("getblockhash", 0) == REGTEST_GENESIS, "unexpected stock network"
        )

        def legacy_peer():
            return next(
                (
                    peer
                    for peer in sv.rpc("getpeerinfo")
                    if "teranode" in peer["subver"].lower()
                ),
                None,
            )

        wait("stock SV/Teranode connection", legacy_peer)
        redials = []
        for height, raw in enumerate(bodies, 1):
            require(
                sv.rpc("submitblock", raw) is None, "stock SV declined a funding block"
            )
            if height == 12:
                # A legacy handshake at genesis can precede a viable sync tip.
                # Let the stock legacy peer request the available funding headers.
                if rpc(two, "getbestblockhash") == REGTEST_GENESIS:
                    sv.rpc(
                        "disconnectnode", wait("stock legacy peer", legacy_peer)["addr"]
                    )
                    redials.append(height)
                early = txid(bytes.fromhex(raw)[:80])
                wait(
                    "stock legacy funding sync",
                    lambda early=early: rpc(two, "getbestblockhash") == early,
                )
        parent = funding["parent_hash"]
        require(
            sv.rpc("getbestblockhash") == parent,
            "stock SV failed to select funding prefix",
        )
        sv_coin = sv.rpc("gettxout", funding["txid"], funding["vout"])
        result = {
            "funding_boundary": funding,
            "sv_image": SV_IMAGE,
            "teranode_image": IMAGE,
            "sv": {
                "funding_output": sv_coin,
                "selected_parent": parent,
                "sync_redial_heights": redials,
            },
            "teranodes": {},
        }
        peers = {"tn1": one, "tn2": two}

        def utxo(peer):
            ident, index = funding["txid"], funding["vout"]
            return json.loads(
                request(
                    f"http://127.0.0.1:{peer[1]}/api/v1/utxo/{ident}/json?vout={index}"
                )
            )

        for name, peer in peers.items():
            wait(
                f"{name} selected archived funding",
                lambda peer=peer: rpc(peer, "getbestblockhash") == parent,
            )
            readbacks = []
            for raw in bodies:
                want = bytes.fromhex(raw)
                ident = txid(want[:80])
                got = stock_body(
                    f"http://127.0.0.1:{peer[1]}/api/v1/block_legacy/{ident}?wire=1"
                )
                require(got == want, "Teranode funding body differs")
                readbacks.append(body_record(got))
            before = utxo(peer)
            require(
                before["status"] == 0,
                "expected an available funded output before the signed freeze",
            )
            result["teranodes"][name] = {
                "asset_readbacks": readbacks,
                "before_freeze": before,
            }
        result["sv"]["before_freeze"] = sv.rpc("queryBlacklist")
        result["sv"]["apply_freeze"] = sv.rpc(
            "addToConsensusBlacklist", {"funds": [freeze_interval(funding)]}
        )
        result["sv"]["after_freeze"] = sv.rpc("queryBlacklist")
        with subprocess.Popen(
            [str(publisher), str(port), peer_id],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        ) as sender:
            sender.stdin.write(signed["raw"] + "\n")
            sender.stdin.flush()
            require(
                sender.stdout.readline().strip() == "published",
                "signed freeze publication failed",
            )
            for name, peer in peers.items():

                def frozen(peer=peer):
                    status = utxo(peer)
                    return status if status["status"] == 5 else None

                result["teranodes"][name]["after_freeze"] = wait(
                    f"{name} funded output frozen", frozen
                )
                result["teranodes"][name]["selected_parent"] = rpc(
                    peer, "getbestblockhash"
                )
        for name in peers:
            result["teranodes"][name]["processed_records"] = stored_alerts(
                container, name, directory
            )
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["run", "verify"])
    p.add_argument("source", type=Path, help="published signed SV regtest history")
    p.add_argument("receipt", type=Path)
    a = p.parse_args()
    if a.action == "run":
        if a.receipt.exists():
            p.error("choose a fresh receipt path")
        with (
            session_lock(),
            tempfile.TemporaryDirectory(prefix="scary-funding-") as tmp,
        ):
            result = measure(a.source, Path(tmp))
        with a.receipt.open("x") as f:
            print(json.dumps(result, indent=2), file=f)
    else:
        result = json.loads(a.receipt.read_bytes())
    print(json.dumps(verify(a.source, result), indent=2))
