# Scary Node

Scary Node is an alpha Rust implementation of a BSV mining node. Its default command listens to the native Teranode peer network on BSV mainnet. An operator can enable block observation, submit signed spends of recently witnessed outputs and give a locally assembled block candidate to an ASIC over Stratum V1.

The node starts with an empty working set. It sees an output created, observes other miners build on its block, and tracks subsequent spends on that branch. A spend becomes eligible only after the creating block has a successor and the node has processed the complete bodies covering the intervening interval. Continued proof of work is the entrant’s basis for recognizing accepted history. This alpha concentrates on ordinary P2PKH payments and places no claim on outputs it has never observed. [Validation model](docs/validation.md)

## Listen on mainnet

Install a current stable Rust toolchain, then build the checked-in revision and dependency lock.

```sh
git clone https://github.com/p2ppsr/scary-node.git
cd scary-node
cargo build --release --locked
./target/release/scary-node
```

The node discovers the published Teranode mainnet bootstrap peers, subscribes to native status, block and subtree gossip, and prints peer and block observations. Startup and a connected peer are distinct events. Block announcements depend on network activity. Use the status API to inspect the process, and Ctrl-C to stop it.

```sh
curl -fsS http://127.0.0.1:8080/status
./target/release/scary-node --help
```

Use repeated `--peer <multiaddr>` options to select specific peers. A peer identity is stored under `--data-dir`. The alpha rebuilds its witnessed transaction set after restart. Use `--observe-bodies` to fetch and verify block bodies while listening. Network, API and asset configuration are documented in the [operator guide](docs/operations.md).

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

## Point an ASIC at your node

Set the payout locking script to an address you control. Run the node with a reachable advertised Asset URL so that peers can retrieve the block you announce. Enable the Stratum listener on a trusted mining LAN. The example expects the operator to supply the two environment variables.

```sh
./target/release/scary-node \
  --stratum-bind 0.0.0.0:3333 \
  --asset-bind 0.0.0.0:9090 \
  --asset-url "$PUBLIC_ASSET_BASE" \
  --payout-script "$PAYOUT_SCRIPT_HEX"
```

Configure the ASIC’s pool URL as `stratum+tcp://YOUR_NODE:3333`, enter a worker name and use `x` as the password. Check `GET /status` for the observed tip. After entry, use a wallet you control to fund a P2PKH output and wait for the node to observe its block and a successor. In the wallet, sign a fee-paying child of that exact output with `SIGHASH_ALL | FORKID`, save its raw bytes to `child.tx`, and submit it to your node before broadcasting it elsewhere. See the [operator guide](docs/operations.md) for the admitted transaction format.

```sh
curl --fail-with-body --data-binary @child.tx \
  -H 'Content-Type: application/octet-stream' http://127.0.0.1:8080/transactions
```

Inspect readiness before assigning work. On mainnet the node first needs contiguous difficulty history and an eligible transaction. Winning the network target requires real hashpower and is probabilistic. The independently reproducible block proof supplied with this release runs on regtest. [Protocol and peer interoperability](docs/protocol.md) · [Open BSV License](LICENSE)

**This is alpha-quality proof-of-concept software.**
