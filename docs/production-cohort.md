# Prospective production workload comparison

Use separate cohorts for sizing and estimation. The independent sampling unit is a session with fresh node volumes, fresh funding and a randomized assignment of miners to links. Fix the opportunity schedule before waiting for node readiness. Log each scheduled parent, both miners' ready flags and reasons, hash attempts, discoveries, tied discoveries, exact solved bytes, independent receivers' arrival times and their active hashes at depth two. A missed opportunity remains in the ledger. Continue tracking settled blocks that subsequently leave the chain.

Run both witnessed-versus-full and full-versus-full controls in each session with matched hash attempts and propagation conditions. Give each miner its own native publication path and independently queried receiving nodes. Randomize sides and traffic order. Send background payments independently of the opportunity schedule. Record feed loss, checkpoint and candidate latency, receiver disagreement and transport retries. Save release binary hashes and capacity limits with every session.

The primary estimate is the difference in stale discoveries divided by discoveries for each miner, with a paired session-level interval. Report availability, discoveries per scheduled opportunity and settled discoveries per scheduled opportunity alongside it. The control estimates asymmetry between paths. Predeclare a two-sided 95% interval with half-width at most one percentage point for the primary difference. Use 16 independent pilot sessions to estimate between-session variance, then freeze the number of fresh evaluation sessions. For equal session sizes use the ceiling of the squared ratio of the pilot standard deviation times the appropriate Student-t critical value to 0.01. For unequal discovery counts use the session-level influence values of the ratio estimates. Keep both arms of each session together in resampling. Publish the actual interval and label precision inconclusive if the planned precision was missed. Publish exclusions, incomplete sessions, and disagreements.

Alpha.3’s 1,024 opportunities came from ready miners through a common relay. Those measurements remain the result of that experiment. The independent-receiver cohort requires its own ledger. [Prior result and limits](reliability-results.md)

## Native SV receiver cohort

The executable cohort assigns one independent SV Node v1.2.3 receiver to each lane. Those receivers connect to one another. A stock SV miner connects separately to each receiver. The witnessed miner replaces the left stock producer in half of the opportunities and delivers through its direct SV P2P path. Reverse the physical assignment by session seed. The two full nodes and their receiver links support the full/full control. The separate mixed-node proof measures one candidate on SV and native Teranode paths. This sampling topology measures native SV-P2P outcomes on a single workstation.

For the sizing pilot use seeds 41001 through 41016, 32 parent opportunities per session and a 1,200 ms delay after offering each common signed payment. Shuffle the 16 witnessed/full and 16 full/full opportunities in each session before funding. Record both miners' availability and hash the ready jobs in disjoint nonce lanes at equal attempts per logical tick. Submit every first-tick discovery to its own producer. In a tie a barrier releases both calls. Independently poll the two receiver tips. The continuation miner is drawn before the race. It builds to depth two on the tip it actually selected. Read both active ancestries after depth two and again at the end of the session. An unavailable job earns zero attempts. A discovered block that misses native publication stays in the discovery denominator. Fresh evaluation seeds start at 51001 and use the same session design.

The common-payment workload submits a newly signed payment on every opportunity. The full references receive the common 105-block setup before the witnessed process joins. Client timings bound active-tip observation after submission. The run records CPU-worker results and the host's local links. Independent background traffic, geographically distributed receivers and physical ASIC performance require separate measurements.

```sh
.native/py/bin/python qualification/native_cohort.py .native/session.json --seed 41001
```

Run the sizing sessions serially to avoid competition between cohort sessions on the host. Keep the sizing data and evaluation data in separate directories. Each completed compressed receipt includes every scheduled exposure, candidate bytes for each discovery, both native submission outcomes and both receiver readbacks. Interrupted attempts retain their partial ledger. The estimator pools the two discovery ratios and computes paired influences per session. The confidence interval uses the Student-t critical value with session-count minus one degrees of freedom.

```sh
.native/py/bin/python qualification/run_cohort.py .native/pilot --phase pilot > .native/pilot-summary.json
.native/py/bin/python qualification/run_cohort.py .native/evaluation --phase evaluation \
  --pilot-summary .native/pilot-summary.json > .native/evaluation-summary.json
```

The full nodes import fixture history before opening the cohort peer links. In sizing session 41007 a simultaneous native header arrival during the original fixture import returned `duplicate-inconclusive` before the entrant started. That attempt contained zero scheduled opportunities. The unchanged session seed was rerun after serializing these setup steps. The node binary, opportunity schedule, receiver links and analysis are identical across completed sizing sessions.

In the original sizing pilot, the witnessed producer was ready on 18/26 sampled parents among the first four opportunities of each session and 208/230 among the remaining opportunities. Of the 22 later misses, 15 reported missing newer announced bodies and seven lacked an eligible common-payment job. The full competitor was ready throughout. This distinguishes transient body coverage and template availability from the effect of relay latency on an already discovered block. [Original pilot and sizing](evidence/chronicle-native-sizing.json)

## Publication latency comparison

The frozen engineering pilot sized the fresh evaluation at 2,263 sessions. Both phases retain the same measured executable. A separate experiment tests the one-second SV publication poll against an otherwise matched executable with immediate notification after durable solve. Run four independently initialized pairs with seeds 62001–62004, 32 opportunities per session, alternating which build runs first. The native fixtures and opportunity semantics are identical. Preserve both control arms and all ties, missing jobs and sole discoveries. Report earliest independent active-tip observation over the receivers, conditional on a solution being observed. Timing includes polling and native ingress. Each process identifies the tested binary by digest. A local scheduling lock prevents these comparison sessions from overlapping the primary cohort.

```sh
.native/py/bin/python qualification/relay_comparison.py docs/evidence/chronicle-native-pilot
.native/py/bin/python qualification/relay_comparison.py .native/relay-comparison \
  --timer "$POLLED_BINARY" --immediate "$NOTIFIED_BINARY" > .native/relay-comparison.json
```

### Measured publication behavior

Four independently initialized seed pairs produced 256 scheduled opportunities across the two builds. All eight sessions closed with matching depth-two and session-final readbacks on both receivers. Every competitive header, assigned nonce lane, proof of work, Merkle root and common payment can be checked from the receipts. The new build wakes an already connected SV peer sender after durable solve. Both builds include the periodic retry path.

| Build and producers | Ready per 64 scheduled, left / right | Stale per discovery, left / right | Settled per 64 scheduled, left / right | Stale difference, percentage points (95% interval) |
|---|---|---|---|---|
| Timer, witnessed / full | 56 / 64 | 19/42 and 2/43 | 23 / 41 | +40.6 (−0.2 to +81.4) |
| Immediate, witnessed / full | 56 / 64 | 0/37 and 11/38 | 37 / 27 | −28.9 (−40.8 to −17.1) |
| Timer, full / full | 64 / 64 | 12/48 and 10/38 | 36 / 28 | −1.3 (−58.2 to +55.6) |
| Immediate, full / full | 64 / 64 | 9/38 and 10/45 | 29 / 35 | +1.5 (−27.7 to +30.6) |

All sole discoveries settled. Under the timer, the witnessed producer won 2 of 21 simultaneous discoveries. With immediate wake it won 11 of 11. The witnessed sole-discovery median time from the pre-job sampling point to the first polled receiver reporting its block fell from 540 ms to 59 ms. The full competitor measured 144 ms and 132 ms. Starting the clock at the actual native submission gives 533 ms and 46 ms on the witnessed path, compared with 130 ms and 121 ms on the full path. The witnessed worker received its local acknowledgment in a median of 12 ms under both builds. Polling starts after the local submission calls return, so these are upper bounds on first active-tip adoption. The receivers' packet arrival times are unknown. The Scary producer reaches its receiver through a published Docker host port. Both stock producers run inside the Docker network. Randomizing the receiver identities preserves that host-to-VM difference.

The change in the witnessed-minus-full stale difference was −69.5 percentage points. A paired session-level delta-method interval using three degrees of freedom spans −118.6 to −20.4 points. The full/full change was +2.8 points (−44.1 to +49.6). A difference of two risk differences can exceed 100 points in magnitude. The paired intervention implicates the delay between a durable solve and native announcement as a cause of the measured disadvantage on this host. The four seeds had different coinbases, jobs and solved hashes on their two runs, so they generated different tie counts. The competitor's native path had a higher measured median after the change, and the corrected result includes eight unready witnessed opportunities. These observations cannot establish equal orphan risk with matched network propagation or under mainnet latency.

Use stock Python 3 to verify the raw records and regenerate the comparison. Each arm in the summary identifies the measured executable by SHA-256. [Raw paired sessions](evidence/chronicle-relay-comparison) · [Paired summary](evidence/chronicle-relay-comparison.json)

```sh
python3 qualification/relay_comparison.py --paired docs/evidence/chronicle-relay-comparison > /tmp/scary-relay-comparison.json
cmp /tmp/scary-relay-comparison.json docs/evidence/chronicle-relay-comparison.json
```

## Corrected executable cohort

The corrected-build study uses the same local topology, common-payment workload, balanced randomized arms, 1,200 ms sampling delay and depth-two settlement as the original. Freeze independent sizing seeds 71001–71016 before collection. Use 16 sessions of 32 opportunities for sizing and the same one-percentage-point precision formula. Begin the fresh evaluation at seed 81001 and run exactly the sample size returned by this pilot. Exclude every engineering-comparison and sizing observation from the fresh estimate. The two original study phases keep their own executable and seeds. Each phase uses the same binary digest as its own sizing pilot. The measured immediate-wake executable for this run has SHA-256 `2d3a2d351ad55ce203a2c50d7394e9d6353e9136b0f4cf9179f9b961274b80b7`.

Include every scheduled parent in availability and yield. Show the first four sampled parents separately from the remaining 28 in each session. Three blocks advance the common parent between opportunities, so the first four sample the first twelve blocks after witnessed funding. The lab uses its existing regtest readiness settings throughout. Report missing jobs and recorded reasons in each stratum. Continue retaining all of those rows in the primary estimate. Local native routes, a CPU nonce worker, a one-payment workload and the short cold-start session shape define the population to which these results apply.

```sh
.native/py/bin/python qualification/run_cohort.py .native/immediate-cohort --study immediate \
  --phase both --binary "$NOTIFIED_BINARY"
python3 qualification/cohort_estimate.py .native/immediate-cohort/evaluation --study immediate \
  --phase evaluation --pilot-summary .native/immediate-cohort/pilot-summary.json
```

The combined command writes a timestamped registration before the first session, verifies saved receipt identities on resume, and publishes the evaluation summary once the last planned session closes. The published [registration](evidence/chronicle-immediate-registration.json) identifies the executable measured on this workstation.
