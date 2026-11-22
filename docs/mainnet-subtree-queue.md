# Unconfirmed subtree intake at mainnet height 976554

The observer counted 5,581 full offers among 6,003 subtree announcements after restarting at height 976547. It independently verified complete block coverage through 976554. The trace contains 5,917 distinct advertised roots. Of those, 5,497 had only full offers and 420 received at least one non-full offer. One of the 420 first encountered a full queue and later received a non-full offer. [Passive observation and confirmed transactions](evidence/chronicle-shadow-subtree-queue.json.gz)

All 6,003 signed publisher/root pairs in the original interval were distinct. Nineteen roots appeared under multiple publishers, accounting for 86 repeat announcements. Repeated advertisements explain a small part of the count. The operator reported nine data peers and the trace contained ten signed originating publishers. A GossipSub peer can relay another publisher’s signed message. Across the interval there were 313 uninterrupted runs of full offers. The median run contained nine announcements, the 95th percentile contained 66, and the longest contained 130. These are event counts. The release logged no timestamps on individual offers. [Frozen GossipSub dispatch](https://github.com/p2ppsr/scary-node/blob/5148c31ae620544c31a61b1710172c4bcc524c54/src/peer.rs#L228)

| Last body callback height | Announcements | Full |
|---|---:|---:|
| Before the first callback | 3 | 0 |
| 976547 | 873 | 719 |
| 976548 | 178 | 175 |
| 976549 | 634 | 623 |
| 976550 | 1,000 | 929 |
| 976551 | 944 | 892 |
| 976552 | 1,094 | 1,039 |
| 976553 | 23 | 23 |
| 976554, through the original cutoff | 1,254 | 1,181 |

## What the counter establishes

One worker handles the entire subtree path for all publishers. It can process one root while 16 distinct roots wait. An offer for a root already pending or among the 256 most recently completed roots returns success immediately. While pending, one root can collect up to three distinct Asset bases. Another source still returns success after that cap. For a new unique root, a failed nonblocking send to a full or closed channel increments the counter. A failed offer creates no pending entry. A subsequent announcement can try again. [Frozen scheduler](https://github.com/p2ppsr/scary-node/blob/5148c31ae620544c31a61b1710172c4bcc524c54/src/peer/fetch.rs#L51) · [Status and event counter](https://github.com/p2ppsr/scary-node/blob/5148c31ae620544c31a61b1710172c4bcc524c54/src/main.rs#L360)

Each fetch resolves the Asset host and constructs a client, reads the body, checks its complete Merkle root, and offers the transactions to ordinary witnessed admission. Consecutive admission batches yield after 32 offers or about four milliseconds of work, whichever comes first. The enrolled process allowed 256 MiB per body, eight seconds to connect, 45 seconds between reads and up to 311 seconds overall for one subtree download. The channel continued accepting fresh roots after bursts. The direct mechanism was backlog pressure on a single worker. The trace cannot apportion that time among remote delivery, local hashing, waiting for the mining lock and transaction checks. [Download path](https://github.com/p2ppsr/scary-node/blob/5148c31ae620544c31a61b1710172c4bcc524c54/src/peer/fetch.rs#L187) · [Verification and admission batches](https://github.com/p2ppsr/scary-node/blob/5148c31ae620544c31a61b1710172c4bcc524c54/src/peer/subtree.rs#L125)

The release logged source failures but omitted successful root identifiers and completion times. There were zero logged failed sources in the matched prefix. At most 17 scheduled roots could still be in progress at the cutoff. Under the frozen scheduler, those observations imply completion for **at least 403 of 420 distinct roots with a non-full offer**. This is a lower bound from queue occupancy. An exact delivery count, an admission delay for any particular root, and the number of unique missing transactions are unavailable. The block download worker has its own queue. [Source and cache handling](https://github.com/p2ppsr/scary-node/blob/5148c31ae620544c31a61b1710172c4bcc524c54/src/peer/fetch.rs#L87)

The next 1,000 announcements ended at height 976557. They comprised 755 full and 245 non-full offers. None of the original 5,497 only-full roots appeared again in this follow-up. Later announcements supplied 241 additional distinct roots with at least one non-full offer. The follow-up measures exact repeats of a subtree commitment. Overlap within differently assembled subtrees requires transaction-level evidence. [Fixed follow-up trace](evidence/chronicle-shadow-subtree-queue.json.gz)

## What later blocks establish

The comparison uses the nine confirmed blocks at heights 976547–976555, including the first successor after the original cutoff. It verifies their complete ordered transaction lists against the proof-of-work headers. One public stock Teranode reported a single native subtree per nonempty payment block and zero for the coinbase-only block. Rebuilding each of those eight roots from the confirmed payments and the native coinbase placeholder reproduced the stock results. None of the eight complete roots appeared in the original trace. [Confirmed-block collector](../qualification/shadow_subtree_blocks.py) · [Stock Asset API](https://bsv-blockchain.github.io/teranode/howto/miners/minersHowToInteractWithAssetServer/)

We also compared each advertised root with the commitments for each individual confirmed transaction, each ordered ordinary-payment prefix, and each such prefix preceded by the stock coinbase placeholder. Twenty-one distinct advertised roots matched placeholder prefixes and all had only full offers. Every matching announcement preceded its corresponding block callback. The exact prefixes overlap. Their union covers **34 confirmed ordinary payments across four blocks**. This establishes confirmed content for those roots. Another peer or an overlapping subtree could already have supplied any payment. Arbitrary reordered subsets, suffixes and transactions in later blocks are outside this comparison. [Verified matching algorithm](../qualification/shadow_subtree_queue.py)

| Confirmed height | Distinct matching prefixes | Longest prefix |
|---|---:|---:|
| 976549 | 8 | 11 payments |
| 976552 | 7 | 14 payments |
| 976553 | 2 | 5 payments |
| 976555 | 4 | 4 payments |

The separate operator snapshot at height 976556 recorded 6,752 subtree notices, 6,190 full offers, nine connected data peers and zero block-coverage gaps. Subtree intake had counted 121 admitted offers, 86,990 duplicates, 60,171 ineligible offers and four held for a successor. SV intake had counted 1,029 admitted offers over the same process lifetime. These transaction-level counts cover repeated offers and independent transports. They do not identify which root delivered an admitted payment. [Ordinary peer-admission accounting](https://github.com/p2ppsr/scary-node/blob/5148c31ae620544c31a61b1710172c4bcc524c54/src/mining/waiting.rs#L137)

## Reproduction

The public receipt uses encounter-order publisher numbers, ordinal root numbers and one-way commitments to advertised roots. It contains ordered confirmed transaction IDs. The verifier reconstructs the root commitments for known confirmed content. Original peer identifiers, unconfirmed transaction IDs and signed transaction bytes are absent.

```sh
.native/py/bin/python qualification/shadow_subtree_queue.py verify \
  docs/evidence/chronicle-shadow-subtree-queue.json.gz
```

An operator with the retained mixed stdout/stderr capture can recreate the trace using process start 18. Twenty-nine notices in the original interval shared a physical log line with diagnostic output. The parser extracts complete event frames and reconciles their count against the original status. Parsing only standalone JSON lines misses those notices. To repeat the independent comparison, supply the recorded mainnet journal, the local cache of confirmed wire bodies keyed by block hash, and an approved public HTTPS Asset base ending in `/api/v1`.

```sh
.native/py/bin/python qualification/shadow_subtree_blocks.py \
  "$journal" "$confirmed_body_directory" "$public_asset_base" confirmed.json
.native/py/bin/python qualification/shadow_subtree_queue.py capture repeated-trace.json.gz \
  --log "$observer_log" --confirmed confirmed.json --operator "$sanitized_status"
```

Reading the combined 7,003 notices scanned 36,115,357 bytes in the existing process log. Reconstructing and writing the trace with all confirmed-content comparisons took 0.66 seconds of wall time and 0.65 seconds of CPU with a peak resident set of 29.9 MiB. The collector issued nine bounded requests for confirmed block metadata totaling 17,345 response bytes. It read 53,970,704 bytes of confirmed block bodies from the local cache. Repeating the collection took 12.21 seconds of wall time including request pacing, 0.23 seconds of CPU and a peak resident set of 156.3 MiB. It opened no mainnet P2P sessions and submitted no transactions. The enrolled release remained active throughout.

The registered scheduler trial measures the effect of backlog depth and concurrent fetches independently. It retains admission order and the separate block queue. Future mainnet telemetry can identify each root’s scheduling outcome, verified completion and processing time, while keeping public reports aggregated. [Controlled comparison](subtree-scheduler-study.md)
