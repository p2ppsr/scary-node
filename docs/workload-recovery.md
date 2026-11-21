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
