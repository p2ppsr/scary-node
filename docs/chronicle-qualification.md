# Chronicle implementation and acceptance evidence

Scary Node builds templates against a selected contemporary parent. It takes each ordinary input from a complete run of witnessed bodies on that branch or from an earlier validated transaction in the same template. The node supplies the exact script, amount and creation height to BDK. Continued proof of work is the premise for accepting history outside the observation boundary. Other miners can extend an invalid parent whose defect requires missing evidence. Miners can also disagree about asset recovery. Both risks remain relevant to production operation.

## Build the interpreter

Use the pinned Rust toolchain from the Dockerfile or Rust 1.98.1, CMake, a C++20 compiler, Git, curl, Perl with shasum, Make and Python 3. The build script supports Linux x86-64, Linux ARM64 and Apple Silicon. It fetches the exact BDK and SV Node revisions, verifies the upstream dependency bundle and builds the static archive and upstream Rust binding together. A small C ABI forwards single-input checks to the same BDK engine when only part of an incoming transaction is evidenced. Rust serialization is checked against the bytes offered for admission.

```sh
./scripts/build-bdk.sh
cargo build --release --locked
cargo test --locked
cat .native/provenance.json
```

The node reports native build provenance and linked runtime versions under `/status` → `mining.interpreter`. GoBDK v1.4.0 uses the pinned release archive as the independent binding oracle. Rust BDK and SV Node v1.2.3 supply the ordinary-script and DAR references respectively. Teranode v0.16.1 supplies the native gossip reference. [BDK](https://github.com/bitcoin-sv/bdk/tree/8a4e1d3ddb16cbb2b3a98f82ea1ee6ff19a297e9) · [SV Node](https://github.com/bitcoin-sv/bitcoin-sv/tree/6504a3aff65ba97c0f6c80962b033e35ecbfed4b) · [Teranode](https://github.com/bsv-blockchain/teranode/tree/807f1bea39f52797c72fd157a543e8bb64622510)

To identify an exact source build, run the following from a Git checkout with Docker and Python 3.12 or later. The builder archives the committed Docker inputs, builds the compiler and runtime stages, starts the executable on an isolated loopback network, and checks its reported identity against the binary and native archive on disk. The receipt includes the source tree, Cargo lock digest, Rust compiler, linked interpreter versions and executable digest. Current Docker builds give each compilation a fresh Cargo target directory and cache downloaded registry and Git dependencies. BuildKit can reuse a completed stage for identical inputs. Docker image identifiers describe the local build and can vary with the builder and package mirror. [Linux ARM64 build receipt](evidence/chronicle-linux-release.json)

```sh
python3 qualification/release_build.py 781ddd73ac78bd1887138fad331eeace21488509 .native/release-build.json
```

On Linux ARM64, the `af9b605` build and the fresh Cargo target build at `6ad4152` produced the same executable SHA-256, `833fbeecc17a45de9edc8777123708ecd2e390d522c436b867a74a208318a528`. Both running nodes reported BDK 1.4.0 and SV Node 1.2.3, and the build-stage native archive matched the embedded digest. Reproduce either build by passing its commit to the command above. [Fresh target build](evidence/chronicle-qualification-linux-release.json) · [Second image with matching executable](evidence/chronicle-second-linux-build.json)

To build the macOS observer from the same committed Rust source, build the pinned native archive first. Run the following in the repository root with the Rust 1.98.1 toolchain on `PATH`. This uses a fresh Cargo target and independently checks the operator interface on the resulting executable. Its manifest records the platform compiler and archive digest. [Measured macOS build](evidence/chronicle-qualification-macos-release.json)

```sh
release_source=$(mktemp -d)
release_target=$(mktemp -d)
git archive af9b605ad8da96d041a540111d46a7c0105209f7 -- Cargo.toml Cargo.lock examples native scripts/build-bdk.sh src | tar -x -C "$release_source"
ln -s "$(cd .native && pwd -P)" "$release_source/.native"
CARGO_TARGET_DIR="$release_target" cargo build --offline --manifest-path "$release_source/Cargo.toml" --locked --release
python3 qualification/operator_access.py --binary "$release_target/release/scary-node" .native/host-build-probe.json
```

## Reproduce the reference comparisons

Start Docker. The DAR driver gives a fresh SV Node private regtest funds, signs messages with three distinct lab keys, and starts Scary Node empty on its native SV TCP peer. The lab API fixture serves the signed records in the maintained receiver's read format. It includes a freeze predating entry. The worker mines the authorized confiscation, Scary Node restarts, then the test visits both sides of the 1,000-block maturity boundary. SV Node selects the recipient's solved block as its active tip. A longer branch restores the recovered outpoint, and re-inclusion consumes it again. The last assertions compare tip hashes and solution dispositions.

```sh
python3 -m venv .native/py
.native/py/bin/pip install ecdsa==0.19.1
cargo build --locked --example chronicle
(cd qualification/go && go build -o ../../.native/go-oracle .)
.native/py/bin/python qualification/chronicle.py .native/chronicle.json
.native/py/bin/python qualification/chronicle_signatures.py .native/signatures.json
.native/py/bin/python qualification/block_context.py --binary ./target/release/scary-node .native/block-context.json
.native/py/bin/python qualification/alert_receipts.py --binary ./target/release/scary-node .native/alert-item-receipts.json
python3 qualification/sv_dar.py .native/sv-dar.json
python3 qualification/scary_dar.py --binary ./target/release/scary-node .native/scary-dar.json
python3 qualification/directive_race.py --binary ./target/release/scary-node .native/directive-race.json
python3 qualification/verify_chronicle.py
./qualification/throughput.sh .native/throughput
```

Each oracle receives the same serialized transaction and its actual previous outputs. The policy call uses the tip height. The consensus call uses the containing height. The SV oracle offers each transaction to the mempool and submits a separately solved regtest block. Save the rejected rows and the reasons. The functional corpus comes from the source pin used to build BDK. The lab uses permissive regtest relay configuration, and the recorded configuration belongs with every comparison.

The throughput driver constructs four 4,096-transaction bodies that spend distinct witnessed P2PKH outputs. Each spend has a valid signature and a known fee. The timer starts before decoding the block and stops after atomic checkpoint replacement and directory sync. The first funding round has unknown inputs and is reported separately. Signing the fixtures and transporting blocks are outside that measurement. The benchmark exercises the release-profile validation and storage code in a test executable. Mixed-validator propagation, sustained queueing and worker opportunity loss require the network workload.

To measure SV Node validation, native transfer, Rust BDK checks and durable tip updates together, use the release binary and the pinned functional-test Python environment. Each body spends 4,096 distinct P2PK outputs. The 128-round run measures more than half a million signed transactions. The mixed lab starts two independent Teranode stores, one SV Node store, the Rust entrant and its Stratum worker. The SV reference receives the initial common history before the race and receives the actual solution directly on its P2P socket.

```sh
cargo build --locked --release
.native/py/bin/python qualification/native_workload.py .native/native-workload.json
.native/py/bin/python qualification/native_workload.py --rounds 128 .native/native-sustained.json
./regtest/mixed.sh > .native/mixed.json
python3 regtest/verify_receipt.py .native/mixed.json
python3 qualification/capacity_recovery.py .native/capacity-recovery.json
.native/py/bin/python qualification/capacity_recovery.py --undo .native/undo-capacity.json
.native/py/bin/python qualification/candidate_capacity.py --binary target/release/scary-node .native/candidate-capacity.json
.native/py/bin/python qualification/peer_loss.py .native/peer-loss.json
.native/py/bin/python qualification/large_body.py .native/large-wire.json
./regtest/mixed.sh --down
```

To reproduce the 4,096-payment mixed Stratum block, start with fresh scoped volumes, then replay the checked-in funding, candidate and successor commitments. The storage fault driver runs against the release image produced by the mixed lab.

```sh
./regtest/mixed.sh --workload 4096 > .native/mixed-load.json
.native/py/bin/python qualification/storage_fault.py .native/storage-fault.json --interrupt
docker build -t scary-node:clock-lab -f qualification/clock.Dockerfile .
.native/py/bin/python qualification/clock_fault.py .native/clock-fault.json
python3 qualification/verify_chronicle.py
./regtest/mixed.sh --down
```

The repeated mixed workload uses a separate fixed-cadence background client and includes all candidate and successor bytes. Run it on fresh volumes after stopping the prior mixed lab.

```sh
./regtest/mixed.sh --stream > .native/mixed-stream.json
gzip -c .native/mixed-stream.json > .native/mixed-stream.json.gz
python3 qualification/verify_stream.py .native/mixed-stream.json.gz
./regtest/mixed.sh --down
```

For the 24-round population, use fresh regtest volumes and run the following commands. This profile uses 256 transactions per authenticated operator request and 96 historical background spends. The verifier checks each fee, Merkle root, peer wire digest, successor and background inclusion.

```sh
./regtest/mixed.sh --stream sustained > .native/mixed-sustained.json
gzip -c .native/mixed-sustained.json > .native/mixed-sustained.json.gz
python3 qualification/verify_stream.py .native/mixed-sustained.json.gz
./regtest/mixed.sh --down
```

To check native settlement between stock reference nodes, leave the mixed network running after a successful proof and run `python3 qualification/mixed_native.py .native/native-successors.json` before teardown. The fixture restarts the second Teranode on its same volume and connects its stock legacy service to SV Node. A container-local host route lets stock Teranode select a source address on the isolated bridge. The launcher drops its network administration capability before starting the daemon. Twelve alternating solves exercise both directions over native protocols. [Stock legacy service](https://github.com/bsv-blockchain/teranode/blob/807f1bea39f52797c72fd157a543e8bb64622510/docs/topics/services/legacy.md)

With the same native link established, run the signed-block workload before tearing down the mixed network. The driver uses a single stock SV submission endpoint and gives the other three nodes only their normal peer connections. Its timer starts after signing and block construction and stops when all four stores select the final hash. It allows up to four blocks in flight and then checks every selected height, three full-node wire readbacks and the entrant’s signature counts. The shared workstation lock keeps race sessions outside the measurement.

```sh
.native/py/bin/python qualification/mixed_native_load.py .native/mixed-native-load.json
python3 qualification/verify_mixed_native_load.py .native/mixed-native-load.json
.native/py/bin/python qualification/mixed_native_load.py --rounds 32 --fanout 16384 .native/mixed-native-large.json
python3 qualification/verify_mixed_native_load.py .native/mixed-native-large.json
```

Pass `--rollback-binary PATH` to the large-body driver to restore the new checkpoint with an older executable and verify its next active tip against SV Node. Pin the older build and retain its digest. The measured rollback uses a body below the MessagePack single-binary limit.

### Exercise the running entrant on the reference vectors

The following driver gives the entrant the actual funding block and one descendant before comparing the same transaction at its admission and read-only consensus interfaces. For policy-eligible cases a real Stratum worker submits the entrant’s solved bytes. The stock SV node reads back the selected block. Consensus-valid cases outside policy use an independently assembled reference block. Each result retains the parent, signed transaction, witnessed checks, independent outcomes and full selected bytes. The verifier reconstructs each extended input from its funded output and replays the block commitments.

```sh
.native/py/bin/python qualification/entrant_chronicle.py .native/entrant-chronicle.json.gz \
  --binary target/release/scary-node
python3 qualification/verify_entrant_chronicle.py .native/entrant-chronicle.json.gz
```

## Native receiver integration on regtest

Build the v0.3.5 synchronization harness from its pinned source. Its Go overlay supplies loopback peers and a lab publisher, while the receiver uses the upstream StreamThread, signature verifier, SQLite models, processing retry, HTTP handlers and node RPC client. All lab streams bind to loopback with a separate regtest protocol identifier. The Python driver creates the lab trust keys and signs genuine alerts, starts the bearer-authenticated gateway and submits Stratum solves to stock SV Node v1.2.3. The receipt records independently queried active hashes, signed records and receiver application state.

```sh
python3 qualification/receiver/build.py
python3 qualification/receiver_dar.py --binary ./target/release/scary-node .native/receiver-dar.json
```

To keep an exact replayable fixture, export the full signed lifecycle and its two block histories before the reference volume closes. The offline verifier checks the funding chain, rotation and signatures, original and replacement branches, and the recipient signature. The optional fresh SV replay re-applies the signed UTXO intervals and authorization using Bitcoin SV Node v1.2.3's administrative RPC and submits the archived byte strings.

```sh
.native/py/bin/python qualification/receiver_dar.py --retain-history \
  --binary target/release/scary-node .native/receiver-dar-history.json.gz
.native/py/bin/python qualification/verify_dar_history.py .native/receiver-dar-history.json.gz
.native/py/bin/python qualification/verify_dar_history.py --replay-sv .native/receiver-dar-history.json.gz
```

Use the stock Teranode Alert Service fixture to compare signed notice, freeze, unfreeze and ordinary payment admission on two Teranodes, SV Node and Scary Node. The independent verification uses the pinned Python test environment. [Stock Teranode alert fixture and evidence](teranode-alert-interoperability.md)

## Operating the signed alert receiver

Run the maintained `go-alert-system` v0.3.5 receiver with its official environment-specific configuration. Keep its raw HTTP API on a private network or behind a loopback-only port mapping. Give its SQLite datastore and P2P key durable storage. Retain the official five mainnet trust keys. Use an independent SQLite database per network with the table prefix `alert_system`. The pinned receiver uses this literal table prefix to retire the previous key set during rotation. For an existing receiver with a different prefix, preserve its DB and configure a fresh DB with this prefix. Replay and verify the signed history and recent peer synchronization before assigning work. [Upstream key update query](https://github.com/bsv-blockchain/go-alert-system/blob/c19f9959fb979f9def944807bd4e58ecadf25717/app/models/public_key.go#L96) The genesis packet is common to the release and the environment selects the trust keys. Configure its node RPC user as `scary`, the password from the gateway token file, and the host as the gateway address reachable from the receiver. Set the receiver timezone to UTC and its `log_output_file` to a private durable file readable by the gateway. The gateway listens on loopback. Co-locate it with the receiver or use a private authenticated tunnel. During an empty-state bootstrap the receiver can disable its initial best-block RPC probe. [Upstream receiver release](https://github.com/bsv-blockchain/go-alert-system/releases/tag/v0.3.5) · [Mainnet configuration](https://github.com/bsv-blockchain/go-alert-system/blob/c19f9959fb979f9def944807bd4e58ecadf25717/app/config/envs/mainnet.json)

```sh
python3 scripts/alert-gateway.py --receiver http://127.0.0.1:3000 \
  --node http://127.0.0.1:8080 --port 19366 --receiver-sync-log "$RECEIVER_LOG" \
  --token-file "$ALERT_TOKEN_FILE" --operator-token-file "$OPERATOR_TOKEN_FILE"
```

Scary Node independently checks the signed contiguous prefix and rotations before applying actions. The gateway forwards node RPC requests to a receipt lookup. A receipt requires the exact previously signed action and its durable sequence. Informational notices, effect intervals, authorizations and block or IP actions remain available in the signed journal under the network data directory. The operator API exposes receiver connectivity, last verified and applied sequences, the durable checkpoint sequence and outstanding actions. The upstream health response retains a peer count and reports a fixed synchronization flag. Mainnet work also requires a successful native peer-sync log event at the receiver's reported signed sequence within the last 20 minutes on the UTC clock. The upstream default peer discovery interval is 10 minutes. Missing, stale and future-dated evidence all withhold work. An unreachable receiver or missed sequence removes work readiness. [Upstream health handler](https://github.com/bsv-blockchain/go-alert-system/blob/c19f9959fb979f9def944807bd4e58ecadf25717/app/api/base/health.go) · [Successful peer sync](https://github.com/bsv-blockchain/go-alert-system/blob/c19f9959fb979f9def944807bd4e58ecadf25717/app/p2p/server.go) Restore the receiver or upstream feed before resuming. Retain the complete data directory when restoring an identity. Keep one writer, and preserve the extranonce watermark across restores.

For passive operation supply a payout script for the unsigned candidate, a journal path, and optional independent current-tip SV peers. The shadow and Stratum modes are mutually exclusive.

```sh
./target/release/scary-node --data-dir "$STATE_DIR" --shadow-log "$SHADOW_LOG" \
  --payout-script "$PAYOUT_SCRIPT_HEX" --sv-peer "$SV_PEER" \
  --operator-token-file "$OPERATOR_TOKEN_FILE" \
  --alert-service-url http://127.0.0.1:19366 --alert-token-file "$ALERT_TOKEN_FILE"
```

The failover driver uses the same private data directory across restarts. After a confirmed Teranode Asset delivery it restricts body intake to native SV TCP, records the selected parent and successor, and restores both transports. Teranode gossip continues to supply independent tip reports. The audit checks header proof of work, unsigned Merkle commitments, source transitions, difficulty predictions and reference hashes observed within two hours of a tip decision. An attempted failover qualifies only when a direct SV body extends the restored parent. A reference advancing without body coverage is recorded.

```sh
python3 qualification/shadow_failover.py --binary ./target/release/scary-node \
  --journal "$SHADOW_LOG" --receipt "$FAILOVER_RECEIPT" -- \
  --data-dir "$STATE_DIR" --sv-discovery --payout-script "$PAYOUT_SCRIPT_HEX" \
  --operator-token-file "$OPERATOR_TOKEN_FILE" --alert-token-file "$ALERT_TOKEN_FILE" \
  --alert-service-url http://127.0.0.1:19366
python3 qualification/shadow.py "$SHADOW_LOG"
python3 qualification/shadow_export.py "$SHADOW_LOG" "$FAILOVER_RECEIPT" .native/shadow-public.json
```

The [passive mainnet checkpoint](mainnet-shadow.md) includes replayable raw headers, a separate public chainwork anchor, signed-state readiness checks and the finite 1,008-block collection command.

## Operator handling of notices

Assign an on-call operator and a deputy to the official notice channel and alert telemetry. Record receipt time, signed sequence, notice terms, affected items and effective heights. Track any specified deadline and route a failed item to the on-call operator while work is paused. Preserve signed bytes, application receipts, job history and independent tip observations for the resolution. Deliver informational notices to the responsible operator. On a substantive rule or interpreter change, pin the new reference and rerun qualification before resuming the affected work. Escalate a significant threat or potential directive event and route official information requests to the responsible operator for a prompt, lawful response. Review the published rules and the actual notice for effective time, deadlines and disclosure restrictions. [NAR Part I, especially I.5](https://nar.bsvblockchain.org/network-access-rules/part-i-master-rules) · [NAR Part III, especially III.2 and III.7](https://nar.bsvblockchain.org/network-access-rules/part-iii-enforcement-rules)

## Production acceptance evidence

| Question | Published observation | Qualification still open |
|---|---|---|
| Does ordinary validation agree with the pinned reference? | The live entrant, Rust BDK, GoBDK and stock SV Node agreed on all 124 policy and consensus decisions. A Stratum worker mined all 46 policy-eligible cases. SV Node selected every block. [Live vector receipt](evidence/chronicle-entrant-vectors.json.gz) | The pinned corpus passed. Future consensus changes require a fresh comparison. |
| Does the recovered asset reach its new owner? | An official signed receiver supplied ten messages spanning all eight types, including rotation. A second stock SV Node replayed the exact history, mature recipient spend and replacement branch. Both stock Teranodes independently applied ordinary freeze and unfreeze. [SV Node replay and Teranode observations](teranode-alert-interoperability.md) | SV Node v1.2.3 defines the DAR pass condition. Record matching or divergent Teranode observations as interoperability evidence. Its documented ownership-reassignment outcome is outside the release gate. |
| Does work survive faults and changing rules? | Same-parent work survived refreshes. The lab exercised both orderings of a signed freeze and a solution, interrupted writes, disk limits, peer loss, software rollback and clock offsets before and after startup. [Release evidence](chronicle-results.md) | Three archive-built release trials recovered a 12-block ordinary fork, a signed invalidation and a 34-block fork after disconnect. Stock SV Node selected a fee-paying entrant block and mined a successor in each case. The entrant restored all three successors from disk. Two additional stock-valid proposals overlapped the delivery of a signed freeze. The entrant returned stale-job errors in both cases. [Recovery receipts](deep-recovery.md) · [Concurrent directive and solution receipts](directive-overlap.md) |
| How much native work can the system sustain? | Four independent stores selected 524,288 signed spends over 32 identical bodies at 15,140 spends/s on the disk-backed Aerospike configuration. [Paired storage trial](mixed-storage-trial.md) | The direct-wallet trial confirmed 1,143,159 of 5,529,600 transfers through drain, or 544.36/s. It missed the sustained target. The production-settings inventory replication confirmed all 5,529,600 transfers through two successors at 3,037.51/s. The entrant checked every payment and issued zero jobs in 225 work slots. Sustained nonempty Stratum production and receipt of a 1.8-million-payment block are separate qualifications. [Direct-wallet evidence](sustained-native-trial.md) · [Production-settings replication](sustained-native-replication.md) |
| Does witnessed entry add stale risk under equal propagation? | Two stock native relay pilots produced opposite stale-rate differences. An immediate-notification change reduced median witnessed submit-to-active time from 533 to 46 ms in four paired trials. [Relay study](production-cohort.md) | Finish both fixed fresh evaluations and the independent receiver-ingress experiment, retaining the full/full control and every unready opportunity. [Propagation measurement](propagation-control.md) |
| Can the entrant follow the live network continuously? | Passive mainnet restart at height 976520 restored 99 linked headers, 30,553 outputs and signed sequence 1. A controlled peer failover had already delivered a successor over the alternate protocol. [Mainnet journal and scope](mainnet-shadow.md) | Observe and reconcile 1,008 consecutive contemporary blocks, including unsigned transaction-bearing decisions, restored state and peer failover. |

A locally originated block relies on the selected parent, the entrant’s evidenced inputs and the effective directives. Later proof of work can extend an earlier violation the entrant could not see. Two miners can also disagree about a recovery outcome. Qualification reports each receiver’s actual disposition. [Validation model](validation.md) · [Teranode reassignment documentation](https://github.com/bsv-blockchain/teranode/blob/807f1bea39f52797c72fd157a543e8bb64622510/docs/topics/services/alert.md#24-utxo-reassignment)

### Stratum identity and standby recovery

Build the release and run `python3 qualification/mining_lease.py .native/mining-lease.json`. Two actual node processes attempt subscriptions using separate state directories containing the same identity. The driver checks enforcement while both are alive, kills the active process, then reconnects a stale standby to the original allocation authority. A third miner uses an independent identity with the same payout. No network proof of work is needed to exercise the allocation boundary. [Measured connection numbers](evidence/chronicle-mining-lease.json)

## Mining clock at startup

The node compares the host wall clock with elapsed monotonic time and with the UTC timestamps of completed native SV protocol handshakes. The network comparison takes a median within each IPv4 /16 or IPv6 /32 prefix, then a median across groups. A mainnet comparison needs three groups. An isolated regtest can compare against its single stock peer. A majority of group representatives must agree within 120 seconds of their median. A discrepancy exceeding 120 seconds suspends work. Losing a connection preserves the last corroborated comparison until new evidence replaces it. Candidate timestamps and worker time rolls use the earlier of local and corroborated peer time for the two-hour limit.

Operator status exposes the endpoint and group counts, the last corroborated offset from the current wall clock, and disagreement. With insufficient samples the initial offset is null. A Teranode-only connection supplies no version-message UTC sample. Synchronize the host independently before starting it. This check provides corroboration from reachable peers and does not authenticate UTC. Reproduce the isolated startup and running-clock checks using the clock fault command above.
