"""Identify the recovered payment membership of an archived Teranode candidate prefix."""

import argparse
import hashlib
import json
import struct
import zipfile
from pathlib import Path

from recover_sustained_chain import read
from restore_teranode_archive import candidate_commitments, payment_commitments
from restore_teranode_archive import verify as verify_restore
from sustained_block_archive import assemble, binding, checked, signed_frames
from verify_peer_intake import read_varint
from verify_sustained_native import decode_block, dsha, require


def root_outpoints(transactions, available):
    outpoints = set()
    for transaction in transactions:
        inputs, pos = read_varint(transaction, 4)
        require(inputs == 1, "source payment has unexpected input count")
        parent = bytes(transaction[pos : pos + 32])
        index = struct.unpack_from("<I", transaction, pos + 32)[0]
        require(
            parent in available and index < available[parent],
            "candidate input is absent from the witnessed terminal parent",
        )
        outpoints.add((parent, index))
    require(len(outpoints) == len(transactions), "duplicate candidate spend")
    return outpoints


def audit(receipt, restore, corpus, active, terminal):
    record = read(receipt)
    verify_restore(restore, record)
    role = restore["role"]
    tip = record["blocks"][-1]
    with (
        zipfile.ZipFile(active) as chain,
        zipfile.ZipFile(terminal) as queue,
        signed_frames(corpus, record["source"]) as (frames, positions),
    ):
        source = binding(receipt, frames)
        require(
            all(json.loads(z.read("source.json")) == source for z in (chain, queue)),
            "different signed source or measured prefix",
        )

        def wire(archive, name, size):
            ordinals = archive.read(name + ".ordinals")
            return assemble(
                archive.read(name + ".prefix"), ordinals, frames, positions, size
            ), ordinals

        raw, _ = wire(chain, tip["hash"], tip["wire_bytes"])
        checked(raw, tip)
        _, parents, outputs = decode_block(raw, tip["hash"])
        available = {
            dsha(tx): len(vout) for tx, vout in zip(parents, outputs, strict=True)
        }
        observation = json.loads(queue.read("observations.json"))[
            "teranode_candidates"
        ][role]
        raw, ordinals = wire(queue, role, observation["wire_bytes"])
        require(
            hashlib.sha256(raw).hexdigest() == observation["wire_sha256"]
            and raw[4:36][::-1].hex() == tip["hash"],
            "original candidate differs from measured complete wire",
        )
        _, txs, _ = decode_block(raw, dsha(raw[:80])[::-1].hex())
        original = candidate_commitments(raw)
        length = restore["candidate_transactions"]
        require(0 < length <= len(txs) - 1, "no nonempty recovered prefix")
        recovered = txs[1 : 1 + length]
        prefix = payment_commitments(recovered)
        samples = [restore[k] for k in ("first_pending_recovery", "pending_recovery")]
        for sample in samples:
            require(
                sample["original"] == original, "original candidate evidence differs"
            )
            cold = sample["after_isolated_restart"]
            require(
                all(
                    cold[k] == prefix[k]
                    for k in (
                        "payments",
                        "sorted_internal_txids_sha256",
                        "sorted_ordinary_wire_sha256",
                    )
                ),
                "the source prefix does not match recovered signed payment membership",
            )
        unchanged = all(
            samples[0]["after_isolated_restart"][key]
            == samples[1]["after_isolated_restart"][key]
            for key in ("ordinary_wire_sha256", "ordered_internal_txids_sha256")
        )
        members = sorted(i for (i,) in struct.iter_unpack("<I", ordinals[: 4 * length]))
        require(len(set(members)) == length, "repeated source ordinal")
        kept_inputs = root_outpoints(recovered, available)
        missing_inputs = root_outpoints(txs[1 + length :], available)
        require(
            kept_inputs.isdisjoint(missing_inputs),
            "two source partitions spend the same output",
        )
        return {
            "stock_role": role,
            "terminal_parent_height": tip["height"],
            "terminal_parent_hash": tip["hash"],
            "original_payments": original["payments"],
            "recovered_payments": length,
            "original_only_payments": original["payments"] - length,
            "cold_only_payments": 0,
            "recovered_distinct_inputs_in_terminal_parent": len(kept_inputs),
            "original_only_distinct_inputs_in_terminal_parent": len(missing_inputs),
            "recovered_sorted_source_ordinals_le32_sha256": hashlib.sha256(
                b"".join(struct.pack("<I", i) for i in members)
            ).hexdigest(),
            "recovered_source_ordinal_bounds": [members[0], members[-1]],
            "both_cold_samples_same_ordered_signed_payments": unchanged,
            "cold_order_matches_original_prefix": [
                sample["after_isolated_restart"]["ordinary_wire_sha256"]
                == prefix["ordinary_wire_sha256"]
                for sample in samples
            ],
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("receipt", "restore", "corpus", "active", "terminal"):
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            audit(
                args.receipt,
                read(args.restore),
                args.corpus,
                args.active,
                args.terminal,
            ),
            indent=2,
        )
    )
