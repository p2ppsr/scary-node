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

## Keep the host recorder running through a terminal close

The wallet sender, Stratum worker, opportunity recorder and resource sampler run on the host. Docker keeps the full nodes running independently. Losing the host driver stopped the original endorsed-funding measurement at 246 opportunities. The surviving full-node stores preserved the exact blocks and terminal pool, while the final 54 scheduled endpoints remained unobserved. [Recorded interruption](endorsed-workload-interruption.md)

On a logged-in macOS workstation, the following alternative launches the complete driver once under the user service manager. Run it from the repository root. Give the service logs and measured output separate fresh directories. The helper inherits the executable search path. Pass study-specific settings through `/usr/bin/env` as shown.

~~~sh
job_dir="$PWD/.native/release-reordered-service"
python3 qualification/launchd_once.py "$job_dir" -- /usr/bin/env \
  SCARY_SUSTAINED_PROFILE=release-reordered "$PWD/.native/py/bin/python" \
  qualification/sustained_replication.py .native/paired-corpus .native/sustained-signed \
  .native/release-reordered-run

launchctl print "$(cat "$job_dir/service-target")"
~~~

The plist stays in the service log directory, with `RunAtLoad` enabled and `KeepAlive` disabled. Closing the submitting terminal leaves the driver owned by the logged-in user’s service manager. After the driver exits, inspect its exit code, `stdout.log` and `stderr.log`, and use the workload verifier above on the actual receipt. A completed timed run also produces `quiesced.json`. If a host interruption leaves partial logs, preserve the original opportunity indices and the unobserved tail. Once the process has finished and its evidence is retained, unload its service with `launchctl bootout "$(cat "$job_dir/service-target")"`.

The release run also retains each submitted worker header through response errors. The independent verifier checks the exact selected header and reports whether the worker received an acknowledgment. The same method applies to the cold arm. [Header attribution and replay](cold-miner-registration.md#attributing-an-accepted-block-when-a-worker-misses-the-reply)

## Prospective test of the ordered-fetch and checkpoint changes

Run one additional 5,529,600-payment workload on the first published Linux ARM64 build that combines the ordered subtree fetcher from `9901c89644b8f588e73d6e6261f0cffe55ad1f6d` with buffering before SHA-256 in the production checkpoint writer. Record its source commit, executable and build receipt here before enrollment. The frozen `5148c31` measurement above retains its own result. This additional run measures the combined implementation on the same host and workload. The separate signed-inventory trials and paired checkpoint measurements test the two changes individually. [Subtree protocol](subtree-scheduler-study.md) · [Checkpoint comparison](checkpoint-encoding-registration.md)

Use a new project named `scary-node-sustained-buffered-reordered` with port shift 9000, the registered R funding history and the same ordered payment IDs, wallets, worker, reference images, resource floors and common measurement lock. Keep the default four overlapping subtree reads, queue capacity 256 and ordinary checkpoint durability. Capture the exact running executable and settings before the timed run. Enroll after the `5148c31` run's terminal handoff on fresh independent stores. Preserve the single timed attempt, all 300 scheduled primary opportunities, the bounded drain, original solved journal, coinbase identities, prepared worker headers and the independently read stock bytes. A failed setup before the first timed offer records its reason and can resume from verified initial conditions.

The primary operating target requires all 5,529,600 registered payments confirmed through two successors in at most 1,843.200 seconds, plus at least one independently selected, nonempty Scary block in each of the six primary windows. Account separately for the source payments originated by Scary and the other miner, unavailable work, solution attempts, stale blocks and readiness. Preserve any earlier throughput miss. The enrolled passive mainnet observer and the three registered stale-rate evaluations keep their existing executables.
