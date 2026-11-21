# Scary Node

Scary Node is an alpha Rust implementation of a BSV mining node. Its default command listens to the native Teranode peer network on BSV mainnet. An operator can enable block observation, submit signed spends of recently witnessed outputs and give a locally assembled block candidate to an ASIC over Stratum V1.

The node starts with an empty working set. It sees an output created, observes other miners build on its block, and tracks subsequent spends on that branch. A spend becomes eligible only after the creating block has a successor and the node has processed the complete bodies covering the intervening interval. Continued proof of work is the entrant’s basis for recognizing accepted history. Scary Node checks Chronicle-era ordinary spends with the shared BDK interpreter. Signed directives govern frozen outputs and authorized confiscations. [Validation model](docs/validation.md)

## Listen on mainnet

Install Rust 1.98.1 and the native build prerequisites. The build fetches the pinned upstream interpreter and links it into the node. [Build and qualification](docs/chronicle-qualification.md)

```sh
git clone https://github.com/p2ppsr/scary-node.git
cd scary-node
./scripts/build-bdk.sh
cargo build --release --locked
./target/release/scary-node
```

The node discovers the published Teranode mainnet bootstrap peers, subscribes to native status, block and subtree gossip, and prints peer and block observations. Startup and a connected peer are distinct events. Block announcements depend on network activity. Use the status API to inspect the process, and Ctrl-C to stop it.

```sh
curl -fsS http://127.0.0.1:8080/status
./target/release/scary-node --help
```

Use repeated `--peer <multiaddr>` options to select specific peers. A peer identity is stored under `--data-dir`. The node checkpoints its witnessed branch, undo data, directive sequence and solved-block state. Use `--observe-bodies` to fetch and verify block bodies while listening. Network, API and asset configuration are documented in the [operator guide](docs/operations.md).

## Demonstrate mining locally

Install Docker with Compose v2, Python 3, and OpenSSL with secp256k1 support. The isolated lab contains Scary Node and two Teranode reference nodes. Run these commands in order.

```sh
./regtest/up.sh
./regtest/prove.sh
./regtest/down.sh
```

The references establish a chain before the empty-state entrant joins. One reference mines a fresh funding output and the other builds on it. The driver sends its signed child to Scary Node and uses a CPU Stratum worker to return valid regtest proof of work. Both Teranodes must adopt the exact solved block, serve its bytes, and agree on a successor. The lab prints a JSON receipt you can verify offline. [Regtest procedure](docs/regtest.md) · [Recorded runs and verification](docs/verification.md)

## Follow a newcomer on an active chain

The longer lab starts two Teranodes, circulates hundreds of signed payments among four wallets, and then brings up Scary Node on a fresh volume. An independent replay measures how many live outputs Rust learns and how many reference-block inputs it can check. The same sixteen payment accounts submit work at each of three Stratum rounds. Compare which offers Rust can mine, the size of its solved blocks, and what the reference miners do with the remaining offers.

```sh
./regtest/scenario.sh --seed 7 > scenario-7.json
python3 regtest/verify_scenario.py scenario-7.json
./regtest/scenario.sh --down
```

[Workload and reproduction](docs/scenario.md) · [Measured results](docs/scenario-results.md)

## Compare stale blocks under equal work

The race lab gives a Scary Node Stratum job and a full-state Teranode job one SHA256d attempt each per tick. They mine on the same parent with the same signed payment. An independent SV Node receives every discovery through a shared relay. The run records both candidates in a simultaneous discovery and checks which branch still holds after two successors.

```sh
./regtest/races.sh --seed 1701 --trials 128 > races-1701.json
./regtest/races.sh --down
```

[Experimental design and reproduction](docs/races.md) · [Stale rates and uncertainty](docs/race-results.md)

The native-receiver study also measures opportunities when a miner has no ready job and includes a full-state versus full-state control. Four paired engineering trials found that immediate SV publication reduced median solo submit-to-active readback from 533 ms to 46 ms. The preregistered evaluation on each frozen executable measures stale rates and their uncertainty. [Native propagation study](docs/production-cohort.md)

## Verify job and branch recovery

The reliability proof submits an older Stratum job after a same-parent update, checks settlement on two Teranodes, and follows a short reorg while preserving the witnessed state it can still use. The larger matched-work sample also checks each locally solved block against its settlement ancestry.

[Run the reliability proof and cohort](docs/reliability.md) · [Reliability results](docs/reliability-results.md)

## Verify Chronicle validation and asset recovery

Run the pinned Rust, Go and SV Node comparison, followed by the signed DAR lifecycle. The latter includes two Scary Node Stratum solutions, a process restart, maturity checks and a reorg. Run `regtest/mixed.sh` for independent SV Node and Teranode adoption, or `regtest/mixed.sh --stream sustained` to reproduce 24 mixed-network Stratum rounds. The four-store block replay covers 524,288 signed spends. The separate 30-minute lab uses native wallet connections and 300 scheduled mining opportunities. [Reproduction and production acceptance](docs/chronicle-qualification.md) · [Measured results](docs/chronicle-results.md) · [Native wallet and Stratum lab](docs/sustained-native-trial.md)

## Point an ASIC at your node

Set the payout locking script to an address you control. Run the node with a reachable advertised Asset URL so that peers can retrieve the block you announce. Enable the Stratum listener on a trusted mining LAN. Supply the payout script, public asset URL and credential-file paths. Configure the alert receiver and authenticated gateway first. [Receiver and operator procedure](docs/chronicle-qualification.md)

```sh
./target/release/scary-node \
  --operator-token-file "$OPERATOR_TOKEN_FILE" --worker-password-file "$WORKER_PASSWORD_FILE" \
  --alert-service-url http://127.0.0.1:19366 --alert-token-file "$ALERT_TOKEN_FILE" \
  --stratum-bind 0.0.0.0:3333 \
  --asset-bind 0.0.0.0:9090 \
  --asset-url "$PUBLIC_ASSET_BASE" \
  --payout-script "$PAYOUT_SCRIPT_HEX"
```

Configure the ASIC’s pool URL as `stratum+tcp://YOUR_NODE:3333`, enter a worker name and use the password from the worker credential file. Check `GET /status` for the observed tip. After entry, use a wallet you control to fund a P2PKH output and wait for the node to observe its block and a successor. In the wallet, sign a fee-paying child of that exact output with `SIGHASH_ALL | FORKID`, save its raw bytes to `child.tx`, and submit it to your node before broadcasting it elsewhere. See the [operator guide](docs/operations.md) for the admitted transaction format.

```sh
curl --fail-with-body --data-binary @child.tx \
  -H "Authorization: Bearer $(cat "$OPERATOR_TOKEN_FILE")" \
  -H 'Content-Type: application/octet-stream' http://127.0.0.1:8080/transactions
```

Inspect readiness before assigning work. On mainnet the node first needs contiguous difficulty history and an eligible transaction. Winning the network target requires real hashpower and is probabilistic. The independently reproducible block proof supplied with this release runs on regtest. [Protocol and peer interoperability](docs/protocol.md) · [Open BSV License](LICENSE)

**This is alpha-quality proof-of-concept software.**
