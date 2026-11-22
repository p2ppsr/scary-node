# Sustained native mining on the waiting-queue release

This registration measures the operating envelope of Scary Node source `5148c31ae620544c31a61b1710172c4bcc524c54`, whose atomic snapshots now include native payments awaiting a funding successor. The Linux ARM64 executable SHA-256 is `5129b3dea8cecf04d369aa7a7d663024879291799af5424cb0158e8e337b643e`. The measured image ID is `sha256:0da9e6af62129fa7385f78ddf190780c3975598c49211b3fbb838837d9ca2d6f`. A reproducer can supply an independently built image and must verify the registered executable. The older E, R and C experiments retain their original releases. This run was specified before any of their timed outcomes were available. [Linux build receipt](evidence/chronicle-waiting-shutdown-linux-build.json) · [Earlier arm registrations](witnessed-miner-registration.md)

Use one fresh project, `scary-node-sustained-release-reordered`, port shift 8000, with two independent stock Teranodes, two disk-backed Aerospike stores, one stock SV Node and one empty entrant. Repeat the registered R starting boundary and native inventory schedule. The entrant joins at height 449, observes funding and reaches the common height-484 split with zero successors at first offer. Use the same 5,529,600 signed payments. Preserve the first eight complete generations, then use R’s predetermined mixed-age order. The offered-order SHA-256 is `03848032e833aa5bf7ed51366454149f0ac00dfae349d380a701f60c32728391`. This gives an exact-workload comparison with R and exercises the released queue under continuous native intake. [Source schedule and funding](witnessed-miner-registration.md)

Use the same four native wallets, stock settings, 1,800-second offer period, 225 entrant and 75 stock work slots, late-inventory drain and two selected successors. Require exact membership of the offered corpus, all required witnessed checks, three independent full-node readbacks per measured block, and inclusion of each selected entrant block on the audited chain. Sustained origination requires at least one nonempty selected Scary block in each of the six consecutive 50-slot intervals. The combined capacity target additionally requires 3,000 confirmed payments per second or higher from first offer through final settlement. Report each endpoint separately, including zero work, each miner’s contributions, readiness, publication latency, resource minima and operating settings. Retain every miss or interruption in the fixed run.

Enroll after the original E/R/C measurements and C’s terminal handoff. Verify fresh independent stores and the existing 45 GiB host and 37 GiB Docker preflight. Preserve at least 20 GiB of host space and 12 GiB of Docker space throughout measurement. Keep other timed experiments outside the common workstation lock. Use one timed attempt, with the same failure accounting and source retention as the earlier registrations.

```sh
SCARY_SUSTAINED_PROFILE=release-reordered .native/py/bin/python qualification/sustained_replication.py \
  .native/paired-corpus .native/sustained-signed .native/release-reordered-run
.native/py/bin/python qualification/verify_sustained_native.py \
  .native/release-reordered-run/receipt.json.gz --corpus .native/sustained-signed \
  --blocks .native/release-reordered-run/blocks --parent-ages
```
