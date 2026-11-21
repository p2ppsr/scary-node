# Passive mainnet observation

The checkpoint covers **90 consecutive blocks, 976422 through 976511**, observed from November 21, 2026 at 02:23:34 to 18:25:17 UTC. The observer started collecting contemporary bodies with an empty witnessed set. The 1,008-block qualification is in progress. Every decision in this checkpoint withheld work while the 147-header mainnet difficulty window filled. The observer ran with shadow assembly enabled and its Stratum listener disabled. [Machine-readable results](evidence/chronicle-shadow-checkpoint.json) · [Compressed observations and reference readbacks](evidence/chronicle-shadow-journal.json.gz)

| Measurement | Checkpoint result |
| --- | --- |
| Consecutive observed tips | 90 with no discontinuity |
| Header proof of work | 87 locally recorded wire headers and three independently retrieved early headers |
| Absolute cumulative chainwork | Sum of all 90 work increments plus the external parent anchor matched the external tip exactly |
| Time and difficulty | 76 complete median-time-past comparisons. Independent CW-144 forecasts become possible after 147 full wire headers |
| Alert readiness | 86 tip decisions had a connected receiver, verified genesis, matching applied, verified and reported sequence 1, and a native peer sync within 20 minutes. Four earlier tip decisions had unavailable directive state |
| Unsigned candidates | Zero. All 90 decisions lacked enough headers to calculate the next difficulty |
| Peer corroboration | 88 tips had at least 11 distinct peer identifiers report the same hash and height within two hours. One additional tip had a matching cached status |
| Recovery | Seven process starts restored the immediately preceding journal hash. A controlled restart restricted block delivery to SV P2P and received the direct successor at height 976425 |

At height 976425, an Asset request for the already committed hash failed because its advertised base was a relative URL. The observer retained that tip. It recorded height 976426 as its direct successor 152 seconds after logging the failure. The checkpoint preserves this original incident. The fetch handler now checks the requested hash against the committed branch while holding the state lock. Failures for hashes already covered leave work readiness intact. A failure for a missing hash, or an unidentified incomplete SV body, still suspends work.

A subsequent release restart at height 976512 restored the same 91 headers, 30,128 witnessed outputs, absolute observation boundary, cumulative window work and applied signed sequence 1. Data peers then reconfirmed the tip. The receiver had two active peers and zero unprocessed alerts. The in-memory pending transaction count fell from 94 to 53 across this restart. Current native transaction arrivals can follow the last durable block checkpoint. The receipt identifies the exact release binary by SHA-256. [Release restart and reconfirmation](evidence/chronicle-shadow-release-restart.json)

At height 976516 the observer upgraded to commit `ddd82d3`. The release's stricter restore checks reopened the same 95-header window, 29,364 witnessed outputs, accumulated work and applied alert sequence. The operator API first reported that current-tip confirmation was outstanding. Two data peers reattached and confirmed the tip. Difficulty history was the remaining readiness condition. The receiver independently reported sequence 1, two alert peers and zero unprocessed alerts. [Measured checkpoint restore on the released binary](evidence/chronicle-shadow-checkpoint-restore.json)

The native transaction-intake release at `af9b605` subsequently reopened the observation boundary at height 976518, restored 97 consecutive headers and 31,369 witnessed outputs, and recovered the same accumulated work and signed sequence. Four data peers reconfirmed the tip. The mainnet process then resumed collecting bodies and native transaction gossip. The receiver had two alert peers and zero outstanding messages. The release was compiled from the committed source archive into a fresh macOS Cargo target directory. [Restart receipt](evidence/chronicle-shadow-native-intake-restart.json) · [Exact running build](evidence/chronicle-qualification-macos-release.json)

## Replay the checkpoint

Run from the repository root with Python 3. The replay reads only the bundled evidence.

```sh
python3 qualification/shadow_checkpoint.py replay docs/evidence/chronicle-shadow-journal.json.gz \
  | diff - docs/evidence/chronicle-shadow-checkpoint.json
python3 -m unittest discover -s qualification -p test_shadow_checkpoint.py
```

The gzip archive includes run-local peer labels, tip decisions, timed peer reports, the failover receipt and public header readbacks. The replay recomputes header hashes, targets, proof of work, accumulated work and median time. For every sufficiently long wire-header window it independently calculates the exact next CW-144 target from per-block work and suitable-block timestamps, compares the node's forecast, then compares the successor when it arrives. For unsigned templates it checks the committed parent, height, time, coinbase height, target and transaction-ID Merkle root. Peer identifiers measure observed reports and can share an upstream miner. This journal records transaction identifiers and cannot independently replay transaction scripts. [Pinned SV Node difficulty implementation](https://github.com/bitcoin-sv/bitcoin-sv/blob/6504a3aff65ba97c0f6c80962b033e35ecbfed4b/src/pow.cpp#L177)

The public chainwork comparison uses an external header for height 976421 as its starting value. At each subsequent height the replay adds `floor(2^256 / (target + 1))`. It verifies the final total and tip hash against the recorded WhatsOnChain response, as well as each intermediate external readback. The three earliest tip records predate local wire-header capture. The archive includes their independently retrieved bytes as retrospective evidence. Their heights remain excluded from the count of locally captured wire headers. [External parent header](https://api.whatsonchain.com/v1/bsv/main/block/00000000000000001076543ba0f684404e1b03b04a38403e531db3f381dced9a/header) · [External checkpoint header](https://api.whatsonchain.com/v1/bsv/main/block/000000000000000000aba8e7b99e254343964efe2347596967945d1aa4ee91c2/header)

## Collect the 1,008-block run

Start the passive observer, maintained Alert System receiver, authenticated gateway and controlled failover driver as described in the [qualification runbook](chronicle-qualification.md). While the observer is running, the following command reads its append-only journal and emits the final evidence bundle once it has 1,008 contiguous observed decisions, independently replayable unsigned work, a recovered process boundary and an observed SV-only delivery. Review the output's gaps, readiness losses, forecasts, corroboration and candidate count before making a qualification claim. The live observer alone selects candidates and peers. Readbacks use a separate process and supply no history or state to the node.

```sh
python3 qualification/shadow_checkpoint.py follow --reference \
  "$SHADOW_LOG" "$FAILOVER_RECEIPT" .native/shadow-final.json.gz \
  > .native/shadow-final-summary.json
```

Use `capture` in place of `follow` to take an immediate checkpoint. Each published evidence bundle is self-contained and the SHA-256 digest in the summary identifies its exact bytes.

A final bundle requires at least one transaction-bearing unsigned candidate built with an independently recomputed CW-144 target and a subsequent observed mainnet header with the predicted target. The active linked run must also contain the recovered failover, sufficient observed body coverage, a ready signed prefix at the endpoint and a recorded template wherever the node reported ready with pending transactions. Each height needs a timed report from another peer or a checked public header. When an Asset endpoint cannot be associated with a peer identity, the verifier uses two reporting peer identifiers or a public header. The public work anchor and final selected hash must reconcile independently.

Run `python3 qualification/shadow_checkpoint.py qualify .native/shadow-final.json.gz` to check these conditions offline. An incomplete checkpoint returns the outstanding evidence and exit status 1. Ordinary `replay` continues to reproduce the published 90-block checkpoint exactly. The collector keeps observing after the first 1,008 tips if a condition is outstanding. A transaction-bearing candidate on the last tip needs another observed block. A peer-report gap can be checked with a public header at that hash. A reference outage or a reference reporting an inactive hash causes the collector to read the journal again and retry. Reference readbacks add zero prospective tip observations and cannot fill a missing local decision.

A missing ready template, deficient recorded body coverage, a lost restore boundary or a recorded incorrect prediction starts a new qualifying interval. The final archive retains the preceding records, excluded count and reasons. An independently computed difficulty or cryptographic contradiction fails replay and requires investigation. A fresh interval needs 1,008 further contiguous local tip decisions and its own successful transport drill. Run the failover driver with a fresh receipt against the same durable state and shadow journal. Append that attempt’s completed receipt to the collector’s existing receipt file in order, retaining failed and partial attempts. The verifier matches all three stages to one drill and the corresponding journal transitions. The offline collector regression extends the archived 90-block prefix with simulated events, exercises a delayed successor and a reference outage, and verifies that only local observations increase the block count.

## Compare unsigned choices with transactions mined by an independent miner

For the first 16 nonempty unsigned templates on the linked series in a checkpoint, compare the ordinary transaction IDs Scary chose at its parent with every transaction in the independently mined immediate successor. This selection rule was recorded before the mainnet observer reached its first 147-header difficulty window. The numerator counts candidate entries included by the next miner. The denominator counts all candidate entries for those 16 parents. A payment appearing in templates at two different heights contributes two opportunities. A zero overlap is a measured result. An unfinished sample keeps its observed denominator.

The reference reader requests public block hashes only. For paginated blocks it combines the initial transaction IDs and the advertised pages in order, checking the full transaction count. The offline verifier reconstructs the complete Merkle root against the locally observed successor header and binds the report to the exact checkpoint archive. Other miners choose their own transactions, fee policies and arrival times. Absence from a successor therefore gives no verdict on the payment’s validity. [Reference block API](https://docs.taal.com/core-products/whatsonchain/block)

```sh
python3 qualification/shadow_inclusion.py capture .native/shadow-final.json.gz .native/shadow-successors.json.gz
python3 qualification/shadow_inclusion.py verify .native/shadow-final.json.gz .native/shadow-successors.json.gz
```

The published 90-block entry checkpoint yields zero candidate-bearing pairs. It predates the first unsigned template. Two independent reference reads returned all 668 transactions at height 976467 and all four at height 976511. Both sets reconstruct the locally observed header commitments. The regression test uses a synthetic transaction choice at the second height to check the counting procedure. [Public reference readbacks](evidence/chronicle-shadow-reference-transactions.json.gz)
