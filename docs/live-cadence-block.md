# A four-node trial at a ten-minute block cadence

## Prospective registration

This isolated regtest trial asks whether stock SV Node, two independent stock Teranodes and the witnessed entrant can select one fully signed body representing 600 seconds of arrivals at 3,000 payments per second. The separately registered wallet trials measure continuous ingress over 1,800 seconds and use six-second work opportunities. A network can perform differently on those two workloads. [Sustained wallet design](sustained-native-trial.md) · [Production-settings replication](sustained-native-replication.md)

Use the first **1,800,000** ordinary transactions, in their published order, from the 5,529,600-payment signed source. Their raw transaction bodies occupy **359,090,161 bytes**. The first 65,536 spend witnessed funding outputs in height 484. The other 1,734,464 spend earlier outputs in the same block. The lab advances the header timestamp by 600 seconds. The new coinbase claims the height-485 subsidy and the exact fees. Before network delivery the offline verifier checks the header, Merkle root, distinct transaction IDs, funding, transfer values, fees and ordered source commitment. The full signed population has a separate signature verification. [Fixture builder and verifier](../qualification/live_cadence_body.py) · [Signed source](https://github.com/p2ppsr/scary-node/releases/tag/native-workload-regtest-v1) · [Stock-format signature check](evidence/chronicle-sustained-source-signatures.json)

Use the Linux ARM64 recovery release at source `8c41f89850ea906fb4ae43bd39a17a884bd0eb62`, ELF SHA-256 `063f28035d8a6e3f7f69d858aafdd448b6c4bba9ebee2f350423bdf53bb4bafe`, alongside stock SV Node 1.2.3 and Teranode 0.16.1. A fresh named Docker project gets two independent disk-backed Aerospike namespaces, each with a 5 GiB UTXO data file. SV uses mainnet diagnostic settings on regtest and advertises a four-billion-byte block allowance. Configure Scary Node for a 512 MiB block, a 1 GiB recent-body journal, and eight million undo items. This profile tests full native receipt and validation of a large block. Stratum origination is measured separately. [Build identity](evidence/chronicle-recovery-linux-build.json) · [Isolated Compose settings](../regtest/live-cadence.compose.yaml)

The three stock nodes replay their common history. Scary joins empty at height 449 and witnesses the two funding stages before the block under test. A loopback Bitcoin P2P publisher announces the block hash to stock SV Node, and streams the body when SV requests it. The two Teranodes and the entrant receive the block through their ordinary native connections. The workstation measures each selected tip independently, using the same monotonic clock. Stock SV Node must return the full body on a separate P2P connection. Both Teranodes must return their own complete legacy wire bodies through their Asset services. All three lengths and SHA-256 digests must agree with the original. The entrant must record every expected script and value check, the exact same-block dependency count and a determinate fee-based coinbase check. Finally all four nodes must select two actual stock-mined successors.

The rate denominator starts immediately before stock inventory announcement and ends when the last of the four nodes is observed on the fixture. Report the individual observations, readback times and settlement separately. The cadence target requires at least 3,000 signed payments per second over that denominator, all three independent reads, complete entrant checks and two successors. An absent read, wrong digest, unobserved tip, resource stop or lower rate remains in the outcome. Retain every measured attempt, including an interruption after announcement. Resource sampling includes per-service memory and CPU, and free bytes on the workstation and Docker backing filesystem. The reserve is 20 GiB on the host and 12 GiB on the backing filesystem. Preflight also budgets 14 GiB for the two new data files and setup. When the verified fixture already exists on the same filesystem, its allocated bytes count toward that staging budget. A smaller positive stock P2P transfer exercises the transport before the measured opportunity. [Native trial](../qualification/live_cadence_run.py) · [Stock transport](../qualification/cadence_transport.py)

## Reproduction

Unpack the two public source archives into `workload/` using the [dataset instructions](sustained-native-trial.md). Use the lab Python environment, Docker Compose and the exact release binary specified above. The local Docker image name can vary. The driver checks the running executable against the release digest. Run from the repository root with space for the file-backed fixture, witness journal and independent reference stores. The commands below leave the run directory and named stores available for inspection.

```sh
python3 qualification/live_cadence_body.py build workload/sustained-signed cadence.block
python3 qualification/live_cadence_body.py verify workload/sustained-signed cadence.block --manifest cadence.json
.native/py/bin/python qualification/cadence_peer_smoke.py cadence-small.json
.native/py/bin/python qualification/live_cadence_run.py prepare workload/paired-corpus workload/sustained-signed cadence-run --prepared-body cadence.block --image "$CADENCE_IMAGE"
.native/py/bin/python qualification/live_cadence_run.py run cadence.block cadence.json cadence-run --image "$CADENCE_IMAGE"
```

The raw body is deterministic. The driver stores its digest, the chosen parent, per-peer observations and the resource trace. The local study files and valid block can be reconstructed from the signed release.

The verified fixture occupies 359,090,376 bytes. Independent replay checked all 1,800,001 distinct transaction IDs, 360,000,000 satoshis in fees, and the final Merkle root. The 130-byte coinbase pays a 67-byte locking script. [Verified body manifest](evidence/chronicle-live-cadence-body.json)

## Recorded four-node result

On November 21, 2026, the four-node regtest selected the 359,090,376-byte block and both direct successors. The final independent tip observation for the payment block took **86.9335 seconds** from native inventory announcement, giving **20,705.48 payments/s** for single-block adoption. Each full node returned the same 359,090,376 bytes with SHA-256 `0862f9980261b5f5c55020c31a0fd620d28acd186c90855cd9b98fc7cfc6c737`. Scary reported all 1,800,000 ordinary transactions determinate, zero deferred script or amount checks, and the expected subsidy and fees. Its accounting found exactly 1,734,464 inputs whose creating transaction was earlier in this block. [Native observation and resource receipt](evidence/chronicle-live-cadence-result.json.gz)

| Node | Selected payment block | Finished independent body read | Selected second successor |
|---|---:|---:|---:|
| SV Node | 25.72 s | 92.40 s | 93.78 s |
| Teranode 1 | 86.93 s | 92.40 s | 93.65 s |
| Teranode 2 | 82.79 s | 92.41 s | 93.65 s |
| Scary Node | 86.10 s | 1,800,000 witnessed checks | 107.61 s |

All times use the same origin at inventory announcement. SV requested the block within 2 ms, the publisher wrote its last bytes after 2.43 seconds, and SV returned the matching pong after 9.06 seconds. All four selected the payment block at height 485 and finished on the height-487 hash `21cf7e3c2d09d22ef983d1b3ecb484a76a804a08f4f2400fb93168e2876d841e`. The external regtest publisher assembled the block from the presigned corpus. This measures native receipt, verification, accounting, storage and propagation over a single block on one machine. The separate wallet trials measure continuous ingress and work availability. [Sustained wallet evidence](sustained-native-replication.md)

The 108 resource samples recorded peak container memory of 3.00 GiB for Scary, 2.89 GiB for SV, 5.57 and 5.95 GiB for Teranode, and 4.07 and 4.26 GiB for their independent Aerospike processes. The workstation reached **6.39 MiB above its 20 GiB free-space floor**. The Docker backing filesystem retained at least 31.81 GiB. The sampler recorded no resource stop. The generic setup recorder inherited a 9 GiB label from the previous wallet trial. A direct query to both running namespaces and an independent file-size check on each dedicated volume found exactly 5,368,709,120 bytes, matching the registered configuration. The receipt retains the original setup label and the independent readbacks. The current setup driver reads the effective configuration and file size before enrollment.

Two setup checks preceded the measured handoff. The first space estimate charged the already built fixture against free space a second time. Crediting only the verified bytes already allocated to that file kept both reserve floors intact. The normal stock startup also briefly lacked its RPC cookie. The setup waited for the stock service and its native peer, then replayed the common history. No payment block had been announced during those checks.

Verify the shipped receipt offline, or export the evidence for a new completed run with the following commands.

```sh
.native/py/bin/python qualification/verify_live_cadence.py docs/evidence/chronicle-live-cadence-result.json.gz
.native/py/bin/python qualification/verify_live_cadence.py cadence-run --export my-cadence-result.json.gz
```

## Restart and independent chain replay

After settlement, the entrant restarted on its original volume. It restored **69,671 witnessed outputs** and the same 38 consecutive validation records from height 450 through 487. The restored record for height 485 includes all 1,800,000 signature and value checks. The signed directive sequence and cumulative work over the window also matched the pre-stop state. All four nodes reported the same final hash, and the stock SV mempool was empty. A supplemental setup check initially read the default lab ports. It stopped before restarting any service. The recovery driver now verifies both cadence port pairs before opening the lab. [Checkpoint and independent replay](evidence/chronicle-live-cadence-restore.json.gz)

A second, empty SV Node v1.2.3 replayed heights 1 through 484 from the public dataset. It requested the 359 MB block over native P2P, validated it and served the identical bytes on a fresh connection. It then accepted both archived successor bodies and reached the exact height-487 hash recorded by the restored entrant. This replay used a temporary 4 GiB RAM filesystem. The four-node performance result above used the original disk-backed stores.

For the process restart, use the eight paused services of the completed cadence run. The capture command temporarily resumes them, restarts the entrant, saves both exact successor bodies and returns the services to their paused state. For the independent replay, use an empty reference container and the public history, signed corpus and reconstructed fixture from the reproduction steps above. Choose fresh paths for new receipts.

```sh
.native/py/bin/python qualification/cadence_recovery.py capture cadence-run restored.json
.native/py/bin/python qualification/cadence_recovery.py replay workload/paired-corpus workload/sustained-signed cadence.block restored.json fresh-stock.json
.native/py/bin/python qualification/cadence_recovery.py verify restored.json fresh-stock.json --measured my-cadence-result.json.gz
.native/py/bin/python qualification/cadence_recovery.py verify docs/evidence/chronicle-live-cadence-restore.json.gz
```
