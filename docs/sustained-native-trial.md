# Prospective 30-minute native transaction and Stratum workload

## Enrollment

One scheduled run uses the same independent disk-backed four-node regtest project, Linux release, isolated native routes and workstation as the [32-block paired burst](mixed-storage-trial.md). The measured workload begins after 65,536 funded ordinary P2PK outputs are witnessed. Each output supplies a chain of transfers among eight disposable transaction signers. Parent outputs recur about every 21 seconds at the registered rate. The existing shared cohort lock covers measurement. The source uses only the isolated stock SV Bitcoin P2P listener. Scary Node receives ordinary transactions from its real native peers. The node's operator submission endpoints receive zero transactions.

The source schedules **5,529,600 distinct signed payments over 1,800 seconds** in 10,800 batches of 512, evenly spaced at six per second. Four normal wallet peer connections share batches. Preparation and signing take place outside the interval. Elapsed measurement starts at the first scheduled offer, even if delivery is delayed. A separate fixed schedule provides 300 proof-of-work opportunities at six-second intervals. In each group of four, three opportunities use a real Scary Node Stratum V1 session and the fourth uses the full SV miner. The 225 entrant and 75 reference opportunities are reported separately with readiness, rejected work, missed slots, accepted hashes, settlement and transaction counts. Neither scheduling loop waits for the other. Miner cadence is accelerated regtest time.

The primary targets are all 5,529,600 distinct payments included and **at least 3,000 signed spends/s from the first scheduled offer through the last required independent body readback and two successor confirmations**. Each full node must return the identical wire digest for every selected workload block. The entrant must have continuous body coverage and report its own witnessed checks; sampled mining readiness and receiver availability are separate observations. Report active adoption times, scheduled versus actual sends, source-side reject messages, outstanding payments, five-minute cohorts, each miner’s share, and any loss of readiness. A blocked work slot counts. There is one primary attempt; preserve partial measurements and report any lower rate.

After the 1,800-second offer window, continue the same six-second miner rotation if payments remain. Beginning 18 seconds into drain, the wallet may rebroadcast still-unconfirmed original bytes through the same native SV ingress, at or below the registered offer rate. Report retransmissions separately and count every payment at most once. After all payments appear, two reference successors settle the final payment block. The drain limit is 300 seconds. Report incomplete inclusion at that limit.

A one-second sampler records independent SV/Teranode heights and hashes, the entrant’s selected parent, pending count, readiness and peer transaction acceptance counters. Container memory/CPU and free disk samples run alongside; Aerospike namespace and disk usage are recorded periodically. The receipt identifies the exact binaries, source corpus commitments, each block header and body digest, script checks, and distinct signed payment count. Full wires remain available in the local run directory. The prior SQLite and Aerospike observations used a 34.6-second or 501.9-second block-input burst and remain separate.

Reserve at least 20 GiB on the workstation data volume and at least 12 GiB on the Docker backing filesystem throughout. A terminal resource stop is recorded as an incomplete attempt. At registration the two 9 GiB databases each reported 861,345 records and 631,733,024 used bytes after the paired trial.

## Reproduction

Keep the isolated Aerospike project at its paired workload's final active tip and retain that run's input corpus. Build the pinned Python test framework and Linux ARM64 node image from the [qualification runbook](chronicle-qualification.md). The wallet gateway publishes only a loopback port to the stock SV peer. The funding setup splits the 16,384 final outputs into 65,536 evidenced outputs. The same Scary Node participates through its existing native links and Stratum listener.

```sh
docker compose -p scary-node-throughput -f regtest/compose.yaml -f regtest/mixed.compose.yaml \
  -f regtest/mixed-native.compose.yaml -f regtest/mixed-aerospike.compose.yaml \
  -f regtest/sustained-wallet.compose.yaml up -d --no-build --pull never wallet-ingress
.native/py/bin/python qualification/sustained_corpus.py .native/paired-corpus .native/sustained-signed
.native/py/bin/python qualification/sustained_native.py .native/sustained-signed .native/sustained-run
```

Run the driver with a fresh output directory. It preserves the append-only source, opportunity, block, receiver and resource logs as well as the compressed summary. `frames.bin` contains complete regtest transaction messages in scheduled order, and `ids.bin` holds their 32-byte internal transaction IDs. Each active body is retained in `blocks/<hash>.block`. Counts of locally emitted wallet messages, native node intake and independently confirmed payments have distinct denominators.


Replay the compact receipt to check the fixed offer and miner schedules, active header chain and work, branch payment accounting, readback digests, timestamps and observed resource floors. The second command also reads all 5,529,600 native envelopes and independently recomputes each archived block’s Merkle root, source membership and five-minute cohort. A completed audit of a partial run returns an explicit failed adoption target. A corrupt or inconsistent receipt terminates verification with an error.

```sh
python3 qualification/verify_sustained_native.py .native/sustained-run/receipt.json.gz
python3 qualification/verify_sustained_native.py .native/sustained-run/receipt.json.gz \
  --corpus .native/sustained-signed --blocks .native/sustained-run/blocks
```

The replay checks transaction identities and commitments. Signature evidence comes from the live nodes’ validation observations. CPU and memory figures use cgroup counters from the seven services. The host and Docker reserve checks use the filesystems on which the run and node data reside.

## Verify the entire wallet source

Funding and receipt files can be packaged independently of the live node stores. The packaging command checks 483 linked historical regtest bodies, the 65,536 outputs in the height-484 funding block, the original stock receipt, the native message checksum and the ordered ID of every payment. It writes only the named workload files and the project license into the two archives. A separate signature audit follows each funded output through its ordered transfers. It uses the pinned SV Node functional framework’s signature-hash calculation and validates the signature with the public key from the preceding output. It checks recipients, amounts and fees without assigning hypothetical block heights to unconfirmed payments.

```sh
python3 qualification/sustained_dataset.py pack .native/paired-corpus .native/sustained-signed .native/offer-assets
python3 qualification/sustained_dataset.py unpack .native/offer-assets .native/replay --receipt .native/offer-assets/source-assets.json
.native/py/bin/python qualification/verify_payment_signatures.py .native/replay/sustained-signed .native/source-signatures.json
```

A new funding run creates fresh ECDSA signatures. Reusing the exact archived funding and transfer wires makes the transaction IDs comparable across the two registered relay configurations.
