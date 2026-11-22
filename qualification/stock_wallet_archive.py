"""Compare simulated regtest wallet recovery using only counts and aggregate commitments."""

import argparse
import json
import os
import re
from pathlib import Path

from recover_sustained_chain import read
from restore_witness_archive import docker
from sv_reference import IMAGE
from verify_receipt import require


def fingerprint(cid):
    # Private keys, addresses and UTXO scripts remain inside the container. The
    # stock RPC writes its dump to the guest's memory filesystem, then the trap
    # removes all scratch files. No wallet spending operation is performed.
    script = r"""
set -eu
umask 077
export LC_ALL=C
folder=$(mktemp -d /dev/shm/scary-wallet-XXXXXXXX)
trap 'rm -rf "$folder"' EXIT
rpc() { bitcoin-cli -regtest -datadir=/data "$@"; }
rpc getblockchaininfo > "$folder/chain"
grep -Eq '"chain": *"regtest"' "$folder/chain"
rpc dumpwallet "$folder/original.dump" >/dev/null
awk 'NF && $1 !~ /^#/ { print "key " $1 }
     /^# extended private masterkey: / { print "master " $NF }' "$folder/original.dump" | sort > "$folder/keys"
awk 'NF && $1 !~ /^#/ { print $0 }' "$folder/original.dump" | sort > "$folder/keyrows"
rpc listunspent 0 9999999 > "$folder/unspent"
awk '/"txid":/ {t=$2} /"vout":/ {v=$2} /"amount":/ {a=$2} /"scriptPubKey":/ {s=$2}
     /^[[:space:]]*}/ && t!="" {if(v==""||a==""||s=="") exit 12; print t,v,a,s; t=v=a=s=""}
     END{if(t!="") exit 13}' "$folder/unspent" > "$folder/outpoints-unsorted"
sort "$folder/outpoints-unsorted" > "$folder/outpoints"
rpc getwalletinfo > "$folder/info"
expected=$(awk '/"txcount":/ { gsub(/,/, "", $2); print $2 }' "$folder/info")
rpc listsinceblock > "$folder/history"
awk '/"txid":/ { gsub(/[",]/, "", $2); print $2 }' "$folder/history" | sort -u > "$folder/ids"
test "$(wc -l < "$folder/ids")" -eq "$expected"
touch "$folder/txrecords" "$folder/txwires"
while IFS= read -r id; do
  rpc gettransaction "$id" > "$folder/record"
  digest=$(sha256sum "$folder/record")
  printf '%s %s\n' "$id" "${digest%% *}" >> "$folder/txrecords"
  awk -v id="$id" '/"hex":/ { gsub(/[",]/, "", $2); print id, $2; found++ } END{if(found!=1) exit 14}' "$folder/record" >> "$folder/txwires"
done < "$folder/ids"
for name in keys keyrows outpoints txrecords txwires; do
  count=$(wc -l < "$folder/$name"); digest=$(sha256sum "$folder/$name")
  printf '%s %s %s\n' "$name" "$count" "${digest%% *}"
done
"""
    raw = docker("exec", cid, "sh", "-ec", script)
    result = {}
    for line in raw.splitlines():
        name, count, sha = line.split()
        require(
            name in ("keys", "keyrows", "outpoints", "txrecords", "txwires")
            and count.isdecimal()
            and re.fullmatch(r"[a-f0-9]{64}", sha) is not None,
            "invalid original wallet commitment",
        )
        result[name] = {"count": int(count), "sha256": sha}
    require(
        set(result) == {"keys", "keyrows", "outpoints", "txrecords", "txwires"}
        and result["keys"]["count"] > 0
        and result["txrecords"]["count"] == result["txwires"]["count"],
        "missing original wallet commitments",
    )
    return result


def compare(archived, original):
    require(
        archived == original,
        "the recovered key, metadata, spendable output or wallet transaction set differs",
    )
    return {
        "private_keys_and_master_rows": archived["keys"]["count"],
        "key_metadata_rows": archived["keyrows"]["count"],
        "spendable_outpoints": archived["outpoints"]["count"],
        "all_three_private_commitments_equal": True,
        "wallet_transaction_records": archived["txrecords"]["count"],
        "wallet_transaction_wires": archived["txwires"]["count"],
        "complete_wallet_transaction_records_equal": True,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("measured", type=Path)
    parser.add_argument("enrolled_sv_container")
    parser.add_argument("new_private_summary", type=Path)
    args = parser.parse_args()
    if args.new_private_summary.exists():
        parser.error("choose a fresh private summary path")
    os.umask(0o077)
    receipt = read(args.measured)
    cfg = json.loads(docker("inspect", args.enrolled_sv_container))[0]
    role = cfg["Config"]["Labels"]
    require(
        cfg["Config"]["Image"] == IMAGE
        and role["com.docker.compose.service"] == "svref"
        and role["com.docker.compose.project"] == receipt["setup"]["project"]
        and cfg["Image"] == receipt["setup"]["topology"]["svref"]["image_id"],
        "different enrolled stock process",
    )
    tip = docker(
        "exec",
        cfg["Id"],
        "bitcoin-cli",
        "-regtest",
        "-datadir=/data",
        "getbestblockhash",
    )
    require(
        tip == receipt["completion"]["final_hash"],
        "stock tip differs from the measured receipt",
    )
    result = fingerprint(cfg["Id"])
    with args.new_private_summary.open("x") as file:
        json.dump({"source_tip": tip, "wallet": result}, file, indent=2)
        file.write("\n")
    print(json.dumps({name: v["count"] for name, v in result.items()}))
