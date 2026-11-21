# Measuring stale blocks under matched work and relay

Does a miner using only witnessed inputs lose more valid blocks when it races a full-state miner on the same parent? This lab compares Scary Node’s real Stratum template with a Teranode full-state mining candidate. Both select the same signed payment and pay the same transaction fee. An independent SV Node receives their discoveries through a common relay and applies its ordinary block validation and chain selection.

## Experiment specified before the measured runs

Run two fresh regtest sessions with workload seeds 1701 and 1702 and **128 opportunities per session**. Include every planned opportunity. A short engineering run checks the apparatus before these sessions. Both sources must be ready on the same active parent and have admitted the payment. The measurement therefore addresses the risk to a ready miner whose input-selection decision agrees with the reference for this payment. The input’s creating block and a successor are visible to Rust. The Teranode maintains its chain and UTXO state from genesis. The full-state producer alternates between the two Teranodes. The candidates can differ in coinbase bytes and header fields. The receipt preserves those differences.

A CPU worker performs one SHA256d header attempt for each miner per logical tick at the identical regtest target. The first tick with a solution ends the opportunity. If both solve on that tick, both enter the same relay window. If one solves, the opportunity contributes a single discovery. The receipt retains the header prefixes, nonce assignments and attempts so a verifier can repeat every hash. Equal hashpower here means identical counted trials per tick. Wall-clock scheduling of these tiny CPU operations is outside that definition.

At this easy target each hash has nearly a one-half chance to qualify. With independent hashes, the probability both qualify on the first successful tick is about one third because one quarter of ticks have two discoveries and three quarters have at least one. That supplies repeated races in a bounded sample. If the two roles also split those races evenly, each miner would lose about one quarter of its discovered blocks in this particular model.

All discoveries receive a validity check against the parent through SV Node’s proposal RPC. Their complete wire blocks then go through the same loopback port, proxy, Docker bridge and ordinary submitblock endpoint. For a tie, two client workers rendezvous before dispatch. Role assignment to submission order is determined before discovery. The proxy reports the request’s elapsed time and its current time using a common millisecond-resolution clock. Subtracting the two estimates when the request’s first bytes arrived. The driver also times dispatch and acknowledgement at the client. Equal paths can still have unequal arrival times and processing costs. The analysis reports these differences.

The judge selects its active block using its actual chain. The corresponding miner submits its winning solution to its normal mining interface, and two subsequent Teranode-mined blocks extend the winner. The driver checks the hashes on the active chain of both Teranodes and the judge. The SV Node adjudicates the tie. The two Teranodes corroborate the selected branch during settlement. They receive the selected native solution after arbitration, so their agreement supplies two independent chain records of settlement and one shared tie decision. The other member of a tie counts as stale only when it was a valid solution, the judge received it, and the recorded ancestry through those two successors excludes it. A failed, unvalidated or missing submission makes the run incomplete.

## Denominators and uncertainty

- **Per discovered block.** For each miner divide its stale blocks by its on-target discoveries, including single discoveries. Compare the two percentages in percentage points. Each opportunity supplies the pair of miner outcomes, so uncertainty estimates resample whole opportunities within each session.
- **Given simultaneous discovery.** Divide each miner’s losses by the number of two-solution ticks. Exactly one of the two valid siblings can appear on the agreed branch. Transform the exact binomial interval for the Scary Node loss proportion into an interval for the paired stale-rate difference. The interval assumes repeatable, independent tie outcomes under the modeled conditions.
- **Observed propagation.** Report tie counts by dispatch order and by measured arrival order. Differences smaller than the proxy clock’s resolution appear as ties. Inspect solo discoveries, candidate byte sizes, hash counts and the two individual sessions alongside the pooled result.

The planned analysis uses two-sided 95% intervals and a signed difference of Scary percentage minus full-state percentage. No equivalence margin was chosen. Exact equality is not identifiable from a finite sample. The regtest target and logical tick width determine how often solutions coincide. This setup tests the selection claim at matched parent, work and delivery geometry. Ordinary native fan-out, propagation across a geographically distributed network, time spent waiting for a job and the frequency of real mainnet races require separate measurements.

## Reproduce the experiment

Use Docker Compose v2.24.4 or newer, Python 3 and OpenSSL with secp256k1 support. Docker must support linux/amd64 for the pinned SV Node 1.2.3 image. The two Teranode services use v0.16.1. The race driver records the images and Rust binary it actually ran, plus digests of the worker, driver, relay and Compose configuration. Use the published commit to verify its archived runs. The lab uses its own Compose project and loopback ports 19633, 19641, 19642, 19651, 19652, 19661 and 19680. Run from the repository root.

```sh
./regtest/races.sh --seed 1701 --trials 128 > races-1701.json
./regtest/races.sh --down
./regtest/races.sh --seed 1702 --trials 128 > races-1702.json
./regtest/races.sh --down
```

A fresh run creates a new wallet key and new coinbases, signatures, headers and transport timings. A seed fixes the experimental assignments. Preserve stdout and stderr if a run fails. Inspect `docker compose -p scary-node-races -f regtest/compose.yaml -f regtest/races.compose.yaml logs` before cleanup. The scoped teardown removes its disposable state.

## Replay the evidence

The verifier accepts either the JSON produced by the runner or the published gzip receipts. It recalculates hashes and signatures from serialized blocks, replays each nonce attempt and derives the active ancestor from the receipts through two successors.

```sh
python3 regtest/verify_races.py docs/evidence/races-1701.json.gz docs/evidence/races-1702.json.gz
python3 regtest/verify_races.py docs/evidence/races-1701.json.gz docs/evidence/races-1702.json.gz --json > race-summary.json
uv run --isolated --no-project --python 3.13 --with matplotlib==3.10.8 \
  python regtest/plot_races.py race-summary.json race-results.png
```

Run the worker and analysis regressions with `python3 -m unittest discover -s regtest -p "test*rac*.py"`. Run `python3 regtest/test_verify_races.py docs/evidence/races-1701.json.gz` to test altered copies of a saved receipt. Only the figure requires Matplotlib. The primary uncertainty calculation resamples 10,000 complete opportunity pairs within the original session. The conditional interval uses the equal-tailed Clopper–Pearson method. [NIST exact binomial limits](https://www.itl.nist.gov/div898/software/dataplot/refman2/auxillar/exacbici.htm) · [NGINX request timing definitions](https://nginx.org/en/docs/http/ngx_http_core_module.html#var_request_time) · [Pinned Teranode mining RPC](https://github.com/bsv-blockchain/teranode/blob/807f1bea39f52797c72fd157a543e8bb64622510/services/rpc/handlers.go)

An engineering pilot exposed a distinction in the judge’s RPCs. One losing sibling returned `inconclusive` from submitblock although its proposal passed, the judge served its exact bytes, and getchaintips listed it as a fully validated fork. SV Node uses that acknowledgement when its per-call validation callback has no verdict. The driver records the raw acknowledgement, the separately observed branch status and the canonical ancestry. The prespecified side-branch rule also permits a stored branch indexed as `valid-headers` when its complete body passed the common-parent proposal check before submission and the independent verifier checks its work, input signature, source amount and fee. The report counts that status separately from `valid-fork`. A bare inconclusive acknowledgement supplies no verdict. The verifier checks proof of work for every discovery because proposal mode leaves that check to the submit path. [SV Node proposal and submitblock handlers](https://github.com/bitcoin-sv/bitcoin-sv/blob/v1.2.3/src/rpc/mining.cpp) · [SV Node branch status definitions](https://github.com/bitcoin-sv/bitcoin-sv/blob/v1.2.3/src/rpc/blockchain.cpp)
