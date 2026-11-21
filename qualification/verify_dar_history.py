"""Replay the signed stock-SV DAR fixture from regtest genesis through both recipient branches."""

import argparse
import gzip
import hashlib
import json
import struct
from pathlib import Path
from ecdsa import SECP256k1, VerifyingKey, util
from verify_teranode_alerts import REGTEST_GENESIS, require_lab_signers
from prove import decode_block, dsha, parse_tx, read_varint, txid
from verify_receipt import check_header, require, single_input
from sv_dar import confiscate


def decode_chain(bodies, previous, start_height):
    blocks = {}
    for height, wire in enumerate(bodies, start_height):
        body = bytes.fromhex(wire)
        h = txid(body[:80])
        check_header(body[:80], h, previous)
        _, txs, outputs = decode_block(body, h)
        blocks[height] = (h, txs, outputs)
        previous = h
    return blocks


def verify_signed_actions(record):
    keys = set(record["genesis_keys"])
    kinds = [2, 4, 1, 2, 3, 5, 6, 7, 8, 1]
    payloads = []
    for seq, (kind, row) in enumerate(
        zip(kinds, record["signed_records"], strict=True), 1
    ):
        raw = bytes.fromhex(row["raw"])
        payload, signatures = raw[:-195], raw[-195:]
        version, signed_seq, _, signed_kind = struct.unpack("<IIQI", payload[:20])
        require(
            (version, signed_seq, signed_kind, row["sequence_number"])
            == (1, seq, kind, seq),
            "signed directive order differs",
        )
        require_lab_signers(payload, signatures, keys)
        action = payload[20:]
        if kind == 8:
            require(len(action) == 5 * 33, "unexpected rotation payload")
            rotated = {action[i : i + 33].hex() for i in range(0, len(action), 33)}
            require(
                len(rotated) == 5 and rotated.isdisjoint(keys),
                "unexpected rotated key set",
            )
            keys = rotated
        payloads.append(action)
    receipt = record["receiver"]
    require(
        receipt["sequence"] == receipt["last_peer_sync"]["sequence"] == len(kinds)
        and receipt["unprocessed_alerts"] == 0
        and receipt["active_peers"] > 0,
        "incomplete signed receiver prefix",
    )
    for seq, (kind, row) in enumerate(
        zip(kinds[2:], record["directive_lifecycle"], strict=True), 3
    ):
        state = row["directive_state"]
        require(
            (
                row["kind"],
                row["sequence"],
                state["last_verified"],
                state["last_applied"],
                state["ready"],
            )
            == (kind, seq, seq, seq, True),
            "application receipt differs",
        )
    require(
        payloads[7][:32][::-1].hex() == record["confirmed_hash"],
        "invalidation refers to a different branch",
    )
    return payloads


def verify_owner_signature(spend, amount, script):
    # This wallet fixture has one P2PKH input and one output, using SIGHASH_ALL|FORKID.
    _, _, unlocking = single_input(spend)
    signature, pub = unlocking[1 : 1 + unlocking[0]], unlocking[2 + unlocking[0] :]
    require(
        len(pub) == unlocking[1 + unlocking[0]] == 33 and signature[-1] == 0x41,
        "unexpected recipient signing form",
    )
    pkh = hashlib.new("ripemd160", hashlib.sha256(pub).digest()).digest()
    require(
        script == b"\x76\xa9\x14" + pkh + b"\x88\xac",
        "recipient key differs from recovered ownership",
    )
    sequence_offset = 42 + len(unlocking)
    sequence = spend[sequence_offset : sequence_offset + 4]
    count, start = read_varint(spend, sequence_offset + 4)
    require(count == 1 and len(script) < 253, "unexpected fixture outputs")
    outpoint = spend[5:41]
    preimage = (
        spend[:4]
        + dsha(outpoint)
        + dsha(sequence)
        + outpoint
        + bytes([len(script)])
        + script
        + struct.pack("<Q", amount)
        + sequence
        + dsha(spend[start:-4])
        + spend[-4:]
        + struct.pack("<I", 0x41)
    )
    key = VerifyingKey.from_string(pub, curve=SECP256k1)
    require(
        key.verify_digest(signature[:-1], dsha(preimage), sigdecode=util.sigdecode_der),
        "recipient signature failed",
    )


def spends_of(chain, outpoint):
    matches = []
    for height, (_, transactions, _) in chain.items():
        for raw in transactions:
            count, offset = read_varint(raw, 4)
            for _ in range(count):
                previous = raw[offset : offset + 32][::-1].hex()
                index = int.from_bytes(raw[offset + 32 : offset + 36], "little")
                if (previous, index) == outpoint:
                    matches.append((height, txid(raw)))
                size, script = read_varint(raw, offset + 36)
                offset = script + size + 4
    return matches


def verify(record):
    actions = verify_signed_actions(record)
    require(
        len(bytes.fromhex(record["node_binary_sha256"])) == 32, "missing release digest"
    )
    source = actions[0][:32].hex()
    vout, start, stop, policy_expires = struct.unpack("<QQQB", actions[0][32:])
    require(
        stop == 2**31 - 1 and not policy_expires, "unexpected signed freeze interval"
    )
    auth = actions[1]
    size, offset = read_varint(auth, 8)
    recovery = bytes.fromhex(record["confiscation_hex"])
    require(
        struct.unpack("<Q", auth[:8])[0] == start
        and len(recovery) == size
        and auth[offset:] == recovery,
        "authorization does not cover exact transaction and height",
    )
    recovered_id = txid(recovery)
    require(recovered_id == record["confiscation_txid"], "recovery txid differs")
    _, recovered_outs, end = parse_tx(recovery, 0)
    require(
        len(recovery) == end and len(recovered_outs) == 2, "unexpected recovery outputs"
    )
    amount, recipient_script = recovered_outs[1]
    require(
        recovery.hex() == confiscate(source, vout, recipient_script),
        "recovery differs from prescribed funded fixture",
    )
    spend = bytes.fromhex(record["recipient_spend_hex"])
    spend_id = txid(spend)
    require(
        spend_id == record["recipient_spend"]
        and single_input(spend)[:2] == (recovered_id, 1),
        "recipient spend uses different output",
    )
    verify_owner_signature(spend, amount, recipient_script)
    maturity = start + 1000
    require(record["maturity_height"] == maturity, "wrong maturity height")
    active = decode_chain(record["active_history_from_height_one"], REGTEST_GENESIS, 1)
    require(
        active[len(active)][0] == record["sv_final_active_tip"],
        "SV final active hash differs",
    )
    found = [
        (height, outs[vout][0])
        for height, (_, txs, outputs) in active.items()
        for raw, outs in zip(txs, outputs, strict=True)
        if txid(raw) == source
    ]
    require(
        len(found) == 1 and found[0][0] < start and found[0][1] == 100_000_000,
        "funded frozen output differs",
    )
    require(
        active[start][0] == record["confiscation_block"]
        and recovery in active[start][1]
        and active[start - 1][0] == record["confiscation_parent"],
        "original recovery missing",
    )
    require(
        record["active_history_from_height_one"][start - 1]
        == record["confiscation_block_hex"],
        "recovery block wire changed",
    )
    early = record["sv_maturity_minus_one"]
    require(
        early["candidate_height"] == maturity - 1
        and early["parent"] == active[maturity - 2][0]
        and "premature-spend-of-confiscation" in early["response"],
        "early SV disposition missing",
    )
    require(
        record["maturity_minus_one"]["outcome"] == "invalid"
        and "immature confiscation" in record["maturity_minus_one"]["reason"],
        "early entrant disposition missing",
    )
    parent = active[maturity - 1][0]
    detached = decode_chain(record["detached_recipient_branch"], parent, maturity)
    require(
        parent == record["recipient_parent"]
        and len(detached) == 3
        and detached[maturity][0] == record["confirmed_hash"],
        "initial settled recipient branch differs",
    )
    require(
        record["detached_recipient_branch"][0] == record["recipient_block_hex"]
        and spend in detached[maturity][1],
        "original recipient spend or bytes missing",
    )
    require(
        record["alternate_hashes"]
        == [active[h][0] for h in range(maturity, maturity + 4)],
        "reorg branch differs",
    )
    require(
        active[maturity + 4][0]
        == record["remined_spend_hash"]
        == record["scary_active_tip"]
        and [h for h, (_, txs, _) in active.items() if spend in txs] == [maturity + 4],
        "recipient spend missing from replacement chain",
    )
    require(
        all(s["chain_state"] == "settled" for s in record["settlement_before_reorg"])
        and record["reorg_solutions"][-1]["chain_state"] == "stale_at_depth",
        "solution settlement differs",
    )
    require(
        spends_of(active, (source, vout)) == [(start, recovered_id)],
        "frozen output did not have exactly its authorized spend",
    )
    require(
        spends_of(active, (recovered_id, 1)) == [(maturity + 4, spend_id)]
        and spends_of(detached, (recovered_id, 1)) == [(maturity, spend_id)],
        "recovered output has an unexpected spender",
    )
    require(
        {(item["height"], item["hash"]) for item in record["settlement_before_reorg"]}
        == {
            (start, record["confiscation_block"]),
            (maturity, record["confirmed_hash"]),
        },
        "Stratum solution record differs",
    )
    ordinary = record["ordinary_freeze_unfreeze"]
    final_hash, txs, _ = active[len(active)]
    require(
        final_hash == ordinary["inclusion"]
        and ordinary["released_payment"] in [txid(tx) for tx in txs],
        "ordinary payment is missing from final chain",
    )
    return {
        "active_blocks": len(active),
        "detached_blocks": len(detached),
        "signed_directives": len(actions),
        "funding_height": found[0][0],
        "authorization_and_inclusion_height": start,
        "maturity_minus_one": maturity - 1,
        "mature_recipient_height": maturity,
        "recipient_reinclusion_height": maturity + 4,
        "sv_final_active_tip": final_hash,
    }


def replay_stock(record):
    from sv_reference import Reference, RpcError
    from run_cohort import session_lock

    actions = verify_signed_actions(record)
    with session_lock(), Reference().running(peer=True, fast_rpc=True) as node:
        require(
            node.rpc("getbestblockhash") == REGTEST_GENESIS,
            "fresh stock replay requires regtest genesis",
        )

        def submit(raw):
            expected = txid(bytes.fromhex(raw)[:80])
            outcome = node.rpc("submitblock", raw)
            require(
                outcome is None and node.rpc("getbestblockhash") == expected,
                f"SV replay failed at {expected}: {outcome}",
            )

        def interval(payload):
            source = payload[:32].hex()
            vout, start, stop, expires = struct.unpack("<QQQB", payload[32:])
            result = node.rpc(
                "addToConsensusBlacklist",
                {
                    "funds": [
                        {
                            "txOut": {"txId": source, "vout": vout},
                            "enforceAtHeight": [{"start": start, "stop": stop}],
                            "policyExpiresWithConsensus": bool(expires),
                        }
                    ]
                },
            )
            require(
                not result.get("notProcessed", []),
                "SV did not apply the signed height interval",
            )

        active = record["active_history_from_height_one"]
        maturity = record["maturity_height"]
        recovery = maturity - 1000
        for height, wire in enumerate(active[: maturity - 1], 1):
            submit(wire)
            if height == recovery - 3:
                interval(actions[0])
            if height == recovery - 1:
                reply = node.rpc(
                    "addToConfiscationTxidWhitelist",
                    {
                        "confiscationTxs": [
                            {
                                "confiscationTx": {
                                    "enforceAtHeight": recovery,
                                    "hex": record["confiscation_hex"],
                                }
                            }
                        ]
                    },
                )
                require(
                    not reply.get("notProcessed", []),
                    "SV did not whitelist the signed recovery",
                )
            if height == maturity - 2:
                try:
                    node.rpc("sendrawtransaction", record["recipient_spend_hex"])
                except RpcError as error:
                    require(
                        "premature-spend-of-confiscation" in str(error),
                        "unrelated SV rejection",
                    )
                else:
                    raise ValueError("premature recipient spend admitted on fresh SV")
        require(
            node.rpc("sendrawtransaction", record["recipient_spend_hex"])
            == record["recipient_spend"],
            "SV did not admit mature recipient",
        )
        for wire in record["detached_recipient_branch"]:
            submit(wire)
        node.rpc("invalidateblock", record["confirmed_hash"])
        require(
            node.rpc("getbestblockhash") == record["recipient_parent"],
            "fresh SV failed to restore original parent",
        )
        for height, wire in enumerate(active[maturity - 1 :], maturity):
            if height == len(active):
                interval(actions[3])
                interval(actions[4])
            submit(wire)
        tip = node.rpc("getbestblockhash")
        require(
            tip == record["sv_final_active_tip"], "fresh SV final active chain differs"
        )
        return {
            "fresh_sv_active_tip": tip,
            "confirmed_detached_blocks": len(record["detached_recipient_branch"]),
            "confirmed_active_blocks": len(active),
        }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("receipt", type=Path)
    p.add_argument(
        "--replay-sv",
        action="store_true",
        help="Also submit both exact histories and signed recovery rules to a fresh pinned regtest SV Node.",
    )
    args = p.parse_args()
    raw = args.receipt.read_bytes()
    record = json.loads(gzip.decompress(raw) if args.receipt.suffix == ".gz" else raw)
    summary = verify(record)
    if args.replay_sv:
        summary["independent_replay"] = replay_stock(record)
    print(json.dumps(summary, indent=2))
