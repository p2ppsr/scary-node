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
./regtest/mixed.sh --down
```

## Operating the signed alert receiver

Run the maintained `go-alert-system` v0.3.5 receiver with its official environment-specific configuration. Keep its raw HTTP API on a private network or behind a loopback-only port mapping. Give its SQLite datastore and P2P key durable storage. Retain the official five mainnet trust keys. The genesis packet is common to the release and the environment selects the trust keys. Configure its node RPC user as `scary`, the password from the gateway token file, and the host as the gateway address reachable from the receiver. The gateway listens on loopback. Co-locate it with the receiver or use a private authenticated tunnel. During an empty-state bootstrap the receiver can disable its initial best-block RPC probe. [Upstream receiver release](https://github.com/bsv-blockchain/go-alert-system/releases/tag/v0.3.5) · [Mainnet configuration](https://github.com/bsv-blockchain/go-alert-system/blob/c19f9959fb979f9def944807bd4e58ecadf25717/app/config/envs/mainnet.json)

```sh
python3 scripts/alert-gateway.py --receiver http://127.0.0.1:3000 \
  --node http://127.0.0.1:8080 --port 19366 \
  --token-file "$ALERT_TOKEN_FILE" --operator-token-file "$OPERATOR_TOKEN_FILE"
```

Scary Node independently checks the signed contiguous prefix and rotations before applying actions. The gateway forwards node RPC requests to a receipt lookup. A receipt requires the exact previously signed action and its durable sequence. Informational notices, effect intervals, authorizations and block or IP actions remain available in the signed journal under the network data directory. The operator API exposes receiver connectivity, last verified and applied sequences, the durable checkpoint sequence and outstanding actions. An unreachable receiver or missed sequence removes work readiness. Restore the receiver or upstream feed before resuming. Retain the complete data directory when restoring an identity. Keep one writer, and preserve the extranonce watermark across restores.

For passive operation supply a payout script for the unsigned candidate, a journal path, and optional independent current-tip SV peers. The shadow and Stratum modes are mutually exclusive.

```sh
./target/release/scary-node --data-dir "$STATE_DIR" --shadow-log "$SHADOW_LOG" \
  --payout-script "$PAYOUT_SCRIPT_HEX" --sv-peer "$SV_PEER" \
  --operator-token-file "$OPERATOR_TOKEN_FILE" \
  --alert-service-url http://127.0.0.1:19366 --alert-token-file "$ALERT_TOKEN_FILE"
```

## Operator handling of notices

Assign an on-call operator and a deputy to the official notice channel and alert telemetry. Record receipt time, signed sequence, notice terms, affected items and effective heights. Track any specified deadline and route a failed item to the on-call operator while work is paused. Preserve signed bytes, application receipts, job history and independent tip observations for the resolution. Deliver informational notices to the responsible operator. On a substantive rule or interpreter change, pin the new reference and rerun qualification before resuming the affected work. Escalate a significant threat or potential directive event and route official information requests to the responsible operator for a prompt, lawful response. Review the published rules and the actual notice for effective time, deadlines and disclosure restrictions. [NAR Part I, especially I.5](https://nar.bsvblockchain.org/network-access-rules/part-i-master-rules) · [NAR Part III, especially III.2 and III.7](https://nar.bsvblockchain.org/network-access-rules/part-iii-enforcement-rules)

## Remaining production qualification

The required evidence includes a signed history through a live receiver, directive-versus-solution races, clock and storage fault recovery, interrupted writes and software rollback. It also includes independent SV Node and Teranode delivery for the same DAR bytes, large contiguous bodies at sustained load, independent receivers for full-state controls, opportunities during unready intervals and a prospective 1,008-block shadow run with restart and peer failover. Record durations, hardware, queue depth, uncovered intervals and each endpoint's verdict. SV Node determines the DAR outcome. [Teranode's documented ownership reassignment behavior](https://github.com/bsv-blockchain/teranode/blob/807f1bea39f52797c72fd157a543e8bb64622510/docs/topics/services/alert.md#24-utxo-reassignment)
