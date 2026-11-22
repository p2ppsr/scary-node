import hashlib
import tempfile
import unittest
from pathlib import Path

from recover_sustained_chain import read
from workload_solved_ledger import audit, extract, public_ledger


class WorkloadSolvedLedgerTests(unittest.TestCase):
    def test_selected_timeout_solutions_and_paid_origin(self):
        from workload_block_fees import economics

        data = Path(__file__).resolve().parents[1] / "docs/evidence"
        receipt = read(data / "chronicle-sustained-reordered.json.gz")
        ledger = read(data / "chronicle-sustained-reordered-ledger.json")
        proof = audit(receipt, ledger)
        self.assertEqual(proof["checkpoint_solutions"], 56)
        self.assertEqual(proof["scary_primary"], {"blocks": 29, "payments": 235809})
        delayed = [
            (row["height"], row["worker_index"])
            for row in proof["selected"]
            if not row["worker_returned_hash"]
        ]
        self.assertEqual(delayed, [(524, 124), (550, 204), (568, 269), (577, 300)])
        fees = read(data / "chronicle-sustained-reordered-fees.json")
        paid = economics(receipt, fees, ledger)
        self.assertEqual(paid["distinct_selected_scary_extranonces"], 30)
        self.assertEqual(
            paid["selected_by_owner"]["scary"]["claimed_above_subsidy_satoshis"],
            47_187_400,
        )
        damaged = dict(ledger, mining_identity="01" * 32)
        with self.assertRaisesRegex(ValueError, "mining identity|miner domain"):
            economics(receipt, fees, damaged)

    def test_read_separate_mining_domain_and_reject_damaged_checkpoint(self):
        import msgpack

        receipt = (
            Path(__file__).resolve().parents[1]
            / "docs/evidence/chronicle-sustained-inventory.json.gz"
        )
        tip = read(receipt)["blocks"][-1]
        payload = msgpack.packb(
            {
                "height": tip["height"],
                "solved_blocks": 0,
                "lifecycle": {"records": []},
                "headers": [bytes.fromhex(tip["header_hex"])],
                "solution_body": None,
                "pending": {},
                "spent": [],
            },
            use_bin_type=True,
        )
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            file = folder / "witness.state"
            identity = "01" * 32
            anchor = folder / ("mining-domain-" + identity)
            anchor.write_bytes(bytes(range(32)))
            file.write_bytes(b"SCARYWS1" + payload + hashlib.sha256(payload).digest())
            proof = public_ledger(extract(file))
            self.assertEqual(proof["mining_identity"], identity)
            self.assertEqual(
                proof["binding_file_sha256"],
                hashlib.sha256(anchor.read_bytes()).hexdigest(),
            )
            self.assertEqual(proof["tip_hash"], tip["hash"])
            self.assertEqual(proof["records"], [])
            (folder / ("mining-domain-" + "02" * 32)).write_bytes(bytes(32))
            with self.assertRaisesRegex(ValueError, "corresponding mining-domain"):
                extract(file)
            self.assertEqual(public_ledger(extract(file, anchor)), proof)
            file.write_bytes(b"SCARYWS1" + payload + bytes(32))
            with self.assertRaisesRegex(ValueError, "checksum"):
                extract(file, anchor)


if __name__ == "__main__":
    unittest.main()
