# GoBDK native build provenance

The Go module at BDK `8a4e1d3d` contains a precompiled macOS arm64 native archive that reports `9d34cf2` at runtime. A fresh build of upstream `MergeGoBDK` from the module's checkout reports `8a4e1d3`. Both report BDK 1.4.0 and SV Node 1.2.3. Their reported SV revisions, `6504a3aff` for the distributed archive and `6504a3a` for the rebuilt archive, match the reference commit `6504a3aff65ba97c0f6c80962b033e35ecbfed4b`. The qualification manifest identifies both native archives and each linked executable. Both Go drivers were compiled by Go 1.26.0. The recorded Apple Clang and CMake settings apply to the local native rebuild. [Compiler, runtime and binary identities](evidence/chronicle-go-native-macos.json)

| macOS arm64 native archive | Reported upstream BDK build | Native archive SHA-256 |
| --- | --- | --- |
| Distributed with GoBDK v1.4.0 | `9d34cf2` | `ba574372509cf478dcca290f7ca1c205c84de24ed2e969fe6b8c3a159a1366d3` |
| Built from the Rust BDK source pin | `8a4e1d3` | `9bbc1844ff6ca9a85ddf185c9e2326e25348e93c698e1adc8eb3509d68d3a9d1` |

The two upstream revisions are siblings with parent `8796a0274e1c75458a3b427b3fa97dea896e523b`. Each Git tree contains 272 tracked blobs and gitlinks. The identities and modes match on 269 entries. The three different entries are the distributed Go native archives for macOS arm64, Linux arm64 and Linux x86-64. This comparison establishes that the tracked Rust, Go and C++ source was identical at the two revisions. [Git-tree evidence](evidence/chronicle-go-native-trees.json) · [Distributed archive's reported upstream revision](https://github.com/bitcoin-sv/bdk/commit/9d34cf20a727185286a686a7800f964c34a55525) · [Pinned BDK release source](https://github.com/bitcoin-sv/bdk/commit/8a4e1d3ddb16cbb2b3a98f82ea1ee6ff19a297e9)

## Same-byte Chronicle comparison on macOS ARM64

Both Go variants replayed all 62 funded transactions from the original live four-way corpus on release `c2a36d5`. The verifier reconstructed the extended transaction bytes and creation heights from the funding bodies. It checked mempool policy at the observed parent and consensus at the actual containing height. Each native archive accepted **46 under policy** and **50 under consensus**. All **248** new decisions agreed with the live stock SV outcomes. All 56 rejected Go contexts reproduced the original Go script rejection reason. The four consensus-only cases remain visible under each archive. This is a deterministic comparison of those inputs and contexts. [Paired raw decisions](evidence/chronicle-go-native-parity.json.gz) · [Live funded corpus and block readbacks](evidence/chronicle-waiting-entrant-corpus.json.gz)

Replay the public comparison without compiling BDK.

```sh
python3 qualification/go_native_parity.py \
  docs/evidence/chronicle-waiting-entrant-corpus.json.gz \
  docs/evidence/chronicle-go-native-macos.json docs/evidence/chronicle-go-native-parity.json.gz \
  --fresh-source-corpus docs/evidence/chronicle-source-go-live-corpus.json.gz
python3 -m unittest discover -s qualification -p test_go_native_parity.py
```

## Fresh stock regtest with the source-built Go archive

The first attempt stopped while funding a signature-vector family, before the harness emitted a complete corpus. The fixture used the first confirmed wallet output returned by `listunspent`. Its calculated change was negative and stock SV Node rejected the funding transaction with RPC error −26, `bad-txns-vout-negative`. The original abort did not capture the selected coin's value. Both corpus funders now choose a confirmed spendable output covering all fixture outputs, the fee and positive change. The aborted attempt contributes zero cases to the reported comparisons. [Retained setup failure](evidence/chronicle-go-native-funding-abort.json)

The corrected fresh run completed all 62 vectors at both heights with the rebuilt source Go oracle, a separate Rust BDK oracle, the frozen `c2a36d5` node and a clean stock SV instance. All 124 outcomes matched. Scary originated all 46 policy-eligible payments over Stratum, and stock SV selected every resulting block. Stock SV accepted 50 cases under consensus. The final independent successor was `154e78f65ffee20ed4a997a4301c816c91aea57b45740ed98310f25794ad163b`. The replay command above checks the new funded transaction bytes, the real active blocks, the exact executable digests and the Go runtime-reported source in each row. [Fresh source-built Go and live stock corpus](evidence/chronicle-source-go-live-corpus.json.gz)

## Confirmed mainnet spend

Both oracles initialized GoBDK with its upstream mainnet settings. Each accepted the ordinary transaction confirmed at height 976522, first using policy at 976521 and then consensus at 976522. Each used the locking script and amount proved against the funding header at 976521. Changing the funding amount by one satoshi while retaining the exact transaction and signature made both oracles reject in both contexts. This experiment covers one confirmed spend. The first prospective unsigned candidate has its own native proof and replay procedure. [Native creation and spending proofs](evidence/chronicle-shadow-native-fee-warmup.json.gz) · [Both mainnet oracle results](evidence/chronicle-go-mainnet-warmup.json) · [Build and replay commands](chronicle-qualification.md#compare-distributed-and-freshly-compiled-gobdk)

## Distributed Linux archive metadata

The version metadata in both distributed Linux archives matches the macOS runtime. The ELF object for each archive identifies the referenced string section and offset for `CGO_SOURCE_GIT_COMMIT_HASH`, `CGO_BSV_GIT_COMMIT_HASH` and the two version strings. Both source pointers resolve to `9d34cf2` and both SV pointers resolve to `6504a3aff`. To reproduce the metadata inspection, extract `version_cgo.cpp.o` with LLVM's archive reader, identify each symbol and its relocation, then read the NUL-terminated value at the indicated string-section offset. [Both Linux archive digests, symbols, offsets and values](evidence/chronicle-go-native-linux-identities.json)

```sh
archive=.native/bdk/module/gobdk/bdkcgo/libGoBDK_linux_x86_64.a
llvm-ar p "$archive" version_cgo.cpp.o > .native/go-version.o
llvm-readelf --symbols --relocations --wide .native/go-version.o
llvm-objdump --full-contents --section=.rodata.str1.1 .native/go-version.o
```

For aarch64, use `libGoBDK_linux_aarch64.a` and string section `.rodata.str1.8`.

## Dynamic comparison in the Linux ARM64 release image

Both Go archives ran inside the frozen `c2a36d5` Linux ARM64 release image. The installed Scary Node executable had SHA-256 `4c9ef032f2ebf4c806331fbbbc53464c517da04cc497209bb9cdb5e4cc572faf`. The driver verified the image ID, executable, all four Linux Go drivers and each native runtime's reported revisions before reading any outcomes. The container used a read-only root filesystem and disabled external networking. Go 1.27.1 compiled the Go drivers. GCC 12.2.0 and CMake 3.25.1 built the exact-source native archive. [Linux ARM64 build manifest](evidence/chronicle-go-native-linux-arm64.json) · [Scary Node release image and executable](evidence/chronicle-waiting-linux-build.json)

| Linux ARM64 native archive | Runtime BDK revision | Native archive SHA-256 |
| --- | --- | --- |
| Distributed with GoBDK v1.4.0 | `9d34cf2` | `3167d77e84633ea5547aed3e04adf40bebb79cd79fbca5a6237f997ef0fdc6fd` |
| Built from the Rust BDK source pin | `8a4e1d3` | `c20d2e64c38a05724b3b7333d00744ba1223efe6b7dee38bfe620c896b25b42c` |

For each archive, 46 of the 62 original funded payments passed policy and 50 passed consensus. All 248 decisions agreed with the stock SV outcomes recorded in the original live run. All 56 Go rejections included the original script rejection reason. The Linux trial reused those same recorded transactions, parents and input heights. The previously recorded Stratum blocks were produced by the original macOS entrant. Both Linux archives also accepted the confirmed mainnet spend at height 976522 under policy and consensus. Each rejected it in both contexts when the independently evidenced input amount was increased by one satoshi. Linux x86-64 coverage here is limited to the distributed archive metadata inspection above. [Linux ARM64 decisions and running identities](evidence/chronicle-go-native-linux-arm64-parity.json.gz) · [Original live funded corpus](evidence/chronicle-waiting-entrant-corpus.json.gz) · [Native mainnet funding and spending proofs](evidence/chronicle-shadow-native-fee-warmup.json.gz)

Verify the published receipt offline using Python 3.11 or later.

```sh
python3 qualification/linux_go_parity.py \
  docs/evidence/chronicle-go-native-linux-arm64.json \
  docs/evidence/chronicle-go-native-linux-arm64-parity.json.gz
```

The qualification runbook also describes how to build all four Linux executables and repeat the capture on an independently built image. [Linux ARM64 reproduction](chronicle-qualification.md#run-both-go-archives-in-the-linux-arm64-release-image)
