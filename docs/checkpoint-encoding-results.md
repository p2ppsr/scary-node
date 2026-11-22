# Durable checkpoint encoding on the reordered workload

Buffering the named MessagePack payload before hashing shortened all **24 paired saves** of the closed height-580 checkpoint. The median reduction was **463.106 ms**, or **28.78%** by paired ratio. The smallest reduction was 375.241 ms and the largest was 588.599 ms. The original writer and buffered writer each produced the same complete bytes in both warm-ups and all 48 timed saves. [Registered comparison](checkpoint-encoding-registration.md) · [All observations and checks](evidence/chronicle-checkpoint-encoding.json.gz)

| Milliseconds per durable replacement | Original writer, median (range) | Buffered before hashing, median (range) |
| --- | ---: | ---: |
| Complete save | 1,610.533 (1,525.928–1,748.144) | 1,147.098 (1,138.987–1,168.607) |
| Encode, hash and streamed writes | 1,493.102 (1,488.076–1,637.276) | 1,031.441 (1,022.369–1,053.908) |
| File sync | 101.548 (18.027–109.141) | 101.730 (93.923–109.354) |
| Replace by rename | 13.575 (13.003–15.869) | 13.407 (12.738–15.249) |
| Directory sync | 0.538 (0.446–1.314) | 0.556 (0.445–1.584) |

The paired ratio of buffered to original save time ranged from 0.6633 to 0.7541. Its median was 0.7119 in the 12 pairs with the original writer first and 0.7125 in the 12 with the buffered writer first. Phase medians are computed independently. The receipt also gives temporary-file open, checksum and flush times for each invocation. [Paired observations](evidence/chronicle-checkpoint-encoding.json.gz)

The original arrangement hashes writes from the serializer before the file buffer combines them. In a separate count over the same object, 316,588,507 payload bytes generated 72,267,681 such calls. Buffering ahead of the hash reduced them to 14,659 while retaining direct large writes. Encoding to an unbuffered counting sink took 163.560 ms. Hashing the contiguous original payload took 701.776 ms. Each supplemental measurement used one pass. The timed saves used the real filesystem and performed both syncs. [Writer code and integrity checks](../qualification/checkpoint_profile.rs)

The source was the Linux entrant's original 316,588,547-byte, checksum-protected checkpoint from the 5,529,600-payment run. Both writers reproduced the archived file byte-for-byte when supplied its payload in original order. Reopening Rust hash maps changed their traversal order. Independent MessagePack decoding of the complete newly encoded file recovered the same nested state as the archive. The receipt includes both whole-file and payload digests. [Original workload and checkpoint](witnessed-workload-results.md) · [File identities](evidence/chronicle-checkpoint-encoding.json.gz)

The run used one Apple M3 Max host with 128 GiB RAM, Docker 27.3.1, a 16-CPU Linux ARM64 VM with 95.35 GiB RAM and a dedicated ext4 volume. Both writers ran in one release-profile executable built from the original workload source. Each pair serialized the same object, reused the same directory and destination, and held the shared workload lock. The benchmark isolates one operation on one real state. The impact on complete settlement and miner propagation requires an ordinary workload measurement. [Release and host identities](evidence/chronicle-checkpoint-encoding.json.gz)

## Reproduce

```sh
python3 qualification/checkpoint_profile.py verify docs/evidence/chronicle-checkpoint-encoding.json.gz
```

For a full rerun, follow the registered build and execution commands with a protected copy of the original checkpoint. The first invocation failed during initial decoding when the read-only container root could not host an anonymous file for a large block body. It produced zero warm-ups and zero timed samples. The subsequent invocation directed temporary body files to the same dedicated writable volume used for checkpoint output and used the identical test binary. [Registered workload and full reproduction](checkpoint-encoding-registration.md) · [Temporary-file configuration](https://github.com/p2ppsr/scary-node/commit/5b7ffbe53e4422e23815db12cfb1711dc9d9c22c)

## Buffered release restart and stock acceptance

On November 22, 2026, the Linux ARM64 release opened an isolated copy of the **316,588,547-byte height-580 checkpoint**, saved a newly admitted payment on ordinary shutdown, and restored that payment before connecting to a peer. After stock SV Node v1.2.3 reconfirmed the parent, a Stratum worker solved height 581. The stock node selected the exact **424-byte** block returned by the worker reconstruction and mined its child at height 582. The entrant observed that child and its solved-block count increased from 56 to 57. [Receipt with complete regtest block and worker job](evidence/chronicle-checkpoint-upgrade.json) · [Linux release identity](evidence/chronicle-buffered-linux-build.json)

| Observation | Result |
| --- | --- |
| Old checkpoint reopened on the buffered release | 131 headers, 69,764 witnessed outputs, eight recoverable blocks, 56 prior solutions, zero eligible payments |
| Stock SV Node recovery | Reindexed its own frozen block files to the same height-580 hash, with zero blocks reoffered |
| Payment before shutdown | Stock accepted a signed spend of a witnessed height-575 output. The entrant marked it eligible with a 1,000-satoshi fee |
| First read after restart | Same parent, headers, witnessed-output count and prior-solution count. One pending payment, zero data peers, mining unready |
| Selected height 581 | `493a68b61f1f51b2b7c794b2a1e6aa61cd317f3d06576ed3f67b9a77c239a83c` |
| Stock successor at height 582 | `25fa9ed10385d2f317a15e8b38d7120062d43a54faa53ee2349573f24d5f4865` |

The process exited successfully after SIGTERM in **1.370 s**. A read-only Linux inotify watcher observed **1.239 s** from temporary-file creation to rename and **1.248 s** from creation to close of the renamed file. The first read of operator status took **2.616 s** from container launch and **2.371 s** from the later restart. The worker received its positive reply **1.176 s** after submitting. The stock node reported the selected hash by **2.492 s** after submission. These are individual wall-time observations from one restore, one shutdown and one restart. The stock poll gives an upper bound on selection time. The watcher measured when the kernel events were read. [Timestamp differences and status snapshots](evidence/chronicle-checkpoint-upgrade.json)

The first setup attempt reached height 580 on stock SV Node, then exited during entrant argument validation. The second exhausted Docker's default address pool during network creation. The successful attempt supplied both required asset arguments and selected an unused explicit subnet on an internal Docker network. All original volumes were mounted read-only by the proof and the original checkpoint digest was rechecked after the independent successor. [Bounded regtest driver](../qualification/checkpoint_upgrade.py)

### Replay the public receipt offline

The verifier checks the release manifest, recorded pre- and post-restart state, funding output, paid fee and signature, the exact job and ordered transactions, proof of work on both headers, and the stock successor’s parent. It reads the recorded timings and status observations from the receipt. Verifying those measurements independently requires repeating the run.

```sh
python3 -m venv .native/receipt-check
.native/receipt-check/bin/pip install 'ecdsa==0.19.1'
.native/receipt-check/bin/python qualification/checkpoint_upgrade.py verify docs/evidence/chronicle-checkpoint-upgrade.json
.native/receipt-check/bin/python -m unittest discover -s qualification -p test_checkpoint_upgrade.py
```

### Repeat with the retained workload checkpoint

The execution driver requires the original stopped or paused height-580 entrant, lease and stock volumes, a protected copy of the checkpoint, the public build’s Linux image in Docker, the pinned SV functional-test environment and Go. The workload receipt is public. The three complete data volumes are operator artifacts. Supply a fresh output directory and the original block directory. The driver can use those blocks to recover a missing stock suffix, checking every hash before submission. The measured run recovered the entire original chain using the stock block files alone. [Pinned test environment](chronicle-qualification.md) · [Original workload and recovery](witnessed-workload-results.md)

```sh
.native/py/bin/pip install 'msgpack==1.1.2' 'ecdsa==0.19.1'
.native/py/bin/python qualification/checkpoint_upgrade.py run \
  docs/evidence/chronicle-buffered-linux-build.json "$CHECKPOINT_COPY" \
  docs/evidence/chronicle-sustained-reordered.json.gz "$BLOCK_DIRECTORY" "$RESULT_DIRECTORY" \
  --stock-volume "$FROZEN_SV_VOLUME" --entrant-volume "$FROZEN_ENTRANT_VOLUME" \
  --lease-volume "$FROZEN_LEASE_VOLUME"
.native/py/bin/python qualification/checkpoint_upgrade.py verify "$RESULT_DIRECTORY/result.json"
```
