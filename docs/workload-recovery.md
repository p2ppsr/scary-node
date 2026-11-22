# Recovering a measured regtest workload

The original receipts record transaction offers, readiness, scheduled work, block delivery, collection times and final queues. A fresh replay tests whether independent stock software can rebuild the same active chain from the retained bytes. A separate Scary Node restore tests the entrant's private observation boundary. Its identity and wallet material belong in private operator custody.

## Inputs

Use the exact common history through height 483, the h484 split funding, the signed payment corpus and the selected block assets for the relevant study. Check the release manifests before replay. Each compact ordinal archive binds a block's header and coinbase to an ordered list of transaction indexes in that signed corpus. The reconstruction verifies the resulting bytes against the complete original SV and both Teranode reads. Retained detached blocks keep their original hashes and bytes. [Signed source and measured outcomes](https://github.com/p2ppsr/scary-node/releases/tag/native-workload-regtest-v1) · [Python and native prerequisites](chronicle-qualification.md)

To create `inventory.zip` directly from the original release, download the inventory body parts and wallet bitmaps, and unpack the signed source using the [dataset instructions](sustained-native-trial.md). The packing command checks the complete release manifest, rebuilds each block and compares it byte for byte with the independently served original. The second command verifies the compact file using only the published receipt and the shared signed corpus.

```sh
gh release download native-workload-regtest-v1 --repo p2ppsr/scary-node --dir offer-assets --skip-existing --pattern 'native-sustained-inventory-*'
python3 qualification/replay_sustained_assets.py offer-assets workload/sustained-signed \
  docs/evidence/chronicle-sustained-inventory.json.gz docs/evidence/chronicle-sustained-inventory-assets.json \
  --compact inventory.zip
python3 qualification/sustained_block_archive.py verify \
  docs/evidence/chronicle-sustained-inventory.json.gz workload/sustained-signed inventory.zip
```

The measured inventory archive uses **7,925,566 bytes** in addition to the shared signed source. It reproduces all **77 blocks and 1,103,139,534 original wire bytes**. Its SHA-256 is `e012e2414c768465ddde932698a98139fc965fc768a01e03384927ed7c74ae62`. The archive includes the original header, transaction-count encoding and coinbase of each block. It preserves transaction order using four-byte ordinals. A block containing a transaction outside the source population is retained in full.

## Rebuild the confirmed chain

Start from the repository root with Docker and the qualification Python environment. The stock SV replay uses an empty 8 GiB guest RAM filesystem and runs with no network peers. The mixed replay starts another empty SV Node, an independent stock Teranode and its own Aerospike namespace. It sends ordinary valid blocks between the two stock nodes on their isolated peer network and reads the resulting bytes from Teranode's Asset service. The mixed replay checks for 38 GiB of free Docker guest memory and a 20 GiB host disk reserve before it starts. Fresh node storage uses temporary guest memory. The original timed measurements used independent disk-backed stores.

```sh
measured=docs/evidence/chronicle-sustained-inventory.json.gz
handoff=docs/evidence/chronicle-sustained-inventory-handoff.json

python3 qualification/recover_sustained_chain.py run "$measured" "$handoff" new-sv.json \
  --history workload/paired-corpus --corpus workload/sustained-signed --archive inventory.zip
python3 qualification/recover_sustained_chain.py verify "$measured" "$handoff" new-sv.json

python3 qualification/recover_teranode_chain.py sustained "$measured" new-teranode.json \
  --history workload/paired-corpus --corpus workload/sustained-signed --archive inventory.zip
python3 qualification/recover_teranode_chain.py verify "$measured" new-teranode.json
```

Either driver accepts a directory of full raw blocks via `--blocks inventory/blocks`. The endorsed arm also needs its registered successor between the common split funding and its first wallet offer. The driver supplies the recorded bytes when the measured arm is endorsed. The completion receipt records selected blocks and checks the separately retained detached set.

The 1,800,000-payment cadence body uses the original h484 split and exact signed fixture. Two small successor bodies are in the public recovery receipt. Reconstruct the fixture first as described in the cadence study. [Cadence fixture, commands and outcomes](live-cadence-block.md)

```sh
cadence=docs/evidence/chronicle-live-cadence-restore.json.gz
python3 qualification/recover_teranode_chain.py cadence "$cadence" new-cadence-tn.json \
  --history workload/paired-corpus --corpus workload/sustained-signed --body h485-1800000.block
python3 qualification/recover_teranode_chain.py verify "$cadence" new-cadence-tn.json
```

The inventory replay selected all 77 workload and settlement blocks on fresh SV Node and Teranode instances. A fresh Aerospike UTXO namespace supported the Teranode. Its Asset API served the original 1,103,139,534 bytes across those 77 reads, and both stock nodes reached height 561 at `73f04b330ef7f053f466e8f5b500f2725464738a29bc1246a71b7b488e61a599`. Setup and replay took 420.11 seconds, including one native peer redial at height 12 after the initial genesis-to-genesis handshake. A separate isolated SV process selected the same complete chain in 360.06 seconds. [Fresh native Teranode receipt](evidence/chronicle-sustained-inventory-fresh-teranode.json) · [Independent SV Node receipt](evidence/chronicle-sustained-inventory-fresh-stock.json)

The fresh cadence Teranode also selected the 1,800,000-payment block and its two successors. Its Asset service returned the original 359,090,376-byte body and the two 181-byte successors. Both fresh stock nodes selected height 487, hash `21cf7e3c2d09d22ef983d1b3ecb484a76a804a08f4f2400fb93168e2876d841e`. This replay took 180.28 seconds including startup and one redial at height 12. [Cadence native recovery receipt](evidence/chronicle-cadence-fresh-teranode.json)

Two untimed samples taken after the original cadence run recorded zero SV mempool entries, zero SV transactions awaiting validation and zero Scary Node pending transactions. Both original Teranodes selected the same h487 tip, reported zero transactions in their next work candidates and returned zero for the block-assembly transaction queue and each instrumented block-priority queue. The eight original services returned to pause after 5.52 seconds. [Original terminal queue observations](evidence/chronicle-cadence-terminal-queues.json)

The original inventory project also exposed both stock Teranode queue gauges and next work candidates. Two samples at the settled h561 tip recorded zero for the candidate transaction counts, assembly queues, block-priority queues, SV mempool, SV validation queue and Scary pending count. All eight services returned to pause after 5.70 seconds. [Original inventory queue observations](evidence/chronicle-sustained-inventory-terminal-queues.json)

### Restart stock SV Node from its archived database

The original inventory SV process was paused at h561. A fresh, isolated process started from a copy of that exact data volume. Ordinary startup selected regtest genesis, reported zero indexed headers and could not serve the h561 header or block. It stayed there for 65.93 seconds. The eleven archived `blk*.dat` files nevertheless contained genesis and all 561 selected blocks. Every selected block matched the retained wire digest. The normal-start and reindex processes both used `-regtest -datadir=/data`; their `/data/regtest` wallet, first block file and two LevelDB `CURRENT` files matched the original byte-for-byte immediately before startup.

Starting again from a clean copy with stock `-reindex=1` selected the original h561 in 88.03 seconds. The process had zero connected peers, received zero re-offered block wires and loaded a wallet containing 77 transactions. A paused copy preserved all block bodies for this workload. Recovering its selected chain required rebuilding the native stock index. [Original stock archive recovery receipt](evidence/chronicle-sustained-inventory-stock-reindex.json)

To repeat this check with a privately archived SV regtest data volume, give the driver the enrolled volume's tar rooted at its `regtest` directory parent. Run `verify` to inspect the shipped result without an operator archive. The driver scans the persisted block files, compares them with the selected body digests and records the first responding RPC and subsequent progress for each native start. It reports any exact block suffix it re-offers if native reindex cannot complete the original chain.

```sh
python3 qualification/recover_stock_archive.py boot "$measured" workload/paired-corpus workload/sustained-signed \
  private-sv-data.tar.gz new-stock-restart.json --archive inventory.zip
python3 qualification/recover_stock_archive.py verify "$measured" \
  docs/evidence/chronicle-sustained-inventory-stock-reindex.json
```

### Compare the original wallet and the recovered wallet

Before archiving the operator's settled regtest volume, capture a private fingerprint from the enrolled stock process at its measured terminal tip. The comparison records key and metadata counts, checks the full spendable outpoint set and covers every wallet transaction returned by `getwalletinfo`. Stock `gettransaction` supplies its serialized transaction and complete wallet record. The key dump stays in the container's temporary memory filesystem. Store the private fingerprint alongside the archive.

```sh
python3 qualification/stock_wallet_archive.py "$measured" "$enrolled_sv_container" private-wallet.json
python3 qualification/recover_stock_archive.py boot "$measured" workload/paired-corpus workload/sustained-signed \
  private-sv-data.tar.gz new-wallet-restart.json --archive inventory.zip \
  --private-wallet-summary private-wallet.json
```

SV Node refills reserve keys each time it opens a wallet. The exact-comparison path supplies `-keypool=1` to preserve a populated reserve while reading the recovered wallet. The stock archive driver records the flags of both running processes. [SV Node wallet startup](https://github.com/bitcoin-sv/bitcoin-sv/blob/6504a3aff65ba97c0f6c80962b033e35ecbfed4b/src/wallet/wallet.cpp#L4154)

The first restore with SV Node’s default keypool returned 2,079 key and master rows against 2,078 in the original. A separate restore with the bounded keypool reopened the original 2,077 key metadata records and master key exactly. All 77 full wallet transaction responses, their signed bytes and the complete set of zero spendable wallet outputs matched. On that copy, normal startup remained at genesis for 65.98 seconds. Reindex selected the original h561 in 87.83 seconds, with zero connected peers and zero block re-offers. [Stock wallet and reindex receipt](evidence/chronicle-sustained-inventory-stock-wallet.json)

### Reopen an archived Teranode and Aerospike pair

Keep each reference's private Compose inspection record, Teranode data volume, Aerospike namespace volume and Aerospike system metadata. The inspection record carries the enrolled images, peer identity and database settings. The driver restores each pair on its own internal Docker bridge and reads every measured block through its Asset API. It checks the native connected-peer gauge and both services' network memberships before and after those reads. Each pair needs 30 GiB of available guest RAM before startup.

```sh
python3 qualification/restore_teranode_archive.py boot "$measured" private-services.json tn1 \
  private-tn1-data.tar.gz private-as1-data.tar.gz private-as1-meta.tar.gz new-tn1.json
python3 qualification/restore_teranode_archive.py verify "$measured" new-tn1.json
```

A restored SV Node can also recover a saved nonempty pending set. On an interrupted workload, pass `--original-pending-ids path/to/stock-pending.sorted-internal.bin.gz` to `qualification/recover_stock_archive.py boot`. It checks every retained ID against the original set, reoffers only missing signed payments from the registered corpus, and compares the final pool byte-for-byte with the original ordered IDs. The proof records the numbers initially retained and reoffered.

When the original node had pending payments, pass `--original-candidate path/to/original-candidate.block` to the boot command. The proof records the restored candidate’s membership and ordered ordinary transaction bytes alongside the independently saved original. The coinbase and mining time can change at restart. A changed payment set is visible in `pending_recovery`.

Use the second pair's three archives and `tn2` for its independent run. Both original inventory pairs reopened at h561, produced an empty candidate on the original hash and served all 77 original Asset bodies. Each returned exactly 1,103,139,534 bytes matching the timed study. Recovery and reads took 86.07 seconds for TN1 and 84.20 seconds for TN2. Each native peer gauge read zero before and after the full readback. [Archived TN1 and AS1](evidence/chronicle-sustained-inventory-archived-tn1.json) · [Archived TN2 and AS2](evidence/chronicle-sustained-inventory-archived-tn2.json)

## Restore an entrant from archived volumes

Freeze the enrolled Compose project after its settlement and final queue observations. Keep the two private entrant archives with its release image. The restore command launches that image on a fresh private RAM filesystem, reads both archives, hashes the checkpoint before startup, and queries the actual running node in a network namespace with no data peers. It compares its selected tip, witnessed coverage and measured script-check counts with the recorded observations. The complete original cadence window also permits a row-for-row restart comparison.

```sh
python3 qualification/restore_witness_archive.py capture ENROLLED_PROJECT private-backup
python3 qualification/restore_witness_archive.py boot "$measured" \
  private-backup/data.tar.gz private-backup/leases.tar.gz new-checkpoint.json \
  --image ENROLLED_IMAGE
python3 qualification/restore_witness_archive.py verify "$measured" new-checkpoint.json
```

For the cadence entrant add `--setting SCARY_MAX_BLOCK_BYTES=536870912 --setting SCARY_MAX_JOURNAL_BYTES=1073741824 --setting SCARY_MAX_UNDO_ITEMS=8000000`. The capture prints the enrolled image ID and applicable numeric settings. Its archives include private identity and lease contents. Publish the resulting numeric and hashed proof only.

The archived inventory entrant booted with zero connected peers at h561 from its 251,355,518-byte checkpoint. It recovered 69,745 witnessed outputs and 112 contiguous validation rows. All 77 rows corresponding to the timed inventory blocks matched the original script-check and ordinary transaction counts. [Inventory entrant restore](evidence/chronicle-sustained-inventory-archived-entrant.json)

The cadence entrant booted its archived 1,000,497,785-byte checkpoint in a separate isolated process with zero data peers. Its 69,671 witnessed outputs and all 38 complete validation records from h450 through h487 matched the original post-settlement process, including 1,800,000 successful script checks in h485. [Cadence entrant restore](evidence/chronicle-cadence-archived-entrant.json)

An independent confirmed-chain reconstruction cannot recreate an original unconfirmed mempool, validation queue or a timed delivery. Preserve the original runtime or an independently demonstrated restorable copy whenever that state is outstanding. The direct-intake control ended with an outstanding queue. [Original handoff and qualification limits](sustained-native-trial.md)
