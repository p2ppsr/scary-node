"""Check the shipped result tables, solved bytes and independent peer outcomes."""

import gzip
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "regtest"))
from prove import decode_block, dsha, txid


def verify(directory):
    rows = json.loads((directory / "chronicle-opcodes.json").read_text())["rows"]
    with gzip.open(directory / "chronicle-signatures.json.gz", "rt") as file:
        rows += json.load(file)["rows"]
    for row in rows:
        for mode in ("policy", "consensus"):
            assert (
                len({row["observations"][name][mode] for name in ("rust", "go", "sv")})
                == 1
            )
        if "expected_consensus" in row:
            assert row["observations"]["sv"]["consensus"] == row["expected_consensus"]
    dar = json.loads((directory / "scary-dar-lifecycle.json").read_text())
    for block, raw, txn_id in [
        ("confiscation_block", "confiscation_block_hex", "confiscation_txid"),
        ("confirmed_hash", "recipient_block_hex", "recipient_spend"),
    ]:
        body = bytes.fromhex(dar[raw])
        assert dsha(body[:80])[::-1].hex() == dar[block]
        assert int.from_bytes(dsha(body[:80]), "little") <= 0x7FFFFF << 232
        _, txs, _ = decode_block(body, dar[block])
        assert len(txs) == 2 and txid(txs[1]) == dar[txn_id]
        assert dsha(dsha(txs[0]) + dsha(txs[1])) == body[36:68]
    assert dar["reorg_solutions"][-1]["chain_state"] == "stale_at_depth"
    assert all(
        item["chain_state"] == "settled" for item in dar["settlement_before_reorg"]
    )
    assert dar["scary_active_tip"] == dar["remined_spend_hash"]
    first, second = json.loads(
        (directory / "chronicle-directive-race.json").read_text()
    )
    assert (
        first["order"] == "directive-first"
        and first["reference_tip"] == first["parent"]
        and not first["solutions"]
    )
    assert (
        second["order"] == "solution-first"
        and second["reference_tip"] == second["candidate"]
    )
    for r in (first, second):
        assert (
            r["receipt"]["sequence"] == r["durable_sequence"] == 1
            and r["receipt"]["kind"] == 2
        )
    signed = json.loads(
        (directory / "chronicle-signed-body-throughput.json").read_text()
    )[1:]
    for r in signed:
        assert r["transactions"] == r["known_signatures"]
        assert (
            abs(
                r["commit_inclusive_tps"]
                - r["transactions"] * 1e6 / r["commit_inclusive_us"]
            )
            < 1
        )
    print(
        f"Verified {len(rows)} differential cases, both solved DAR bodies, reorg dispositions and both directive orderings."
    )


if __name__ == "__main__":
    verify(ROOT / "docs/evidence")
