"""Check the public mining identity against the two original native peer logs."""

import argparse
import gzip
import hashlib
import json
import re
from pathlib import Path

from recover_sustained_chain import read
from verify_sustained_native import dsha, require

ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
INLINE_KEY = re.compile(rb"12D3KooW[1-9A-HJ-NP-Za-km-z]{44}(?![1-9A-HJ-NP-Za-km-z])")


def protobuf(peer_id):
    text = peer_id.decode("ascii")
    integer = 0
    for char in text:
        integer = integer * 58 + ALPHABET.index(char)
    raw = bytes(len(text) - len(text.lstrip("1"))) + integer.to_bytes(
        (integer.bit_length() + 7) // 8, "big"
    )
    require(
        len(raw) == 38 and raw[:6] == bytes.fromhex("002408011220"),
        "unexpected inline Ed25519 identity",
    )
    return raw[2:]


def compare(ledger, first, second):
    expected = ledger["mining_identity"]
    reports = {}
    matches = set()
    for role, path in (("tn1", first), ("tn2", second)):
        data = (
            gzip.decompress(path.read_bytes())
            if path.suffix == ".gz"
            else path.read_bytes()
        )
        distinct = set(INLINE_KEY.findall(data))
        observed = [item for item in distinct if dsha(protobuf(item)).hex() == expected]
        require(
            len(observed) == 1, "expected one native peer matching the mining identity"
        )
        match = observed[0]
        rows = [line for line in data.splitlines() if match in line]
        reports[role] = {
            "log_sha256": hashlib.sha256(data).hexdigest(),
            "log_bytes": len(data),
            "distinct_inline_ed25519_peers": len(distinct),
            "matching_lines": len(rows),
            "first_matching_log_time": rows[0].split()[0].decode(),
            "last_matching_log_time": rows[-1].split()[0].decode(),
        }
        matches.add(match)
    require(len(matches) == 1, "two native receivers observed different peer IDs")
    peer_id = matches.pop()
    return {
        "mining_identity": expected,
        "public_peer_id": peer_id.decode(),
        "public_identity_protobuf_hex": protobuf(peer_id).hex(),
        "receivers": reports,
    }


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    for name in ("ledger", "tn1_log", "tn2_log"):
        cli.add_argument(name, type=Path)
    args = cli.parse_args()
    print(json.dumps(compare(read(args.ledger), args.tn1_log, args.tn2_log), indent=2))
