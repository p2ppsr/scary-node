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
