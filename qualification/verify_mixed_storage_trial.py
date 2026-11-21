"""Compare the registered disk-backed replay against its exact SQLite stock workload."""

import argparse
import json
import math
from pathlib import Path
from verify_mixed_native_load import verify
from verify_receipt import require


def check(reference, replay, build=None):
    baseline = verify(reference)
    measured = verify(replay)
    trial = replay["storage_trial"]
    pairs = list(zip(reference["rows"], replay["rows"], strict=True))
    require(
        baseline["blocks"] == measured["blocks"] == 32
        and baseline["signed_spends"] == measured["signed_spends"] == 524288,
        "different workload counts",
    )
    require(
        reference["funding_header"] == replay["funding_header"]
        and reference["funding_block"] == replay["funding_block"],
        "funding boundary differs",
    )
    for left, right in pairs:
        require(
            all(
                left[key] == right[key]
                for key in (
                    "height",
                    "hash",
                    "header_hex",
                    "wire_sha256",
                    "bytes",
                    "independent_wire_sha256",
                    "witnessed_signature_checks",
                )
            ),
            "measured input or stock readback differs",
        )
    require(
        trial["source_revision"] == "af9b605ad8da96d041a540111d46a7c0105209f7"
        and trial["registration"] == "9963c88d116095aa25adf934addf65173bba1e15",
        "wrong trial source or registration",
    )
    require(
        trial["entry_state"]["tip_hash"] == reference["funding_block"]
        and trial["entry_state"]["height"] == reference["rows"][0]["height"] - 1
        and trial["entry_state"]["connected_data_peers"] >= 3,
        "funding coverage or initial topology differs",
    )
    stores = trial["topology"]
    require(
        set(stores) == {"svref", "tn1", "tn2", "as1", "as2", "scary-node", "ingress"},
        "missing services",
    )
    for n in (1, 2):
        store, peer = stores[f"as{n}"], stores[f"tn{n}"]
        require(
            store["registered_datafile_bytes"] == 9 * 1024**3
            and store["backing_size_bytes"] > 0
            and len(store["networks"]) == 1
            and store["networks"][0] in peer["networks"]
            and f"as{n}:3000/test" in peer["utxo_store"],
            "stock UTXO backend differs",
        )
        require(
            "sha256:83ac381fdad8b1ec73392c3811958b828ab0801fb032a9ec58e63e07e9fb1cbc"
            in store["image_ref"],
            "unregistered store image",
        )
    require(
        stores["as1"]["networks"] != stores["as2"]["networks"],
        "database networks are shared",
    )

    def data_mount(s):
        return next(
            m["volume"]
            for m in s["mounts"]
            if m["target"] == "/opt/aerospike/data" and m["read_write"]
        )

    require(
        data_mount(stores["as1"]) != data_mount(stores["as2"]),
        "UTXO volumes are shared",
    )
    if build is None:
        expected_image = (
            "sha256:5d3fe94ee11ed4170cc39a074967c220757ed6078b0df623d7054b35afc55f4a"
        )
        expected_binary = (
            "833fbeecc17a45de9edc8777123708ecd2e390d522c436b867a74a208318a528"
        )
    else:
        require(
            build["source_commit"]
            in (trial["source_revision"], "6ad4152ee517bc282ac70af4b0550ff6ba4458bc"),
            "different reference source build",
        )
        expected_image, expected_binary = (
            build["image_id"],
            build["release"]["executable_sha256"],
        )
    require(
        stores["scary-node"]["image_id"] == expected_image
        and replay["scary_binary_sha256"] == expected_binary,
        "entrant build differs",
    )
    first = replay["rows"][0]["offered_ns"]
    ingress = (
        measured["signed_spends"]
        * 1e9
        / (replay["rows"][-1]["source_accepted_ns"] - first)
    )
    adoption = measured["signed_spends"] * 1e9 / (replay["measured_wall_ns"] - first)
    require(
        math.isclose(ingress, trial["source_elapsed_spends_per_s"])
        and math.isclose(adoption, trial["first_offer_to_final_adoption_spends_per_s"])
        and trial["target_spends_per_s"] == 3000
        and trial["both_targets_met"] == (min(ingress, adoption) >= 3000),
        "registered performance outcomes differ",
    )
    return {
        "paired_blocks": len(pairs),
        "paired_signed_spends": measured["signed_spends"],
        "full_node_readbacks_per_run": measured["full_node_readbacks"],
        "sqlite_spends_per_s": baseline["transactions_per_second"],
        "aerospike_source_spends_per_s": ingress,
        "aerospike_all_stores_spends_per_s": adoption,
        "both_targets_met": trial["both_targets_met"],
        "final_native_observations_s": measured[
            "seconds_after_final_source_acceptance"
        ],
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("sqlite_receipt", type=Path)
    p.add_argument("new_receipt", type=Path)
    p.add_argument(
        "--build-receipt",
        type=Path,
        help="Check a new independent build of the same Rust source. The default checks the published Linux ARM64 build.",
    )
    a = p.parse_args()
    build = json.loads(a.build_receipt.read_text()) if a.build_receipt else None
    print(
        json.dumps(
            check(
                json.loads(a.sqlite_receipt.read_text()),
                json.loads(a.new_receipt.read_text()),
                build,
            ),
            indent=2,
        )
    )
