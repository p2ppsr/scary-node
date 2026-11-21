# Recovering work across tip changes

The node holds a witnessed view of one fully observed branch. A competing body whose header and transaction commitment can be checked is buffered. The incumbent remains active at equal work. When buffered bodies connect from an observed ancestor and exceed the incumbent’s accumulated work, the node reverses the detached blocks, validates the replacement in parent order and keeps the resulting branch. An alternate body that fails the contextual checks leaves the earlier verified branch available for recovery. Merkle commitments, contextual difficulty, time, watched spends and amounts are checked using the same paths as for a direct extension.

The journal stores up to eight recent blocks, subject to a 64 MiB body budget and 250,000 total undo entries. It reverses observed coin creations, removals and consumption records, including recent-spend cache eviction. A separate queue holds up to 24 competing or out-of-order bodies within a 64 MiB aggregate budget. A missing parent can arrive after its child. A branch whose ancestor has left the journal stays outside the automatic recovery window. When a credible higher tip is pending, the node withholds new jobs until it obtains a usable branch. An already admitted payment stays pending if its inputs remain eligible on the replacement branch. A wallet can resubmit a detached payment after its input becomes eligible again. The view is in memory. Restarting establishes a new observation boundary.

## Stratum template lifetime

A same-parent update issues a fresh job with `clean_jobs=false`. Each connection retains up to eight jobs for the current parent. A late target share can solve any retained job against the transaction bytes and coinbase that job originally committed to. Template data shared between jobs keeps the transaction buffers bounded by observed admission. A parent change or unavailable current work invalidates affected jobs. If the server has no replacement, it closes a connection whose worker holds retired work.

The node tracks solutions by header hash under `GET /status`, at `mining.solutions`. It records the original job, parent, queue timestamp, publication outcome and explicitly reported peer tips. It marks a solution observed active when its verified body joins the local branch, and settled when it has two observed successors. A competing branch at that depth gives `stale_at_depth`. A later branch switch can revise an earlier observation. These are local records. The regtest driver checks the recorded settled ancestry separately on full validators. The node retains the latest 256 solutions and up to 16 local state transitions per solution.

## Reproduce the live proof

The dedicated reliability project uses ports 19733, 19741, 19742, 19751, 19752 and 19780 on loopback. The worker, test wallet and both full-state Teranodes run the ordinary regtest workflow. The script starts from fresh dedicated volumes. It creates two witnessed outputs, starts work spending one, offers the other after that job is issued, and submits the old job. It then proves the short branch change using separate, temporary test miners with the same prefix. [Proof results and receipts](reliability-results.md)

```sh
./regtest/reliability.sh > reliability-proof.json
python3 regtest/verify_reliability.py proof reliability-proof.json
./regtest/reliability.sh --down
python3 regtest/verify_reliability.py proof docs/evidence/reliability-proof-repeat.json.gz
```

## Reproduce the enlarged control

Run eight fresh 128-opportunity sessions with seeds 2701 through 2708. Both roles see the same signed payment and parent, and each performs one SHA256d attempt per logical tick. Every discovered body goes to the same independently validating SV Node through one loopback relay. The selected block is published through its miner’s normal interface and extended by two Teranodes. The driver reads each locally solved Rust block back before resetting each session. Incomplete runs leave their logs and available receipts. The order and count of completed sessions are part of the published verification. [Race methodology](races.md)

```sh
./regtest/reliability-sample.sh "$PWD/reliability-sample"
python3 regtest/verify_reliability.py pack reliability-sample > reliability-cohort.json
python3 regtest/verify_reliability.py cohort reliability-cohort.json
python3 regtest/verify_reliability.py cohort docs/evidence/reliability-cohort.json.gz
```

The precision target is about six percentage points of half-width for the per-discovery comparison, based on four times the original sample and its observed variability. This is a precision target for the existing local relay experiment. An equivalence margin would specify how much extra stale risk is operationally acceptable. Neither the sample size nor the test protocol addresses production race incidence, availability against independently varying native peers or the effect of an identical template engine using different state.
