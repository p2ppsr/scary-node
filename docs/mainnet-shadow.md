# Passive mainnet observation

**Archived on November 22, 2026.** The project stopped the passive observer, receiver and evidence collectors before reaching the original 1,008-block requirement. The published checkpoints below preserve the observations and can be replayed offline.

The journal through height **976548** contains **127 consecutive selected heights**. After a local process interruption, data peers reconfirmed 976547 and the next observed block directly extended it. There were 624 seconds between peer reports spanning the interruption. Both surrounding blocks passed independent full-wire and Merkle checks. [Interruption timeline and reproduction](shadow-service-interruption.md)

## Restart with payments awaiting endorsement

The original 90-block checkpoint below has been extended. At height **976545**, the waiting-queue release reopened 320 eligible payments and the same selected parent. The next measured graceful stop at **976546** saved **12 native offers awaiting a successor**, totaling 36,013 bytes, and two eligible payments. The earlier atomic file at this same parent contained zero in both pools. The restarted observer reported all 12 waiting and both eligible before any data peer connected. Five peers reconfirmed the parent within two seconds. The maintained receiver reported two peers, signed sequence 1 and zero outstanding actions.

A Linux ARM64 process reopened the same closed checkpoint with its Docker network disabled. Its reported 12 waiting offers, 36,013 waiting bytes, two eligible payments, 125 headers, 38,836 witnessed outputs and directive sequence 1 agreed with macOS. The checkpoint binds the exact order, source and bytes of the saved offers. The isolated process reported its live counts and remained unready for work.

The first successor, height **976547**, contained two transactions. Scary received the 351-byte body over SV P2P. A separate Teranode Asset endpoint returned the identical bytes. The header proof of work, parent and complete Merkle root verified. None of the 12 saved offers was mined in that block. The post-block checkpoint placed **all 12 exact transaction IDs in the eligible pool**, kept the two earlier eligible payments, and contained 22 eligible payments in total. Native gossip continued throughout this mainnet observation. The stock regtest isolates the single-offer case by measuring zero retransmission. The public receipt supplies the successor bytes and aggregate commitments. Rechecking individual unconfirmed payments requires the operator’s private snapshots. [Controlled restart, Linux boot and successor](evidence/chronicle-mainnet-waiting-restart.json) · [Single-offer stock regtests](early-peer-intake.md#restart-before-the-funding-block-has-a-successor)

~~~sh
.native/py/bin/python qualification/shadow_pending_restore.py "$EARLIER_STATE" "$CLOSED_STATE" --awaiting-successor
.native/py/bin/python qualification/shadow_pending_restore.py "$CLOSED_STATE" "$SUCCESSOR_STATE" --successor-body "$SUCCESSOR_WIRE"
~~~

## First 90 consecutive blocks

The checkpoint covers **90 consecutive blocks, 976422 through 976511**, observed from November 21, 2026 at 02:23:34 to 18:25:17 UTC. The observer started collecting contemporary bodies with an empty witnessed set. Every decision in this checkpoint withheld work while the 147-header mainnet difficulty window filled. The observer ran with shadow assembly enabled and its Stratum listener disabled. [Machine-readable results](evidence/chronicle-shadow-checkpoint.json) · [Compressed observations and reference readbacks](evidence/chronicle-shadow-journal.json.gz)

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

## Original qualification criteria

The study required 1,008 consecutive local decisions, corroborated work and difficulty, a ready directive prefix, a recovered transport failover and an independently checked transaction-bearing candidate followed by a successor. The archived interval fell short. The offline `qualify` command lists the missing conditions for each published checkpoint. Reference readbacks add corroboration to an existing journal entry and cannot supply a missing local decision. [Archived collection protocol](https://github.com/p2ppsr/scary-node/blob/3eac2e9/docs/mainnet-shadow.md#collect-the-1008-block-run)

## Compare unsigned choices with transactions mined by an independent miner

The prespecified comparison would count matches between the ordinary transaction IDs Scary chose at its parent and every transaction in the independently mined immediate successor for the first 16 nonempty unsigned templates. This selection rule was recorded before the mainnet observer reached its first 147-header difficulty window. The numerator counts candidate entries included by the next miner. The denominator counts all candidate entries for those 16 parents. A payment appearing in templates at two different heights contributes two opportunities. A zero overlap is a measured result. An unfinished sample keeps its observed denominator.

The reference reader requests public block hashes only. For paginated blocks it combines the initial transaction IDs and the advertised pages in order, checking the full transaction count. The offline verifier reconstructs the complete Merkle root against the locally observed successor header and binds the report to the exact checkpoint archive. Other miners choose their own transactions, fee policies and arrival times. Absence from a successor therefore gives no verdict on the payment’s validity. [Reference block API](https://docs.taal.com/core-products/whatsonchain/block)

The published 90-block entry checkpoint yields zero candidate-bearing pairs. It predates the first unsigned template. Two independent reference reads returned all 668 transactions at height 976467 and all four at height 976511. Both sets reconstruct the locally observed header commitments. The regression test uses a synthetic transaction choice at the second height to check the counting procedure. [Public reference readbacks](evidence/chronicle-shadow-reference-transactions.json.gz)


The November 21 passive upgrade at height **976520** recovered the same selected hash, 99 contiguous headers from 976422, 30,553 witnessed outputs and durable directive sequence 1. Two data peers were connected when tip reconfirmation was recorded. The signed receiver reported two alert peers and zero outstanding actions. The published prefix contained 99 of the required 1,008 observations. [Restart and reconfirmation](evidence/chronicle-shadow-recovery-upgrade.json) · [macOS release provenance](evidence/chronicle-recovery-macos-build.json) · [Linux build from the same source](evidence/chronicle-recovery-linux-build.json)

At height **976525**, the entrant had 104 consecutive local headers, 31,339 witnessed outputs and 111 pending transactions. Since the height-976520 restart, native admission counted 314 accepted SV-peer offers and 33 accepted Teranode subtree offers. Both paths also delivered repeats and ineligible transactions. Five SV network groups corroborated local time within one second. The signed directive prefix was current. The 147-header difficulty window was still incomplete, and the node recorded no unsigned candidate or local solution at that parent. An independent readback matched the local header, parent and increment in chainwork. Admission counters measure events received through each transport. A transaction can appear more than once. [Mainnet observation and reference headers](evidence/chronicle-mainnet-native-offers.json)

## Confirmed fee and inclusion check

At height **976529**, the station had observed 108 consecutive contemporary tips, zero unsigned candidates and 68 pending payments in that tip decision. A separate preflight used a confirmed transaction at height **976522**. Its single input came from **976521**, and the difference between that funding output and the spending outputs was **64 satoshis**. Both raw transactions have Merkle paths under their respective proof-of-work headers. A subsequent native Asset read of the funding block reproduced the same proof. [Confirmed fee and inclusion evidence](evidence/chronicle-shadow-native-fee-warmup.json.gz)

```sh
python3 qualification/shadow_native_warmup.py docs/evidence/chronicle-shadow-native-fee-warmup.json.gz
python3 -m unittest discover -s qualification -p 'test_shadow*.py'
```

On November 21, the passive observer upgraded to the same successor-aware release that had passed the complete live Chronicle corpus and DAR replay. The protected restart restored the same 110-header range, 32,668 witnessed outputs, cumulative work and applied directive sequence at height 976531. Five data peers reconfirmed the tip within three seconds. The receiver had two synchronized peers and no outstanding actions. The restored snapshot contained 125 eligible pending transactions, down from 335 in memory just before the stop. The last disk snapshot predates some peer admissions. The independently reconciled chain boundary survived. [Restart receipt](evidence/chronicle-waiting-mainnet-restart.json) · [Release validation](chronicle-results.md)

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

The corrected macOS observer subsequently received ten new eligible native payments at height 976535. Its controlled SIGTERM saved **81 eligible payments**, up from **71** in the earlier atomic snapshot. Independent comparison found all 71 original IDs, all ten additions, zero removals and identical header and outpoint commitments. The restarted process reported 81 before a data peer connected. Two peers reconfirmed within two seconds. A separate Linux ARM64 process reopened the exact closed snapshot using the same source release, with zero incoming transactions and 81 eligible payments. The live process also held **32 offers awaiting a successor** before stopping. That version reopened with zero waiting offers. These 32 had never qualified for its template. [Live process, snapshot comparison and isolated Linux reconstruction](evidence/chronicle-mainnet-controlled-stop.json)

The independently reconciled journal through that restart contains **114 consecutive observed tips** from height 976422 to 976535. The anchor at 976421 and the selected tip agree with the observed work increment. The replay verifies the earlier native transport failover and reports no missing coverage height or unexplained template omission. The one recorded body-coverage interruption and its recovery remain in the journal. The node still lacked the 147 headers it needs for the first mainnet candidate. The `qualify` command exits with status 1 and lists the incomplete 1,008-block and candidate conditions at this checkpoint. [Redacted journal, work and independent headers](evidence/chronicle-mainnet-h976535.json.gz)

```sh
python3 qualification/shadow_checkpoint.py replay docs/evidence/chronicle-mainnet-h976535.json.gz
python3 qualification/shadow_checkpoint.py qualify docs/evidence/chronicle-mainnet-h976535.json.gz
```

## Recover a live journal reader at the append boundary

At height 976536 the separate first-candidate reader exited with a JSON decoding error while the observer continued recording. A reader can encounter the old end of a file between the writer's JSON payload and its newline. If it advances beyond that fragment and the writer finishes before its next read, it can receive a newline by itself. The readers now stop that scan at the first unterminated byte sequence. The next scan reads the complete row from its start. A malformed newline-terminated record raises an error. The binary read also handles a pause in the middle of a UTF-8 character.

The independently reconciled prefix through height 976538 contains **117 consecutive prospective tips** and **zero unsigned transaction-bearing candidates**. The external receipt confirms the accumulated work against the active tip and observation anchor. The native bodies at heights 976536, 976537 and 976538 contain 12, 506 and 3 transactions. Each complete body matched its confirmed header and Merkle root. The reader obtained a five-group clock sample at every one of those heights. [Recovered observations and external work](evidence/chronicle-mainnet-h976538.json.gz) · [Original diagnostic and the three independently rehashed bodies](evidence/chronicle-shadow-tail-recovery.json)

```sh
python3 qualification/shadow_checkpoint.py replay docs/evidence/chronicle-mainnet-h976538.json.gz
python3 -m unittest discover -s qualification -p test_shadow_journal.py
```

A second retrieval over Teranode Asset returned all three complete bodies. Their combined 14,427,048 bytes matched the original native SV P2P deliveries byte for byte by SHA-256. The reader checked their proof of work and transaction Merkle roots again. [Independent transport readback](evidence/chronicle-shadow-tail-asset-readback.json)

To check those body commitments, obtain the confirmed native blocks by hash from a stock peer or Teranode Asset service. Save each block's complete wire bytes under its displayed hash in `$CONFIRMED_BLOCK_DIR`, then run the following from the repository root.

```sh
python3 - "$CONFIRMED_BLOCK_DIR" <<'PY'
import hashlib, json, sys
from pathlib import Path
sys.path.insert(0, "qualification")
from shadow_native import proofs, read_varint
receipt = json.loads(Path("docs/evidence/chronicle-shadow-tail-recovery.json").read_text())
for row in receipt["affected_and_following"]:
    block = (Path(sys.argv[1]) / row["block_hash"]).read_bytes()
    assert len(block) == row["block_bytes"]
    assert hashlib.sha256(block).hexdigest() == row["sha256_native_body"]
    assert read_varint(block, 80)[0] == row["transactions"]
    proofs(block, row["height"], row["block_hash"], set())
    print(row["height"], row["transactions"], "transactions verified")
PY
```

## Attribute observed tips to the running executable

The release report can replay a dated checkpoint offline. It replays the existing qualification contract and groups the selected window's consecutive prospective tip records by the executable SHA-256 in the preceding journal start. A restore keeps its own event and contributes zero blocks. A start without an executable digest leaves subsequent tips unattributed until a later start supplies one. The report records the first and last heights, block hashes, UTC observation times and journal positions for each segment. An archive can contain several segments from the same binary after another binary has run.

```sh
python3 qualification/shadow_release_segments.py docs/evidence/chronicle-mainnet-h976538.json.gz
```

The 117-tip checkpoint includes 14 process starts and zero transaction-bearing unsigned templates. Two same-binary starts restored height 976535. The first produced zero subsequent tip observations before the second start. The second produced the three observations at 976536–976538. The native transport drill at 976424–976425 matches journal records 7, 8, 24 and 28. Those legacy records contain no executable SHA. [Full segment, restart and transport attribution](evidence/chronicle-mainnet-release-segments-117.json) · [Observed archive](evidence/chronicle-mainnet-h976538.json.gz)

| Observed heights | Executable SHA-256 prefix | Release source | Tip observations |
| --- | --- | --- | ---: |
| 976422–976518 | Unknown | Unattributed legacy records | 97 |
| 976519–976520 | `5c1fbe433746` | [af9b605](evidence/chronicle-qualification-macos-release.json) | 2 |
| 976521–976531 | `8f99871ba04a` | [8c41f89](evidence/chronicle-recovery-macos-build.json) | 11 |
| 976532–976535 | `da0703697a19` | [c2a36d5](evidence/chronicle-waiting-macos-build.json) | 4 |
| 976536–976538 | `0aab15b07c70` | [10b93b4](evidence/chronicle-shutdown-macos-build.json) | 3 |
| **Total** | | | **117** |

The four identified builds accounted for 20 of the 117 observed tips.
