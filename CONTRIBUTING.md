# Contributing to Scary Node

Start with the two-Teranode regtest if you want to follow a signed payment from a wallet to a settled block. The saved receipt is small enough to inspect without building the node. [Quick start](README.md#try-it-on-regtest) · [Evidence by question](docs/README.md)

## Where a change belongs

| Area | Entry point |
| --- | --- |
| Wire parsing, branch selection, witnessed validation, snapshots and Stratum | [Mining modules](src/mining) |
| Native Teranode discovery and Asset fetching | [Peer transport](src/peer.rs) and [Asset modules](src/peer) |
| Contemporary SV TCP peering | [SV transport](src/sv_peer.rs) |
| Signed directives and local receiver adapter | [Directive ledger](src/alerts.rs) and [gateway](scripts/alert-gateway.py) |
| Multi-node examples and their offline verifiers | [Regtest](regtest) |
| Reference comparisons, fault tests and study replay | [Qualification drivers](qualification) |

## Report a discrepancy

Open an issue with the source commit, network, OS and architecture, command, expected result and observed result. For a consensus difference, identify the selected parent, candidate height, reference version and whether the disagreement was in mempool policy or block validation. A small disposable regtest fixture, first differing transaction or decoded receipt is usually more helpful than a full log. Remove real wallet keys, credential files, identifying endpoints and nonpublic payment information before posting. [Project issues](https://github.com/p2ppsr/scary-node/issues)

## Prepare a patch

Build from the repository root on a supported native platform. Use the pinned toolchain and run the checks relevant to your change. The receipt verifier uses Python’s standard library. The full qualification test suite also imports the pinned SV Node source under `.native` and Python dependencies.

```sh
./scripts/build-bdk.sh
rustup component add --toolchain 1.98.1 clippy rustfmt
rustup run 1.98.1 cargo fmt --all -- --check
rustup run 1.98.1 cargo test --release --locked
rustup run 1.98.1 cargo clippy --release --locked --all-targets -- -D warnings
python3 regtest/verify_receipt.py docs/evidence/chronicle-two-teranode-quickstart.json
python3 -m venv .native/py
.native/py/bin/pip install ecdsa==0.19.1 msgpack==1.1.2
.native/py/bin/python -m unittest discover -s qualification
python3 -m unittest discover -s regtest
```

Open a pull request describing the behavior change, why it matters and the shortest way to reproduce it. For fixes, include a regression test exercising the affected public path where practical. Cite the pinned reference revision for changes to validation rules. Identify skipped checks and give the release or commit for any measured result. Preserve original receipts when a later experiment supersedes them. Contributions are offered under the project’s existing license. [Build prerequisites](docs/chronicle-qualification.md#build-the-interpreter) · [Open BSV License](LICENSE)
