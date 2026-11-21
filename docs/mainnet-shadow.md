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
