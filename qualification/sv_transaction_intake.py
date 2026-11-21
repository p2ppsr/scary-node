"""Mine a signed wallet payment delivered only through the independent SV peer's mempool relay."""

import argparse
import contextlib
import hashlib
import json
import tempfile
from pathlib import Path

from native_cohort import dsha, funding_transactions, stratum_job
from peer_loss import nonce
from run_cohort import session_lock
from scary_dar import ROOT, LabFeed, entrant, get_port, response, until
from sv_reference import IMAGE, Reference
from test_framework.mininode import CBlock, CTransaction, FromHex


def run(binary, directory):
    with (
        Reference().running(peer=True, fast_rpc=True) as full,
        contextlib.ExitStack() as life,
    ):
        full.rpc("generate", 105)
        feed = LabFeed(full, directory)
        life.callback(feed.server.shutdown)
        port, worker = get_port(), get_port()
        api = life.enter_context(entrant(binary, full, feed, directory, port, worker))

        def mining():
            return response(api + "/status")["mining"]

        binary_sha256 = hashlib.sha256(binary.read_bytes()).hexdigest()
        assert (
            response(api + "/status")["release"]["executable_sha256"] == binary_sha256
        )
        funding, (payment,) = funding_transactions(full, 1)
        funding_id = full.rpc("sendrawtransaction", funding)
        first = full.rpc("generate", 1)[0]
        until(lambda: mining()["tip_hash"] == first, "funding block witnessed")
        parent = full.rpc("generate", 1)[0]
        until(
            lambda: (
                mining()["tip_hash"] == parent and mining()["observation_window_ready"]
            ),
            "ready parent",
        )
        start = mining()
        assert start["pending_transactions"] == 0
        before = start["peer_transaction_intake"]

        payment_id = full.rpc("sendrawtransaction", payment.hex())
        assert payment_id == dsha(payment)[::-1].hex() and payment_id in full.rpc(
            "getrawmempool"
        )
        until(
            lambda: mining()["pending_transactions"] == 1,
            "ordinary payment relayed by full SV peer",
        )
        after = mining()["peer_transaction_intake"]
        assert after["sv"]["admitted"] == before["sv"]["admitted"] + 1
        assert after["teranode_subtree"] == before["teranode_subtree"]
        with stratum_job(parent, payment, worker, port, "sv-relayed-payment") as job:
            solution = nonce(job)
            header = job.header(solution)
            solved_hash = dsha(header)[::-1].hex()
            assert job.submit_native(solution)
        until(
            lambda: full.rpc("getbestblockhash") == solved_hash,
            "SV selected Stratum block",
        )
        result = full.rpc("getblock", solved_hash)
        assert result["previousblockhash"] == parent and result["tx"][1:] == [
            payment_id
        ]
        wire = full.rpc("getblock", solved_hash, 0)
        block = FromHex(CBlock(), wire)
        assert block.vtx[1].serialize() == payment
        source = FromHex(CTransaction(), funding)
        spent = block.vtx[1].vin[0].prevout
        assert spent.hash == int(funding_id, 16)
        fee = source.vout[spent.n].nValue - sum(out.nValue for out in block.vtx[1].vout)
        subsidy = (50 * 100_000_000) >> (result["height"] // 150)
        assert fee > 0 and len(block.vtx[0].vout) == 1
        payout = block.vtx[0].vout[0]
        assert bytes(payout.scriptPubKey) == b"\x51" and payout.nValue == subsidy + fee
        successor = full.rpc("generate", 1)[0]
        until(
            lambda: mining()["tip_hash"] == successor,
            "entrant observed independent successor",
        )
        assert full.rpc("getblock", successor)["previousblockhash"] == solved_hash
        return {
            "network": "regtest",
            "reference": IMAGE,
            "binary_sha256": binary_sha256,
            "source": "Stock SV Node received sendrawtransaction and delivered the payment over the entrant’s only SV connection.",
            "peer_intake_before": before,
            "peer_intake_after": after,
            "funding_txid": funding_id,
            "funding_hex": funding,
            "witnessed_funding_block": first,
            "creating_block_wire": full.rpc("getblock", first, 0),
            "parent_header": full.rpc("getblockheader", parent, False),
            "parent": parent,
            "payment_hex": payment.hex(),
            "payment_txid": payment_id,
            "fee_satoshis": fee,
            "subsidy_satoshis": subsidy,
            "payout_satoshis": payout.nValue,
            "block_hash": solved_hash,
            "solved_block": wire,
            "header_hex": header.hex(),
            "block_transaction_ids": result["tx"],
            "sv_successor": successor,
            "sv_successor_header": full.rpc("getblockheader", successor, False),
        }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("receipt", type=Path)
    p.add_argument("--binary", type=Path, default=ROOT / "target/release/scary-node")
    args = p.parse_args()
    with session_lock(), tempfile.TemporaryDirectory() as directory:
        receipt = run(args.binary, Path(directory))
    args.receipt.write_text(json.dumps(receipt, indent=2) + "\n")
