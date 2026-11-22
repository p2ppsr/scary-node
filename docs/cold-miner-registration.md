# Cold native funding after successor-triggered admission

Arm C uses source `c2a36d5c6a83f2262dffd596f5eecb421db6b132`, Linux executable SHA-256 `4c9ef032f2ebf4c806331fbbbc53464c517da04cc497209bb9cdb5e4cc572faf`. The measured local image must be `sha256:12e959a8368b5f4cc561eb754d1588e62f6a5f68bbc38d238174eaa40b8b827a`; a reproducer can use its independently built image after verifying the same executable at startup. This version rechecks retained native payments when a selected successor endorses their witnessed funding. The original evenly spaced [native inventory trial](sustained-native-replication.md) provides the historical comparison. The [endorsed funding and reordered timing registrations](witnessed-miner-registration.md) test different inputs to the frozen older executable.

Use the original 5,529,600 signed payments and the exact original order. The stock nodes independently replay h1–483 and select h484 funding `3fb3a456448fd6d91801a35c04112ffab3f30dc0a6a14e426effff4f91660ba9`. The entrant starts empty at h449, witnesses h451 and h484, and starts the first native inventory announcement with zero successors of h484. The SHA-256 of concatenated internal IDs in arrival order is `243170edb3df9bea289c32207732d988f6e4b92f67b17634a72e709ae8b735a2`. Use fresh independent stores in Compose project `scary-node-sustained-cold`, port shift 7000. Freeze the same four native wallet connections, stock reference options, two independent disk-backed 9 GiB Aerospike stores, route, resource floor, readbacks and stopping criteria as the two earlier registrations.

The primary period is 1,800 seconds at six 512-payment advertisements per second, with 225 entrant and 75 full-state work opportunities in the same six-second schedule. Keep all offers, losses, misses, reannouncements, primary and drain slots, same exact body readbacks, source membership, full-time denominator and two-successor termination. Do not resend before the registered incomplete-payment drain at 1,818 seconds. The stop deadline is 2,100 seconds. Enroll this single C arm after E and R individually or after recording their independent provisioning blockers. Preserve all completed outcomes. The regular-origin criterion requires a nonempty entrant block on the independently audited chain at least once in each of the six consecutive sets of 50 primary opportunities. Report the number of included payments the entrant originated and the number originated by stock SV with all system-wide throughput figures. The joint target additionally requires complete verified source membership, full entrant checks, no duplicate or foreign payment, two selected successors and at least 3,000 confirmed payments per complete observation second. Include retained, promoted, rejected or expired native admissions when this release exposes them, along with measured work readiness and source-to-settlement time. A qualifying result establishes a single observed operating point on isolated regtest.

The original C run closed with 5,529,600 payments settled through two successors. All 225 entrant slots recorded no eligible transaction. [Measured outcomes and reproduction](cold-workload-results.md)

## Reproduction

Use the same restored history and signed payments as the [native inventory control](sustained-native-replication.md). Supply the registered image by local ID, or specify both `--image` and `--image-id` for a build that produces the registered ELF.

```sh
SCARY_SUSTAINED_PROFILE=cold .native/py/bin/python qualification/sustained_replication.py \
  .native/paired-corpus .native/sustained-signed .native/cold-changed-run
.native/py/bin/python qualification/verify_sustained_native.py .native/cold-changed-run/receipt.json.gz \
  --corpus .native/sustained-signed --blocks .native/cold-changed-run/blocks --parent-ages
```

### Measuring admission after a successor

The entrant reports cumulative counters for transactions received over SV P2P and from Teranode subtrees. The command below subtracts successive sampled counter values across the six five-minute offer intervals and the drain, and checks their sum against the full sampled change. Each row includes the actual entrant read times and selected heights. An interval with no new observations has no measured change. A counter that falls during an interval has an unknown change. Counts describe intake events on each route. The same signed payment can arrive more than once. `discarded_after_recheck` combines reasons and cannot establish whether a given input was spent. The independently checked source membership and parent ages supply the chain-side outcomes.

```sh
.native/py/bin/python qualification/workload_waiting_offers.py .native/cold-changed-run/receipt.json.gz
```

The fee verifier also follows each selected payment's same-block ancestors back to the first input created outside that block. It records the number of blocks between that input's creation and the payment's inclusion. A distance of one means the input was created by the candidate's parent and had no successor there. Every payment descending from that input in the same candidate inherits that limit. Distances of two or greater satisfy this particular age requirement if the entrant witnessed the output and received all of the required payments in time. The calculation describes the final selected chain. It does not reconstruct transaction arrival times, previous competing templates, or the cause of individual admission decisions.

```sh
.native/py/bin/python qualification/workload_block_fees.py wires .native/cold-changed-run/receipt.json.gz \
  .native/sustained-signed .native/cold-changed-run/blocks > cold-block-fees.json
```

## Attributing an accepted block when a worker misses the reply

For this arm the worker records the job notification, both extranonces and the exact proof-of-work header it prepared for each submission. A worker timeout or rejection stays in the opportunity record. The offline verifier reconstructs the coinbase commitment and header, checks proof of work and matches the complete 80 bytes against the selected headers returned by the three reference nodes. It reports acknowledged solutions and chain-side selections separately. A repeated header counts as one block. If repeated attempts span intervals, the verifier leaves that block out of the per-interval result. This recording change preserves the enrolled binary, slot schedule, time denominator and thresholds.

```sh
python3 qualification/workload_worker_headers.py .native/cold-changed-run/receipt.json.gz
```

The host monotonic clock also records the interval from a worker’s connection attempt through completion of Stratum setup, the interval from its submit call through a response or error, and the start and finish of each stock node’s complete body read. Each stock read includes any retries within that request. The driver retains the group audit completion after decoding, membership accounting and writes. These fields distinguish worker response time, sampled tip selection and body retrieval on a common clock. Earlier receipts preserve their original timestamp resolution.

### A selected block after the worker loses its connection

In a separate live regtest on release `5148c31`, stock SV Node v1.2.3 relayed one signed payment. A loopback relay forwarded the worker’s real `mining.submit` to Scary Node and closed the response path to the worker. The worker reported `Stratum session closed`. The node returned a positive response to the relay. Stock SV Node selected the resulting 399-byte block, served its two transactions, and mined a successor on that exact hash. The coinbase included the payment’s 1,100-satoshi fee. Both the worker error and the active-chain inclusion belong in the result. This test exercises one response-path failure on regtest. [Worker job, forwarded submission, stock block and successor](evidence/chronicle-stratum-lost-reply.json)

Replay the public receipt with the qualification Python environment. The verifier rebuilds the header from the recorded job, matches the forwarded nonce, checks the payment signature and fee, and verifies the selected block and successor. The second command recreates the live test with Docker and the archive-built macOS or Linux executable.

```sh
.native/py/bin/python qualification/verify_peer_intake.py docs/evidence/chronicle-stratum-lost-reply.json
.native/py/bin/python qualification/sv_transaction_intake.py .native/stratum-lost-reply.json \
  --binary target/release/scary-node --lose-worker-reply
```
