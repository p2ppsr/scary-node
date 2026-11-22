# Measuring the Teranode unconfirmed subtree queue

The passive observer reported 5,581 queue-full indications in its first 6,003 subtree announcements after the service restart at height 976547. Independent block intake had retained complete coverage to 976554. A full indication describes one attempted inventory offer. A transaction can appear in more than one subtree, arrive from an SV peer, or arrive later in a block.

## Prospective engineering comparison

Use source `5148c31ae620544c31a61b1710172c4bcc524c54` for the serial reference. Compare that implementation’s 16-slot backlog with a 256-slot serial backlog and a 256-slot backlog with four overlapping subtree fetches. The block-body queue keeps its own original worker and depth. Preserve announcement order when applying verified subtrees so a dependent payment can follow its parent. Keep the three-source limit, verified Merkle roots, per-body size limit, bounded temporary storage and ordinary witnessed admission checks. This study changes inventory retrieval only. The enrolled continuous-wallet releases and mainnet observer retain their frozen binaries.

Use the first 128 signed spends in their original order from the published 4,096-payment mixed-network block. Supply its original funded block at height 107 and a locally mined empty regtest successor before offering inventory. A loopback Asset server returns 64 correct subtree-data bodies containing two consecutive payments each. Offer one distinct root every 20 ms. Each read waits 100 ms before answering, with the first root waiting 400 ms to exercise completion out of order. Record the actual offer, request and response times, admitted roots, full offers, peak concurrent requests, ordered template transaction IDs, complete queue drain time and unchanged selected parent. Root verification and payment admission run inside the actual Rust node code.

Run six trials per treatment under the workstation measurement lock. Rotate treatment order through all six permutations to balance placement. Report all trials. The acceptance conditions for the four-fetch treatment are complete ordered admission of the 128 payments in every trial, peak concurrency between two and four, an unchanged selected tip, and median drain time lower than the 256-slot serial treatment. Report the old queue’s actual omissions and the larger serial queue’s effect separately. A second stock Teranode interoperability run must show a signed payment delivered over actual gossip, mined through Stratum, selected and extended by the stock reference. These measurements concern a bounded signed fixture under specified read delays. They cannot establish unique payment loss or throughput on mainnet. [Signed source blocks and payments](evidence/chronicle-mixed-workload.json.gz) · [Stock Teranode native intake fixture](early-peer-intake.md)

## Reproduce the scheduler trials

Build the shared BDK and Python environment from the [qualification runbook](chronicle-qualification.md) and install the release Rust toolchain 1.98.1. The procedure exports the two source revisions into temporary directories, appends the same test-only probe, and builds an optimized test executable for each. It records the source revisions, injected module and executable digests. Each treatment starts a fresh mining state and an isolated loopback Asset service. The funding body and the 128 signed payment bytes come from the published mixed-network artifact. The deterministic empty successor selects parent `0c67f3ede46fb83e2381df38630735189b8dcf72be131a9bec9a264e4884f152`. [Archived-source harness and verifier](../qualification/subtree_scheduler.py) · [Rust timing probe](../qualification/subtree_scheduler_probe.rs)

```sh
probe_dir=.native/subtree-scheduler-probes
.native/py/bin/python qualification/subtree_scheduler.py build "$probe_dir" reference \
  5148c31ae620544c31a61b1710172c4bcc524c54
.native/py/bin/python qualification/subtree_scheduler.py build "$probe_dir" candidate \
  9901c89644b8f588e73d6e6261f0cffe55ad1f6d
.native/py/bin/python qualification/subtree_scheduler.py run "$probe_dir" \
  .native/subtree-scheduler-trials.json.gz
.native/py/bin/python qualification/subtree_scheduler.py verify \
  .native/subtree-scheduler-trials.json.gz
```

The driver takes the same workstation lock as the other measured workloads. It preserves a partial record before starting each trial and after its completion. The server timestamps each request and the corresponding response becoming ready. The probe records queue drain when the production scheduler removes its final pending root, polling at one-millisecond intervals. The independent verifier compares each resulting unsigned template with the expected signed payment IDs in the order their roots entered the queue. [Original registration](https://github.com/p2ppsr/scary-node/blob/aed924238ab3fd4d9d18ea2c69fd066e5b727a74/docs/subtree-scheduler-study.md) · [Published pre-trial harness](https://github.com/p2ppsr/scary-node/tree/bfd0a36464562f787bc8fcf47113c829be634f20/qualification)

In the separate stock interoperability run, the candidate release received a 191-byte signed payment from Teranode’s native subtree stream, included its 1,000-satoshi fee in the Stratum template, and published the solved block. The stock Teranode selected that block and mined a successor. [Stock adoption and receipt](https://github.com/p2ppsr/scary-node/blob/4d823be50968fc675775f607e6ca76097367d136/docs/early-peer-intake.md#ordered-subtree-fetches-on-a-stock-teranode)

The first attempted launch of the reference probe produced zero stdout bytes and zero completed trials. macOS recorded `SIGKILL (Code Signature Invalid)` during process launch. The same executable also failed to list its tests on that inode. A byte-identical copy on a fresh inode listed them successfully. The corrected installer uses an atomic rename, and the runner lists the required test in both binaries before enrolling a trial. Both executable SHA-256 digests and the signed fixture were retained. The original partial and launch diagnostic are represented in the [startup receipt](evidence/chronicle-subtree-scheduler-startup.json).
