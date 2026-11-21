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

A finite collector can save each independently read successor as soon as the observed pair exists. It verifies the full ordered transaction list against the observed header before writing it to the local cache. A later outage or a collector restart can then retry the missing block without downloading previously verified blocks again. `progress.json` lists outstanding heights and `attempts.jsonl` retains failed and successful readbacks. The collector rereads the live journal before fixing the final sample and replays every cached list against its selected header.

```sh
python3 qualification/shadow_inclusion_follow.py "$SHADOW_LOG" "$FAILOVER_RECEIPT" "$PRIVATE_SUCCESSOR_DIR"
python3 qualification/shadow_inclusion.py verify \
  "$PRIVATE_SUCCESSOR_DIR/checkpoint.json.gz" "$PRIVATE_SUCCESSOR_DIR/successors.json.gz"
```

The published 90-block entry checkpoint yields zero candidate-bearing pairs. It predates the first unsigned template. Two independent reference reads returned all 668 transactions at height 976467 and all four at height 976511. Both sets reconstruct the locally observed header commitments. The regression test uses a synthetic transaction choice at the second height to check the counting procedure. [Public reference readbacks](evidence/chronicle-shadow-reference-transactions.json.gz)


The November 21 passive upgrade at height **976520** recovered the same selected hash, 99 contiguous headers from 976422, 30,553 witnessed outputs and durable directive sequence 1. Two data peers were connected when tip reconfirmation was recorded. The signed receiver reported two alert peers and zero outstanding actions. These 99 observations remain a prefix of the 1,008-block acceptance interval. [Restart and reconfirmation](evidence/chronicle-shadow-recovery-upgrade.json) · [macOS release provenance](evidence/chronicle-recovery-macos-build.json) · [Linux build from the same source](evidence/chronicle-recovery-linux-build.json)

At height **976525**, the entrant had 104 consecutive local headers, 31,339 witnessed outputs and 111 pending transactions. Since the height-976520 restart, native admission counted 314 accepted SV-peer offers and 33 accepted Teranode subtree offers. Both paths also delivered repeats and ineligible transactions. Five SV network groups corroborated local time within one second. The signed directive prefix was current. The 147-header difficulty window was still incomplete, and the node recorded no unsigned candidate or local solution at that parent. An independent readback matched the local header, parent and increment in chainwork. Admission counters measure events received through each transport. A transaction can appear more than once. [Mainnet observation and reference headers](evidence/chronicle-mainnet-native-offers.json)

## Independently replay the first unsigned candidate's fees

The native evidence reader selects the **first transaction-bearing unsigned decision** in the prospectively written station journal. It verifies the 147 linked proof-of-work headers, computes the next CW-144 target, checks the candidate's parent, median time, coinbase height and Merkle root, then compares the very next observed header with the selected parent, height and target. It also checks the signed-prefix status at the decision and samples the running station's clock report for the same parent. The time check requires a corroborated offset from at least three SV network groups and a sample within 120 seconds of the decision.

For each ordinary input the reader retains the exact funding outpoint, value and locking script. It checks funding transaction bytes and their Merkle branches against the selected chain, and resolves outputs from earlier transactions in the same template in order. It compares every independently calculated fee with the node's admitted fee and reconciles the sum plus subsidy with the coinbase payout. Coinbase and confiscation outputs have independent age checks. The source proofs establish creation and value. The station’s witnessed spend journal and shared BDK supply spend and script validation. The signed-prefix check reads the node’s receipt. Full signed-message verification is exercised in the [receiver and DAR qualification](chronicle-results.md).

Start the reader before the first eligible decision, using a private directory on the same filesystem as the node state. The live decoder requires Python and MessagePack. The verifier for an existing receipt uses the Python standard library.

```sh
python3 -m venv .native/shadow-reader
.native/shadow-reader/bin/pip install 'msgpack==1.1.2'
.native/shadow-reader/bin/python qualification/shadow_first_candidate.py follow \
  "$SHADOW_LOG" "$DATA_DIR/mainnet/witness.state" "$PRIVATE_EVIDENCE" \
  --status-url "$LOCAL_API" --token-file "$OPERATOR_TOKEN_FILE" \
  --asset-list "$APPROVED_ASSET_URL_FILE"
python3 qualification/shadow_first_candidate.py verify "$PRIVATE_EVIDENCE/first.json.gz"
```

Each line of the optional Asset file is an operator-selected native Asset base URL, including the API prefix. The reader requests a previously witnessed, confirmed block by hash when its body has left the local evidence cache. The node receives no chain data from the reader. The reader retains up to four immutable checkpoint links, the first candidate's exact bytes and funding proofs. Its compressed body cache defaults to 2 GiB. The `--cache-bytes` and `--max-block-bytes` options set archive and remote read limits. A missing source or clock sample leaves the public count incomplete. A conflicting immediate successor is reported against the original decision. Keep the candidate receipt private while its signed payments are unconfirmed. The summary contains counts, fees, difficulty and the confirmed parent hash.

At height **976529**, the station had observed 108 consecutive contemporary tips, zero unsigned candidates and 68 pending payments in that tip decision. A separate preflight used a confirmed transaction at height **976522**. Its single input came from **976521**, and the difference between that funding output and the spending outputs was **64 satoshis**. Both raw transactions have Merkle paths under their respective proof-of-work headers. A subsequent native Asset read of the funding block reproduced the same proof. The first passive candidate and the 1,008-block qualification remain open at this checkpoint. [Confirmed fee and inclusion evidence](evidence/chronicle-shadow-native-fee-warmup.json.gz)

```sh
python3 qualification/shadow_native_warmup.py docs/evidence/chronicle-shadow-native-fee-warmup.json.gz
python3 -m unittest discover -s qualification -p 'test_shadow*.py'
```

On November 21, the passive observer upgraded to the same successor-aware release that had passed the complete live Chronicle corpus and DAR replay. The protected restart restored the same 110-header range, 32,668 witnessed outputs, cumulative work and applied directive sequence at height 976531. Five data peers reconfirmed the tip within three seconds. The receiver had two synchronized peers and no outstanding actions. The restored snapshot contained 125 eligible pending transactions, down from 335 in memory just before the stop. The last disk snapshot predates some peer admissions. The independently reconciled chain boundary survived. [Restart receipt](evidence/chronicle-waiting-mainnet-restart.json) · [Release validation](chronicle-results.md)

### Cross-check the first candidate on macOS and Linux ARM64

Wait for the native reader to report a complete receipt for the first candidate, its observed successor and its funding proofs. The macOS command below runs the distributed and exact-source GoBDK oracles on that same capture. On a Linux ARM64 Docker host, place the identical private receipt and paired macOS result in the private evidence directory. Supply the four Linux binaries and corresponding manifest built for the selected release image. The Linux oracles receive transactions over standard input inside a read-only container with external networking disabled. The container sees only the mounted oracle directory. [Building the two native variants](chronicle-qualification.md#compare-distributed-and-freshly-compiled-gobdk)

```sh
umask 077
python3 qualification/shadow_bdk.py "$PRIVATE_EVIDENCE/first.json.gz" \
  --go-oracles .native/go-oracles > "$PRIVATE_EVIDENCE/first-go-result.json"
python3 qualification/linux_shadow_candidate.py \
  "$PRIVATE_EVIDENCE/first.json.gz" "$PRIVATE_EVIDENCE/first-go-result.json" \
  .native/linux-arm64-go-oracles "$PRIVATE_EVIDENCE/linux-summary.json" \
  --journal "$SHADOW_LOG"
```

The summary contains the committed receipt digest, confirmed parent and successor, fees, transaction counts, interpreter identities, agreement and ordinary validation outcomes. The journal identifies the originating station executable separately from the Linux image used for the independent replay. The driver compares policy and consensus outcomes and individual script-rejection details between platforms, then counts confiscations separately. The optional `--private-decisions` path retains detailed results locally if investigation is required. An incomplete candidate, mismatched receipt or unrecognized executable fails verification.

## Recover eligible payments across a controlled stop

The Linux ARM64 release restored an immutable copy of the live macOS checkpoint at height **976534**. It opened 113 headers, 33,394 witnessed outputs, signed sequence 1 and all 73 eligible payments on disk. Docker disabled external networking. Both native intake counters stayed at zero, the receiver was disconnected and mining readiness stayed false. This isolates payment recovery from peer retransmission. The checkpoint and its unconfirmed payments stay in operator custody. The public receipt identifies the source image, binary, BDK archive, whole checkpoint, payload, witnessed outputs and eligible transaction set by digest. [Isolated Linux restore](evidence/chronicle-shadow-before-stop-offline.json)

The live passive observer then upgraded at height **976535**, with 114 headers, 36,321 witnessed outputs and 71 eligible payments. It restored the same parent and all 71 payments, and data peers reconfirmed the tip within two seconds. The old process had two additional offers awaiting their funding block’s successor. That queue reopened empty. [Live upgrade and reconfirmation](evidence/chronicle-shadow-shutdown-upgrade.json)

To check an operator’s own checkpoint, keep an immutable link to the atomic state file in a private directory on the same filesystem. Use the qualification Python environment, its pinned SV test framework and a local Linux image built from the release source. The existing regtest controlled-stop fixtures supply public signed payment and block bytes for both signal tests. [Reference builds and lab environment](chronicle-qualification.md) · [Regtest stop and restore](operations.md#stop-and-restore-the-miner)

```sh
umask 077
ln "$DATA_DIR/mainnet/witness.state" "$PRIVATE_EVIDENCE/earlier.state"
.native/py/bin/pip install 'msgpack==1.1.2'
.native/py/bin/python qualification/shadow_restore_boot.py \
  "$PRIVATE_EVIDENCE/earlier.state" "$DATA_DIR/mainnet/alerts" \
  "$LINUX_BUILD_RECEIPT" "$PRIVATE_EVIDENCE/offline-restore.json"
```

To measure the controlled stop, retain the earlier checkpoint, wait for eligible peer admissions on the same parent, send the observer SIGTERM and wait for it to exit. Link the resulting `witness.state` as `closed.state` before relaunching. Compare the two immutable files, run the isolated Linux restore on the closed file and compare its count with the live process after restart. If a new block or directive changed the boundary during the stop, record that event and assess it separately. The comparison verifies each signed transaction ID against its stored bytes and requires every earlier payment to appear in the closed pool. The input-set digests use sorted 32-byte transaction hashes in internal byte order.

```sh
.native/py/bin/python qualification/shadow_pending_restore.py \
  "$PRIVATE_EVIDENCE/earlier.state" "$PRIVATE_EVIDENCE/closed.state"
.native/py/bin/python qualification/shadow_restore_boot.py \
  "$PRIVATE_EVIDENCE/closed.state" "$DATA_DIR/mainnet/alerts" \
  "$LINUX_BUILD_RECEIPT" "$PRIVATE_EVIDENCE/closed-offline.json"
```
