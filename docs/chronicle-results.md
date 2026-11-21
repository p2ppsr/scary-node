# Chronicle engineering results

On November 20, 2026 Pacific, Scary Node mined an authorized confiscation and a spend by the recovered output's recipient on isolated regtest. A stock SV Node v1.2.3 selected both Stratum solutions through Bitcoin SV P2P. Scary Node retained the recovered output across restart. The recipient's spend failed at inclusion plus 999 and succeeded at inclusion plus 1,000. After two successors, SV Node switched to a longer replacement branch. Scary Node restored the recovered output, changed the original spend block's disposition from settled to stale, and followed the spend into its replacement block. The alert feed in this experiment used three distinct lab wallet signers and an authenticated delivery fixture. [Scary Node lifecycle receipt](evidence/scary-dar-lifecycle.json) · [Independent SV Node RPC lifecycle](evidence/sv-dar-lifecycle.json)

| Comparison | Cases | SV policy accepted | SV block accepted | Decisions differing across Rust BDK, GoBDK and SV Node |
| --- | ---: | ---: | ---: | ---: |
| Pinned Chronicle opcode vectors and false-result control | 11 | 10 | 10 | 0 of 22 |
| Pinned clean-stack, low-S, Chronicle sighash and push-only families | 51 | 36 | 40 | 0 of 102 |

The four extra consensus acceptances in the signature corpus show why policy and block validation need separate oracles. The funder script, input values, effective heights, extended transaction, block hash and three outcomes are in the receipts. The cases exercise one and multiple inputs, both transaction versions and acceptance and rejection paths. These are deterministic vectors and do not estimate a disagreement rate on future traffic. [Opcode rows](evidence/chronicle-opcodes.json) · [Signature rows](evidence/chronicle-signatures.json.gz) · [Native build manifest](evidence/chronicle-macos-bdk.json)

A separate fresh-volume run put two Teranode v0.16.1 peers on an isolated Docker network. Scary Node joined at height 101 with one contemporary header, mined an ordinary payment at height 104, and both reference processes served the identical body and adopted a successor. The block hash was `36ae4c568778ca3a2f0d8f71c6cd0bd850ca1a9b5d3906abc203d17a05882afc`. This test evaluates the native Teranode publication path. The same-byte DAR interoperability run requires matching directive state on the receiving Teranode. [Teranode receipt](evidence/chronicle-teranode-proof.json)

The directive race used two fresh isolated SV Node processes and actual network-target Stratum submissions. With the signed freeze durably applied first, the worker lost the job, the solution queue stayed empty and SV Node stayed on its original parent. With delivery completed first, SV Node adopted the exact candidate before the freeze arrived. These are forced orderings at the two boundaries. [Directive receipts and peer dispositions](evidence/chronicle-directive-race.json)

## Validation and durable storage throughput

The host was an Apple M3 Max with 16 CPU cores and 128 GB RAM. Docker ran the Linux ARM64 release-profile test binary with the pinned native archive. Each measured body spent 4,096 distinct witnessed P2PKH outputs. All 16,384 signatures passed and every input value was known. Times include block parsing, hashing, BDK evaluation, witnessed-state update, undo and solution serialization, file sync, rename and directory sync. Fixture signing and network transit were outside the timer.

| Signed body | Observation time (ms) | Total with checkpoint (ms) | Transactions/s with checkpoint |
| --- | ---: | ---: | ---: |
| 1 | 114.5 | 145.1 | 28,227 |
| 2 | 119.2 | 167.1 | 24,518 |
| 3 | 119.7 | 183.4 | 22,332 |
| 4 | 119.1 | 199.6 | 20,518 |

Checkpoint size grew from 3.70 MB to 10.09 MB over the signed rounds. Rewriting the snapshot accounts for much of the growing end-to-end time. The measurements apply to these four in-process bodies and this filesystem. They establish neither sustained live network throughput nor a supported hardware envelope. [Per-body measurements](evidence/chronicle-signed-body-throughput.json) · [Linux build identity](evidence/chronicle-linux-bdk.json)

A separate test used the macOS ARM64 release executable and a pinned Linux AMD64 SV Node v1.2.3 running under Docker Desktop on the same workstation. SV Node received each prepared body through RPC, validated it and announced it over native TCP. The timer ran through Scary Node's observed durable tip. The signed data and headers were constructed before timing. All ordinary inputs were witnessed P2PK outputs. The per-body rows include RPC-return and durable-observation latency with an 80 ms polling interval. The 128-block run checked every one of its 524,288 signatures and reached the full node's exact final tip.

| Native workload | Signed transactions | Wire MB | Time (seconds) | Transactions/s |
| --- | ---: | ---: | ---: | ---: |
| 8 consecutive bodies | 32,768 | 6.54 | 4.687 | 6,992 |
| 128 consecutive bodies | 524,288 | 104.61 | 110.651 | 4,738 |

The 128-block median durable-observation latency was 794 ms. These runs establish capacity for 4,096 signed spends per approximately 817 kB body on this host and reference path. They leave sustained multi-gigabyte bodies, broader scripts, production mempool intake, simultaneous ASIC clients and WAN propagation unmeasured. The short and long runs identify their respective release executable digests. [Eight-block measurements](evidence/chronicle-native-workload.json) · [128-block measurements](evidence/chronicle-native-sustained.json)

On a separate fresh mixed network, two Teranodes selected the solution through their gossip and Asset network. An independent SV Node received the same solution directly from the entrant over Bitcoin SV P2P. All three returned body SHA-256 `941107d0b0b4b3c61a735a8335b641aa33906e3e855addfdc0d0508b03c708df` at height 104. Teranode generated the two successors and the lab supplied those successor bytes to the SV reference. All three full nodes selected the original solution in their depth-two ancestry. [Mixed native receipt](evidence/chronicle-mixed-peers.json)

A resource-fault run exhausted a one-output witness budget on a valid incoming block. Scary Node withheld work at height 106, preserved the one known output and restored that exact checkpoint on restart. With capacity increased, the next child announcement triggered retrieval of the missing parent over SV P2P. Scary Node resumed at the reference tip, height 108, with three witnessed outputs. [Capacity and recovery receipt](evidence/chronicle-capacity-recovery.json)

A stock SV Node selected a 12,583,267-byte block carrying a large data output and a fee-paying signed spend. Scary Node checked the witnessed input, retained the complete wire body in a read-only mapping on its data volume, restored the same byte length and tip after restart, and followed the next reference block. A previous alpha.4 executable then restored the newer checkpoint and followed another live successor. Both executable digests are in the receipt. Streamed bytes remain provisional until full-body validation and checkpoint commit. [Large-body receipt](evidence/chronicle-large-body.json)

## Mainnet observation and production status

A passive observer started with an empty mainnet data directory. It verified a contemporary body at height 976422, hash `000000000000000013f094c21f4ca9132162425fa4e3e905f88166a564f88528`. Its BDK build was identified and Stratum was disabled. At that observation it had one body in its difficulty window, zero pending transactions and no synchronized Alert System peer. It issued no candidate. Startup of the upstream receiver and its DHT connections alone did not establish a verified signed tip. The 1,008-block operating qualification remains open. [First shadow observation](evidence/chronicle-shadow-first.json)

Production qualification is open. Outstanding evidence includes large-body streaming under sustained mixed-node load, same-byte Teranode DAR telemetry, complete signed delivery across an upstream alert service, full-state controls with independent receiving paths and unavailable opportunities, operational recovery under storage and clock faults, and the completed mainnet shadow cohort. SV Node remains the DAR reference. The earlier 1,024 ready-opportunity common-relay result has its original scope. [Reproduce the measurements](chronicle-qualification.md) · [Prospective cohort design](production-cohort.md)
