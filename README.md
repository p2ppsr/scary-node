# Scary Node

Scary Node is an alpha Rust mining node for Bitcoin SV (BSV). It joins the native Teranode network, follows contemporary blocks and can give an ASIC a candidate over Stratum V1. The default command listens on mainnet. The included Docker lab lets you mine a block locally and ask two independently stored Teranodes what they accepted.

## How can a miner start empty?

Suppose Alice receives an output in a block Scary Node sees. Another miner extends that block. If Scary Node has processed every intervening body on the selected branch, it knows Alice’s locking script, amount, creation height and whether the output has since been spent. It can check a signed payment from Alice against the shared BDK interpreter, account for the fee, and include the payment in a candidate. It can also include a child of an earlier validated transaction in that candidate.

For an input created before entry, those facts may be unavailable. Scary Node leaves that spend out of its own work and reports unavailable checks as deferred on incoming blocks. Accepting the selected parent rests on subsequent proof of work and the effective signed directives. A violation confined to unwitnessed history can escape detection. The size of the usable set depends on which outputs people spend and how continuously the node observes the chain. [Validation rules](docs/validation.md) · [Two measured entry runs](docs/scenario-results.md)

## Try it on regtest

Clone the repository. To see what a solved block contains, replay a saved run with Python 3. This first check works offline from the checkout.

```sh
git clone https://github.com/p2ppsr/scary-node.git
cd scary-node
python3 regtest/verify_receipt.py docs/evidence/regtest-release.json
```

To produce your own receipt, start Docker with Compose v2 and install Python 3 and an OpenSSL executable with secp256k1 support. The first build downloads the pinned native interpreter, Rust dependencies and reference images. Start from fresh lab volumes.

```sh
mkdir -p .regtest
./regtest/up.sh
./regtest/prove.sh > .regtest/receipt.json
python3 regtest/verify_receipt.py .regtest/receipt.json
./regtest/down.sh
```

The references mine the initial history while the entrant is offline. A funding block and its successor then make a signed payment eligible. A CPU worker solves Scary’s job. The live driver asks both Teranodes for the winning block, compares the bytes and waits for a successor. The offline verifier prints the solved hash, height and fee after checking the recorded headers, Merkle commitments, coinbase and peer body digests. Teardown removes this lab’s containers, networks and volumes. The receipt stays in `.regtest/`. [Lab procedure](docs/regtest.md)

## Listen on mainnet

A native build needs Rust 1.98.1 with rustup, Git, curl, CMake, Make, a C++20 compiler, Python 3 and Perl with shasum. Supported native build targets are Linux x86-64, Linux ARM64 and Apple Silicon. Run these commands from the checkout.

```sh
./scripts/build-bdk.sh
rustup run 1.98.1 cargo build --release --locked
./target/release/scary-node
```

From a second terminal, run `curl -fsS http://127.0.0.1:8080/status` to inspect peer connections and the selected tip. Discovery and the next block announcement can take time. Stop with Ctrl-C. Add `--observe-bodies` to retrieve blocks and build witnessed state. Give different identities their own `--data-dir`. Use `--help` for static peers and network options. [Operator guide](docs/operations.md) · [Native build and provenance](docs/chronicle-qualification.md#build-the-interpreter)

## Connect a worker or explore the evidence

- **ASIC or pool operator.** Configure the authenticated alert receiver, payout locking script, worker credential and reachable read-only Asset endpoint. The operator guide gives the node command, ASIC settings and readiness checks. [Connect a Stratum worker](docs/operations.md#connect-a-stratum-worker)
- **Protocol or consensus engineer.** Follow a payment through admission, block assembly, a short reorg and settlement. Compare funded Chronicle transactions and recovered assets with the pinned reference implementations. [Guide to the evidence](docs/README.md)
- **Contributor.** Report a reproducible discrepancy, improve a guide or submit a patch. The contributor guide lists the fast checks, source layout and information that helps others reproduce a result. [Contributing](CONTRIBUTING.md) · [Open BSV License](LICENSE)

**This is alpha-quality proof-of-concept software.**
