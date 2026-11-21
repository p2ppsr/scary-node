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
