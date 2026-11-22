# Joining an active regtest chain

This experiment measures how much of a running chain a new miner can learn by following complete blocks after entry. Two Teranodes maintain independent chain and UTXO databases. Four simulated wallets submit real signed payments. Scary Node starts with a fresh identity and volume after the reference chain already contains payment history. The observer used for analysis can replay that history independently.

The published runs show the entrant’s UTXO coverage at entry, each mined block’s size and fees, and how differently two observers assessed the same final block. [Measurements and run receipts](scenario-results.md)

## Run the workload

Use Docker with Compose v2 supporting `!override` (2.24.4 or newer), Python 3, and an OpenSSL executable with secp256k1 support. Run from the repository root. The scenario starts its own isolated Compose project and uses loopback ports 19533, 19541, 19542, 19551, 19552, 19580, and 19581. The small proof in `regtest/prove.sh` uses a separate project. The release was exercised on Linux arm64 containers, Docker Compose 2.30.3-desktop.1, Python 3.14.7 and OpenSSL 4.0.3. The Dockerfile pins Rust 1.98.1, also used for the local Rust tests. [Docker Compose override support](https://docs.docker.com/reference/compose-file/merge/#replace-value)

```sh
./regtest/scenario.sh --seed 7 > scenario-7.json
python3 regtest/verify_scenario.py --expect-admission-order scenario-7.json
python3 regtest/verify_scenario.py scenario-7.json --json > summary-7.json
./regtest/scenario.sh --down
./regtest/scenario.sh --seed 19 > scenario-19.json
python3 regtest/verify_scenario.py --expect-admission-order scenario-19.json
./regtest/scenario.sh --down
```

The launcher writes progress to stderr and the receipt to stdout. It requires fresh scenario volumes. After a failed run, use `docker compose -p scary-node-scenario -f regtest/compose.yaml -f regtest/scenario.compose.yaml logs` to inspect the participants. The `--down` command removes only the scenario containers, networks and volumes. Each run creates new temporary wallet keys. Seeds control the workload choices. Keys, signatures, block times and nonces give reruns different identifiers.

## Verify the shipped records

The current Chronicle seed-7 run records both observers' executable SHA-256 and the payments admitted for each Stratum job. The worker constructs its expected coinbase branch in admission order. The offline check also compares that order with the exact block bytes returned by the two references.

```sh
python3 regtest/verify_scenario.py --expect-admission-order docs/evidence/chronicle-scenario-seed7.json.gz
```

The historical seed-7 and seed-19 runs used an earlier template ordering. Both seeds have compressed copies of their original receipts. The verifier accepts gzip directly and uses Python plus OpenSSL for the independent signature checks. The `--json` option emits a per-height timeline, mining opportunities and the matched observer comparison.

```sh
python3 regtest/verify_scenario.py docs/evidence/scenario-7.json.gz --json > summary-7.json
python3 regtest/verify_scenario.py docs/evidence/scenario-19.json.gz --json > summary-19.json
```

To exercise the local block-level rejection, rollback and partial-check cases, first build the pinned BDK archive with `./scripts/build-bdk.sh`, then run the Rust suite. To check that altered evidence fails verification, run the receipt regression against either seed. It uses in-memory copies of the saved record.

```sh
cargo test --locked
python3 regtest/test_verify_scenario.py docs/evidence/scenario-7.json.gz
```

Two focused Rust tests exercise the matched read-only payment and the block rollback described in the results.

```sh
cargo test --locked matched_parent_read_only_amount_check_depends_on_witnessed_source
cargo test --locked witnessed_amount_violation_rolls_back_prior_transactions_in_that_body
```

To regenerate the figure, install Matplotlib 3.10.8 in a disposable Python environment. With `uv` installed, the following command uses Python 3.13 and an isolated environment. Only figure rendering needs Matplotlib.

```sh
uv run --isolated --no-project --python 3.13 --with matplotlib==3.10.8 \
  python regtest/plot_scenario.py summary-7.json scenario-7.png
```

## Who has which information

| Participant | Data available in the experiment | Decision it makes |
| --- | --- | --- |
| Reference miners | Complete regtest history and spendable state in their own stores | Admit wallet payments, mine their own templates, check Rust blocks, serve active-chain bodies |
| Wallet driver | Keys and outpoints of its simulated participants | Create valid payments and deliver them to the selected operators |
| Rust entrant | Signed native gossip, each retrieved complete body from its entry tip onward, and direct wallet submissions | Track witnessed outputs and consumptions, check the spend conditions it knows, choose eligible transactions, construct the coinbase and issue Stratum work |
| Second Rust observer | A separate fresh identity and volume, two native peers, and bodies starting at the penultimate height | Check the final reference block with a shorter observation history than the original entrant |
| CPU worker | Rust Stratum jobs and their Merkle branches | Find regtest proof of work |
| Offline verifier | Serialized blocks starting at genesis, offers, runtime counters and reported reference receipts | Reconstruct the full chain and the entrant’s narrower view, cross-check measurements and recompute the commitments |

The wallets simulate arrivals between mining events. Every mined block and signature is real regtest data. The height gate in this lab opens after the entry body and twelve successors. It is an operator-selected minimum for mining. Knowledge of each input still depends on whether the entrant witnessed its output and followed every intervening spend on the same branch.

## Reading the measurements

- **History at entry.** Classify pre-entry payment transactions by the number of live outputs at the join tip. Fully spent transactions contribute zero live outputs. Partly spent and wholly unspent transactions contribute at least one. This is a spend-status inventory. The experiment leaves the reference databases intact and measures no disk savings from deleting history.
- **Live-output coverage.** Divide the number of unspent outputs the entrant tracks by the complete-chain UTXO count at the same height. Include immature coinbases in the full count and report them separately. A second ratio uses only outputs locked to the workload wallets. Dormant pre-entry balances can remain invisible for the entire run.
- **Incoming checks.** For each reference-mined block, count input outpoints recognized before consumption, successful Script checks, transactions with enough information to check amounts, and payments whose supported checks were determinate. The separately reported deferred-Script counter covers recognized inputs whose scripts exceed the supported form or budget. Unknown input outpoints have their own counter. An input can be checked in a mixed transaction even while another input is unknown. Knowing an output exists also differs from having a mature input eligible for the next job.
- **Mining opportunities.** Compare the number of offers, local admissions, confirmed deferred payments, serialized block size including the coinbase, and fees across jobs. Each opportunity has the same offered batch size. Account turnover changes how many of those offers the entrant can include. The resulting sizes describe the specified workload.
- **Independent judgment.** Read-only local assessments use the entrant’s actual branch state. A definitely consumed witnessed outpoint supplies evidence for rejection. A never-seen outpoint remains unresolved. Runtime observation rows describe checks performed on the real incoming blocks. Local controls exercise failure paths without sending invalid traffic to the reference peers.

For a matched comparison, the driver starts a second copy of the same Rust executable immediately before the final reference block. It verifies both observers were at the same parent, then reads their actual assessment rows for the identical child block. The body hash, transactions, reference miner and height are held fixed. The mature process serves Stratum and uses the scenario gate. The fresh process is passive and has a different peer identity. Both run the same per-input validation code. The relevant difference for the comparison is which prevouts their observation histories contain.

The native announcement identifies the producer, block and Asset service. The block commits to its transactions. The independent evidence for the producer’s UTXO inventory comes from its local telemetry and replay. The references’ active tips and matching bytes show their ordinary acceptance of the Rust blocks. Participant identities and source URLs remain visible. A receiver’s private beliefs about the producer cannot be measured from these messages.
