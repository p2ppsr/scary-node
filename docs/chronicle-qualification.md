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

Pass `--rollback-binary PATH` to the large-body driver to restore the new checkpoint with an older executable and verify its next active tip against SV Node. Pin the older build and retain its digest. The measured rollback uses a body below the MessagePack single-binary limit.

## Native receiver integration on regtest

Build the v0.3.5 synchronization harness from its pinned source. Its Go overlay supplies loopback peers and a lab publisher, while the receiver uses the upstream StreamThread, signature verifier, SQLite models, processing retry, HTTP handlers and node RPC client. All lab streams bind to loopback with a separate regtest protocol identifier. The Python driver creates the lab trust keys and signs genuine alerts, starts the bearer-authenticated gateway and submits Stratum solves to stock SV Node v1.2.3. The receipt records independently queried active hashes, signed records and receiver application state.

```sh
python3 qualification/receiver/build.py
python3 qualification/receiver_dar.py --binary ./target/release/scary-node .native/receiver-dar.json
```

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

## Remaining production qualification

The required evidence includes a signed history through a live receiver, directive-versus-solution races, clock and storage fault recovery, interrupted writes and software rollback. It also includes independent SV Node and Teranode delivery for the same DAR bytes, large contiguous bodies at sustained load, independent receivers for full-state controls, opportunities during unready intervals and a prospective 1,008-block shadow run with restart and peer failover. Record durations, hardware, queue depth, uncovered intervals and each endpoint's verdict. SV Node determines the DAR outcome. [Teranode's documented ownership reassignment behavior](https://github.com/bsv-blockchain/teranode/blob/807f1bea39f52797c72fd157a543e8bb64622510/docs/topics/services/alert.md#24-utxo-reassignment)

### Stratum identity and standby recovery

Build the release and run `python3 qualification/mining_lease.py .native/mining-lease.json`. Two actual node processes attempt subscriptions using separate state directories containing the same identity. The driver checks enforcement while both are alive, kills the active process, then reconnects a stale standby to the original allocation authority. A third miner uses an independent identity with the same payout. No network proof of work is needed to exercise the allocation boundary. [Measured connection numbers](evidence/chronicle-mining-lease.json)
