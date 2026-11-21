# First alpha verification

These receipts belong to the initial `0.1.0-alpha.1` release. The second alpha adds the active-chain workload, two matched observers and independently checked runs with two workload seeds. [Second alpha results](scenario-results.md)

The two recorded Docker runs used two Teranode v0.16.1 processes, separate chain and UTXO databases, a Rust release binary and a CPU worker speaking Stratum V1 over TCP. Each process ran on the same isolated Docker host. The driver used reference RPC to mine funding and successor blocks and to read active tips. Rust built its own template and distributed its solved block over native gossip and Asset HTTP. Both reference nodes supplied the final wire bytes. [Topology and reproduction](regtest.md) · [Driver](../regtest/prove.py)

The release run used Rust 1.98.1 on Linux arm64, Docker Compose 2.30.3-desktop.1, Python 3.14.7 and OpenSSL 4.0.3. The SHA-256 of the tested Linux executable was `ccb4b41c243e4aa454c4fd023c1516d83fa2ebf12b5e5e3f76701510715dab38`. The Compose file pins the reference image by its multi-architecture manifest digest. An intermediate run hit the reference Asset service’s rate limit while rapidly minting maturity blocks. The released fixture sets an explicit lab-only request budget and waits for the second reference at each height during setup. [Container build](../Dockerfile) · [Fixture configuration](../regtest/compose.yaml)

## What happened on the release chain

| Height | Observation |
| ---: | --- |
| 101 | Rust joined an existing chain with one observed header and one output from that body. It rejected a signed spend of a pre-entry coin. |
| 102 | `tn1` mined the funding transaction. Rust recognized its output, then rejected the signed child because the creating block had no successor. |
| 103 | `tn2` extended the funding block. Rust admitted the identical signed child and gave the worker its own coinbase and Merkle branch in Stratum job `6`. |
| 104 | The worker solved the job. Rust advertised `6fd851c4e8b13bce2383d7c9b3891d9f59de1a561033949093e3d929126f216b` and served native block, subtree and subtree-data requests. Both Teranodes adopted the block and returned the same wire bytes containing the child and its 1,000-satoshi fee. |
| 105 | `tn2` mined `4b9e57e859b012883fcc8c54ed9e11344fb8548216c8f5be76b9a9ec5bccffe3` on top of the Rust block. Both reference processes reported it as their active tip. |

The released run finished at `2026-11-21T00:36:28Z`. A separate clean integration run also solved a distinct block at height 104. Each receipt includes its signed child, funding block and solved wire block. [Release receipt](evidence/regtest-release.json) · [Earlier integration receipt](evidence/regtest-integration.json)

## Check the recorded bytes

From the repository root, run the standard-library verifier. It recalculates the header hashes, proof of work, funding inclusion, child outpoint and fee, coinbase reward and Merkle commitments. It compares both recorded body digests with the bytes. The receipt also preserves the live nodes’ reported sources, the Rust Asset request events and the successor header. Rerunning the Docker procedure obtains fresh evidence from running validators.

```sh
python3 regtest/verify_receipt.py docs/evidence/regtest-release.json
python3 regtest/verify_receipt.py docs/evidence/regtest-integration.json
```

## Mainnet observations

A passive listener discovered four mainnet peers and received signed statuses and subtree announcements. Enabling body observation then retrieved real block bodies for heights 976407 and 976408. An integration build cleared its working set eight times when multiple peers announced height 976408. The release schedules one download per hash and tracks alternate sources. A regression test exercises 500 same-hash offers alongside distinct queued blocks. The release build subsequently received the next block from ten distinct signed publishers and preserved contiguous coverage across heights 976408 and 976409, with zero resets and 3,711 watched outputs at the second tip. The snapshots record each run separately. [Listening evidence](evidence/mainnet-listening.json) · [Queue regression tests](../src/peer/fetch.rs)

The runs exercised the witnessed P2PKH path, the Rust SDK Script interpreter, native block delivery and a CPU Stratum client. Physical ASIC firmware, sustained mainnet difficulty-window coverage, an independently mined mainnet block and comparative stale rates each need their own measurement. The evidence demonstrates that the witnessed-output construction can produce an accepted, fee-bearing block under the specified conditions. [Validation model](validation.md)
