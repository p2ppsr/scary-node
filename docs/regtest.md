# Two-Teranode regtest proof

Run from the repository root with Docker running and Compose v2 available. The scripts create a project-scoped lab. The reference nodes use separate chain and UTXO stores. The example uses disposable keys and coins on an isolated BSV regression chain. Run the host driver with Python 3 and an OpenSSL executable supporting secp256k1. Two public fixture peer identities come from [Teranode v0.16.1](https://github.com/bsv-blockchain/teranode/blob/807f1bea39f52797c72fd157a543e8bb64622510/compose/cmd/gennodes/peer_keys.json). The fixed RPC credentials and payout example belong only to this lab. Each run generates a new temporary wallet key locally and removes it after the proof. An ingress proxy forwards six loopback ports to RPC, Asset, admission and Stratum. Both reference nodes and the entrant have only an internal Docker interface.

1. Run `./regtest/up.sh`. The driver checks that both reference services respond and that the entrant has joined both native peers.
2. Run `./regtest/prove.sh > receipt.json`. The driver stops the entrant while the references establish chain history. After restart, it checks that the entrant has observed one body. It offers a signed transaction whose input predates entry and records its rejection.
3. The first reference mines that funding transaction. The driver offers its signed child to Scary Node before the creating block has a successor, records the rejection, and has the second reference mine the successor. Scary Node then admits the identical child. The driver supplies the child only to Scary Node.
4. The Stratum worker receives the job, searches for a nonce, submits it and reconstructs the winning header. Both references must adopt the exact hash and serve the candidate bytes. The driver then mines a successor on a reference and verifies both active tips.
5. Run `python3 regtest/verify_receipt.py receipt.json`. The offline verifier checks the funding outpoint and fee, four headers and their work, transaction Merkle commitments, coinbase reward and both reported body digests. The live driver obtains the acceptance observations from the reference processes.
6. Run `./regtest/down.sh` to remove this lab’s containers, networks and volumes. A clean rerun creates fresh identifiers.

## Verified on November 22, 2026

The empty entrant joined at height 101 with one observed header and one witnessed output. Its operator endpoint declined the signed funding transaction spending a pre-entry coin. Teranode 1 mined that funding transaction at height 102. The entrant declined its 191-byte signed child until Teranode 2 extended the funding block at height 103. A CPU Stratum worker then solved the entrant’s 414-byte block at height 104 with a 1,000-satoshi child fee. Both stock Teranodes selected `2b86ecea75994403772e4913c1484ffd6d8afb6deff03b9af2ccce9a71b13cef` and independently served its exact bytes. Both reference RPCs reported the same height-105 successor. The entrant reported that successor with two connected data peers. [Wire bytes, reference readbacks and release identity](evidence/chronicle-two-teranode-quickstart.json)

The tested Linux ARM64 executable reported SHA-256 `ddfbac3d6b268bb670cfd50d936598b0b97adee828c2d5e8c4a21c58a28f4d0d`, which matched a separate read of the executable inside its running container. It reported linked BDK 1.4.0 and SV Node 1.2.3. The release build used the 27 Dockerfile and runtime input files recorded in the receipt. Temporary private Docker subnets isolated this run from other local projects. Recompute the recorded block, fee, observed tip identities and Git input digests from the checkout:

```sh
python3 regtest/verify_receipt.py --check-source docs/evidence/chronicle-two-teranode-quickstart.json
```

For a local Rust build, run `./scripts/build-bdk.sh` before `cargo test --locked` so Cargo can find the pinned interpreter sources and archive. A short-target CPU worker is adequate for regtest. The ordinary operator path remains native mainnet listening.
