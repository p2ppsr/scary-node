# A mainnet service interruption between two selected blocks

On November 22, 2026 UTC, Scary Node restored mainnet height 976547 after the observer and its local alert gateway stopped. The stock alert receiver remained running. Data peers reconfirmed the restored tip. The first newly observed block, 976548, directly extended it. The journal contains **127 consecutive selected heights** from 976422 through 976548. There were **624 seconds between local peer reports** across the interruption. The observer was unavailable for part of that interval. A transient branch seen only by other peers during that time would be absent from the local record. [Redacted journal and external chainwork readback](evidence/chronicle-mainnet-h976548.json.gz)

| UTC | Observation |
| --- | --- |
| 01:18:35 | Scary recorded its decision on 976547. It had 126 consecutive bodies and lacked a complete mainnet difficulty window. |
| 01:18:56 | Last peer report before the service interruption. |
| 01:29:18 | The same executable restored the exact 976547 hash. |
| 01:29:20 | First peer report after restart. Sixteen peer identifiers supplied 17 reports of the restored hash before Scary recorded its next decision. |
| 01:37:14 and 01:47:30 | Independent active-tip queries both returned 976547 and the same absolute chainwork. |
| 01:48:20 | First peer report of 976548. Eleven peer identifiers reported this hash 12 times before the local decision. |
| 01:49:04 | Scary finished observing 976548, its 127th consecutive body. The elapsed 44 seconds include delivery and local processing. Its difficulty window was still incomplete. |

SV P2P supplied both blocks. An independent Teranode Asset endpoint returned identical full wires. Height 976547 contained two transactions in 351 bytes. Height 976548 contained 382 transactions in 17,154,921 bytes. The receipt includes the body digests, two external interim tip reads and independently retrieved ordered lists of all 384 confirmed transaction IDs. Each list reproduces the Merkle root in its observed header. [Wire digests and independent transaction lists](evidence/chronicle-shadow-service-interruption.json.gz)

A subsequent live health sample at height 976549 recorded nine connected data peers and a reconfirmed tip. The authenticated gateway reported two alert peers, sequence 1 and zero outstanding actions. Scary had verified and applied sequence 1. This sample follows the end of the archived 127-height interval.

## Reproduce the observations

The first two commands replay the journal, the external chainwork anchors, the restart and both ordered transaction lists offline. The third applies the production qualification conditions. On this checkpoint it exits with status 1. The 1,008-block span and a transaction-bearing unsigned candidate with a verified successor remain outstanding.

```sh
journal=docs/evidence/chronicle-mainnet-h976548.json.gz
receipt=docs/evidence/chronicle-shadow-service-interruption.json.gz
python3 qualification/shadow_checkpoint.py replay "$journal"
python3 qualification/shadow_restart_boundary.py "$journal" "$receipt"
python3 qualification/shadow_checkpoint.py qualify "$journal"
```

To repeat full-wire verification, set `TERANODE_ASSET` to a reachable mainnet Asset base ending in `/api/v1`. The verifier requests the two confirmed blocks by hash, checks their byte counts, SHA-256 digests and proof of work, parses every transaction and compares each Merkle root. Alternatively, place the two full native block wires in a directory under their displayed block hashes and pass `--block-dir "$CONFIRMED_BLOCK_DIR"`. The full-wire result reports **17,155,272 bytes**. The offline journal-only command reports zero full-wire bytes and verifies the ordered transaction lists.

```sh
python3 qualification/shadow_restart_boundary.py "$journal" "$receipt" --asset "$TERANODE_ASSET"
```
