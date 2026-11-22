# Where a workload worker’s reply and a block’s publication were observed

The reordered regtest produced 56 distinct Scary Node network-target solutions. The original entrant’s saved journal contains all 56. Three independent full-node body reads place 30 on the final branch. The worker received a positive reply for 26 of those 30. Its connection timed out on the other four. Conversely, slot 140 returned a positive worker reply for a solution that had no positive Teranode publication receipt, SV delivery receipt or final-chain inclusion in the captured records. A worker reply establishes what the worker heard. The selected header and its descendants establish what the network included. [Closed workload receipt](evidence/chronicle-sustained-reordered.json.gz) · [Original solved journal](evidence/chronicle-sustained-reordered-ledger.json)

## Clocks and observables

The worker recorded the start and end of each opportunity with the host monotonic clock. This interval includes its status query, connection, Stratum setup, regtest proof of work and submission handling. The primary receipt also timestamps each successful poll of a stock tip on that same clock. A poll can skip a short-lived tip. The original worker recorded no timestamp for the start of the submit call itself.

Inside the measured node, `queued_at_ms` records the completed candidate checks before the solution checkpoint write. A successful native Teranode block announcement queues an event. Its handler reacquires the mining lock, records `published_at_ms`, saves the checkpoint, updates peer status and emits the `gossip_published` log line. The log timestamp therefore includes work after the local publish call returned. The SV `sent_body` receipt is also written by an event handler after the transport sent a complete body. Neither receipt directly timestamps the first packet arriving at a stock peer. [Exact measured Stratum code](https://github.com/p2ppsr/scary-node/blob/af9b605/src/mining/stratum.rs#L314) · [Teranode publisher](https://github.com/p2ppsr/scary-node/blob/af9b605/src/peer.rs#L312) · [Receipt and log order](https://github.com/p2ppsr/scary-node/blob/af9b605/src/main.rs#L449)

| Solutions | Count | Positive Teranode log | Median worker slot | Median local queue to positive receipt | Median positive receipt to first log |
|---|---:|---:|---:|---:|---:|
| Acknowledged primary, on final branch | 26 | 26 | 2.952 s | 2.416 s | 1.962 s |
| Acknowledged primary, outside final branch | 25 | 24 | 3.705 s | 3.845 s | 2.156 s |
| Selected after worker error, including drain | 4 | 4 | 7.379 s | 5.192 s | 2.278 s |
| All local solutions, including drain | 56 | 55 | 3.590 s | 3.147 s | 2.123 s |

Each median uses only rows with both endpoints. Docker retained 79 positive log lines for 55 distinct block hashes, including repeats. Forty solutions had an SV `sent_body` receipt. The median queue-to-recorded-SV-send interval for those 40 was 10.613 s. Two acknowledged primary solutions outside the terminal branch had earlier local inclusion observations. The measured differences across these groups are descriptive. The workload assigned different miners different slots and did not independently fix competing block arrival times. The separate matched-ingress study supplies the control for the stale-risk claim. [Original positive log lines](evidence/chronicle-sustained-reordered-gossip.txt) · [Matched propagation experiment](propagation-control.md)

## Four selected blocks with missing worker responses

| Worker slot | Height | Payments | Worker error to first sampled stock tip | Worker error to logged native publication |
|---:|---:|---:|---:|---:|
| 124 | 524 | 64 | 3.050 s | 9.214 s |
| 204 | 550 | 1 | 6.872 s | 5.794 s |
| 269 | 568 | 10 | 3.078 s | 2.397 s |
| 300, drain | 577 | 128 | No tip poll captured this hash | 6.871 s |

The first interval uses the earliest successful tip observation across the three stock nodes and the worker error on the same host clock. The last column aligns Docker UTC with the host run’s original wall-clock anchor. This experiment retained no separate clock-offset bounds for that comparison. At height 524 the recorded times place two Teranode active-tip reports before the entrant’s logged publication confirmation. At height 577 all three independently returned the block bytes on the final branch although no sampled tip contained its hash. These records support attribution and locate delays in observing results. They do not isolate time spent inside checkpoint serialization, disk sync, network transfer or reference-node validation.

The joint capacity experiment still uses the registered full denominator through two successors. The cold and release runs now retain exact prepared headers, separate worker phases and each independent stock body-fetch interval. A separate live regtest forwarded the same submission to the node and closed only the worker response path. Stock independently selected and extended that solution. [Clock definitions, independent worker test and reproduction](cold-miner-registration.md#attributing-an-accepted-block-when-a-worker-misses-the-reply)

## Reproduce every timing row

```sh
.native/py/bin/python qualification/workload_publication.py \
  docs/evidence/chronicle-sustained-reordered.json.gz \
  docs/evidence/chronicle-sustained-reordered-gossip.txt \
  docs/evidence/chronicle-sustained-reordered-ledger.json
```

The verifier joins the immutable worker and stock records to the checksum-verified journal by block hash. For an original timeout with no saved worker hash, the selected journal entry must match exactly one errored opportunity on that parent before a slot time is attributed. The output preserves every local solution, all available timing endpoints and the number contributing to each distribution.
