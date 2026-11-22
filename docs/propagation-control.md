# Measuring propagation separately from native publication

The two frozen native studies start both submissions at a barrier and hash ready jobs in matched logical ticks. Their paths from a discovery to a receiver differ. A witnessed solution goes through the worker’s Stratum connection, candidate verification, durable storage, native announcement and the host-to-container SV peer connection. A stock miner receives an RPC solution and relays through its container peer link. Rotating container assignments changes physical sides. It leaves the implementation-specific paths intact. Sequential polls of the two receiver tips also include scheduling and validation latency. A competing block can reach a receiver without ever becoming its polled active tip. [Frozen opportunity driver](https://github.com/p2ppsr/scary-node/blob/783214923d1c90faf8b4428c561ceaa194b83926/qualification/native_cohort.py) · [Published paired relay study](production-cohort.md#publication-latency-comparison)

The four closed paired engineering sessions illustrate the distinction. With polled publication the witnessed miner won 2 of 21 tied discoveries. With immediate notification it won 11 of 11. Among ties, the witnessed median submission-to-first-polled-active time used just two observations on the polled build. The full competitor’s corresponding immediate-wake median used one. These observations support studying publication timing and are insufficient to establish equal wire arrival or an intrinsic stale-rate difference attributable to partial state. [Closed per-arm timing counts](evidence/chronicle-relay-comparison.json)

The two published 16-session sizing pilots locate every witnessed/full stale in a simultaneous-discovery race. The table uses the recorded winning hash after two successors. A first-active observation means at least one independent receiver reported that hash during polling. [Polled-build raw sessions](evidence/chronicle-native-pilot) · [Immediate-build raw shard](evidence/chronicle-immediate-pilot/sessions-71001-71016.jsonl.gz)

| Native path | Simultaneous wins, witnessed / full | Sole discoveries settled, witnessed / full | Tied hashes ever polled active, witnessed / full |
| --- | ---: | ---: | ---: |
| Polled, 80 ties | 12 / 68 | 65 of 65 / 111 of 111 | 12 / 68 |
| Immediate, 83 ties | 80 / 3 | 71 of 71 / 102 of 102 | 83 / 6 |

All 349 sole discoveries across these two witnessed/full samples settled. The reversal occurred in which concurrent solution won. The two sizing cohorts used independent seeds, and the set of solutions eligible for a first-active latency statistic changed markedly. The paired four-session intervention and these sizing observations answer different questions. Neither supplies a wire-arrival timestamp for the competing candidates.

## Controlled paths

The method takes the two genuine ordinary producer submissions and places a separate native observation peer on each producer’s egress. A worker supplies only the expected hash and body digest to the delivery logic. A gate obtains the exact valid two-transaction body from its producer over regtest SV P2P, accepting native block pushes and requesting announced inventory by hash. It verifies the header, target, Merkle commitment, payment and worker digest. Both candidates are delivered to **both** independent stock receivers. Two candidates per receiver are necessary to test arrival order before PoW decides the branch. [Transport and receipt implementation](../qualification/matched_propagation.py)

| Component | Experimental requirement |
| --- | --- |
| Producer sides | Keep the left stock fall-back miner and Scary’s contemporary data source on a private producer side. Keep the right stock producer on its own side. Preload the same stock fixture, give the witnessed entrant its normal observation window, and offer the same signed payment to all producers. |
| Measurement paths | Handshake two dedicated source connections and four dedicated destination connections, one per producer–receiver pair. Use the same SV version, single TCP stream, frame ordering and host monotonic clock on all four gates. |
| Isolation | Call `receiver_roster` before offering the payment and retain its result in `receiver_peers_before_offer`. Each stock receiver must see exactly its two separately tagged gate connections. Settlement reads both rosters again and the session verifier checks the same connection IDs and tags. Independently attest private network membership. Remove direct producer-to-receiver, receiver-to-receiver and alternate gossip paths during measurement. Keep the receivers on the selected common parent, and disable autonomous continuation mining until the prescribed step. Unknown connections or a parent diverging during setup invalidate the timing evidence and remain visible in the attempt ledger. |
| Restoration | The preselected continuation receiver mines through the target height plus two. The gate requests those three complete blocks from that receiver over its existing native connection, checks the headers and Merkle roots, then relays them to the two stock producers and the other receiver. All four private bridges and both receiver rosters stay isolated. Each stock must select the continuation before the next payment. |

The trial driver durably records the full session plan before it sends the first payment. The plan specifies physical orientation, first lane and receiver dispatch orders, arm, nonce lanes, continuation receiver, offer timing, job response deadline, common staging deadline, signed network delay, fence timeout and allowable arrival skew per parent. The readiness sampler makes one bounded normal job request per lane on a common schedule and records start, finish and unavailability. Missing work earns zero hashes and still consumes the scheduled opportunity. Every discovered block invokes its ordinary native submission, including a candidate whose submission or publication later fails.

For a discovery at host monotonic time `t₀`, the two gates share a staging deadline `t₀ + H`. A timely native body is released on each path at `t₀ + H + dᵢ`. An available late body is released as soon as its native bytes and assigned release time permit. The receipt records the missed common cutoff. Missing native bodies receive an explicit disposition. The continuation window stays fixed on parents with one or zero discoveries. Source validation, RPC or Stratum handling, disk persistence and time to native egress retain their own recorded timing. The imposed release schedule controls the downstream portion of the path.

## Certifying receiver ingress

Each gate sends an inventory notice, the complete block and a fresh 64-bit ping nonce consecutively on one TCP stream. The receiver can return the matching pong only after the bytes preceding that ping have reached the receiver. For each receiver and lane, let `Lᵢ` be the timestamp immediately before the gate writes the packet and `Uᵢ` the timestamp when the gate finishes reading that receiver’s matching pong. Complete block ingress lies in `[Lᵢ, Uᵢ]`. Receiver validation and delayed ping processing can widen this conservative interval. A pong certifies transport ordering and supplies no proof of block acceptance. [Stock SV Node ping handler](https://github.com/bitcoin-sv/bitcoin-sv/blob/6504a3aff65ba97c0f6c80962b033e35ecbfed4b/src/net/net_processing.cpp#L4234)

For that receiver the signed left-minus-right arrival difference lies in **`[Lₗ − Uᵣ, Uₗ − Lᵣ]`**. The verifier certifies an assigned differential `dₗ − dᵣ` only when the entire interval falls within its preregistered tolerance and both producers emitted their verified bytes before the common staging deadline. Report the bounds and certification for each receiver. Call a tied opportunity delay-matched only when both receivers certify it. Broad or missing fences remain in the scheduled cohort. A later arrival through a second path would violate the bound’s interpretation, which is why peer isolation is a prerequisite for live qualification.

## Outcome and comparison

Prespecify balanced equal-delay, left-delayed and right-delayed conditions in both the witnessed/full and full/full arms. Choose the timing tolerance using independent transport calibration and freeze it before inspecting stale outcomes. On each scheduled parent preserve both lanes’ readiness, hash attempts, native submission dispositions, source bytes, deadlines, four receiver legs, precontinuation tips, selected ancestor at depth two and session-final ancestry. Preserve unresolved and interrupted sessions separately from completed measurements. Compare the external plan with the complete exposure ledger before analyzing it.

For equal assigned delays, report stale discoveries per discovery, as well as readiness, discoveries and settlements per scheduled parent. Report the fraction of ties whose arrival timing was certified, and the fraction with a late or absent native body. Compare the signed left-minus-right stale difference when the assigned delay is on the left with the same difference when the delay is on the right. Reversing the delay tests an experimentally assigned network effect. Keep both roles and both receivers together in each session when deriving confidence intervals. Publish the full/full control and the all-scheduled comparison alongside any descriptive subset with certified arrivals. A receiver-specific acceptance or validation-order difference can remain after matching transport.

## Running a prospective study

The calibration alternates a single Scary or stock discovery across 16 consecutive parents. Each sends one distinct signed payment. Both lanes mirror the same valid body to both isolated stock receivers. For each parent the receipt includes four pong fences, independent active tips, independent stored-body digests and unchanged peer connection IDs. The independent verifier checks the complete wires and recomputes the proposed staging deadline, tolerance and signed offset. These mirrored deliveries contain no competing hash. [Calibration fixture](../qualification/matched_fixture.py) · [Calibration verifier](../qualification/verify_matched_calibration.py)

On the frozen Chronicle executable `8f99871b`, both stock receivers returned the exact selected body on all 16 parents. The four persistent sender–receiver streams produced 64 matched pong fences. Among the 32 per-receiver skew intervals, the median maximum absolute endpoint was **4.978 ms**, with a maximum of **20.104 ms**. The native source capture median was **21.500 ms** for the eight Scary blocks and **107.035 ms** for the eight stock blocks. Median local submission acknowledgment took **10.517 ms** on Scary and **5.092 ms** on the stock miner. The stock RPC acknowledged earlier while its complete body reached the gate later. The slowest native capture was **117.771 ms**. The calibration formula yields a **250 ms** common staging deadline, **23 ms** tolerance and **184 ms** signed delay. These are parameters for the prospective competition. The two initially queued transport checks stopped before fixture creation while the concurrent load trial drained. No race opportunity or calibration body was consumed in those two waits. [Raw isolated calibration](evidence/chronicle-matched-ingress-calibration.json)

In the independent continuation check, each receiving stock node took a turn mining an unopposed three-block branch. The gate fetched all six complete blocks using ordinary native `getdata`, checked the body commitments and relayed them over the existing isolated links. Both producers and both receivers selected the same final hash, `2a1b8fd96fe5dd1ab335bcd38ae139ebca52d4223d4cc489411f8254042e821e`. The receiver rosters stayed unchanged. This exercised the post-deadline transfer path before the first competing opportunity. [Six native continuation bodies and four-stock readbacks](evidence/chronicle-matched-native-continuation.json) · [Published fixed pilot registration](evidence/chronicle-matched-registration.json)

### Interrupted first pilot and replacement observer

The first registered sizing pilot stopped during the third offered parent of its first session. A stock HTTP observer returned an incomplete chunked reply to a block lookup. The first two parents had complete ledgers. The third had no recoverable gate outcome after the ephemeral stores and connections closed. Of the 576 originally scheduled parents, three were offered, two were fully recorded, one offered parent remained unresolved and 573 were never offered. No complete session, sizing estimate or stale-rate estimate was obtained. The first registration and its partial session remain available for audit. [Interruption accounting](evidence/chronicle-matched-interruption.json) · [Preserved partial ledger](evidence/chronicle-matched-interrupted-session.json.gz)

The replacement observer makes a single current-tip query per stock receiver and at most two identical by-hash body lookups. Each attempt uses a 200 ms socket timeout and its own host-clock interval. A recognized stock RPC error returns immediately. A malformed or interrupted body read gets one retry against the same hash. Exhaustion leaves that site's observation unknown. Both the receiver and the scheduled parent remain in the ledger. The observed hash is checked against the requested header before a body can count as stored. Raw readiness and discoveries are checkpointed before dispatch, and native receipts are checkpointed before the monitor consumes the deadline probe. The public archive also reports per-site readback attempts and unavailability for parents with no discovery. The common horizon is a minimum. A slow observation can delay continuation and the report records the overrun. Fresh registration uses a separate nonce-seed namespace and the original full/full control, zero offset and both signed-offset arms. The two earlier observed outcomes supply no sizing data. [Bounded receiver readback](../qualification/matched_continuation.py) · [Incomplete-response regressions](../qualification/test_matched_observer.py)

The prospective registration pins the calibrated executable, both release manifests’ source identity, the measurement code, the timing rule and distinct pilot/evaluation seed ranges. Each session has 36 scheduled parents, six per combination of producer pair and signed delay. The runner requests both jobs 1.2 seconds after offering the same payment. Jobs arriving beyond the next 1.5 seconds count as unavailable. It retains all offered payments and every hash attempt, including worker template bytes for replaying earlier nonces. [Study driver and fixed-phase checker](../qualification/matched_study.py)

Each receiver independently records its current active hash and whether it can supply each discovered full body. One probe starts 100 milliseconds before the common horizon. A second starts after gate closure and before the continuation. Query start and finish bracket each observation on the gate clock. A slow query can finish after the scheduled deadline. Only a completed on-time readback proves storage by that deadline. Native transport, storage, active selection and subsequent settlement have separate receipts. Receivers never compete to become the continuation miner. The preregistered receiver generates the successors after both gate intervals have ended. [Native continuation and timed readbacks](../qualification/matched_continuation.py)

The sizing pilot requires 16 complete sessions and 576 opportunities. Its primary statistic is the witnessed-minus-full difference in stale discoveries divided by discoveries in the equal-assigned arm. A two-sided 95% interval clusters by independent session. The resulting between-session variance sets a fresh fixed evaluation size for a target half-width of one percentage point. The report also checks whether the upper endpoint is at most +1 percentage point, so readers can distinguish precision from the range of extra stale risk compatible with the observation. Exact equality and mainnet risk remain outside the reach of a finite regtest experiment. Each producer arm needs at least 95% of equal-assigned tied opportunities certified on **both** receivers for an ingress-matched interpretation. Uncertified deliveries and unready opportunities remain in the all-assigned analysis. An interrupted measured session remains on disk for reconciliation and prevents a complete-phase result.

Set `BINARY` to the frozen native executable, `MAC_MANIFEST` and `LINUX_MANIFEST` to its two verified builds, and `STATE` to a persistent local study directory. Install the lab’s pinned SV Node framework dependencies before starting the fixture. Save the registration under a new published evidence path. The runner verifies the registration on the public branch before sampling. The evaluation also checks the published sizing record against the complete closed pilot. On a shared workstation, both calibration and study commands accept `--quiescence-receipt PATH` to wait for a separate workload drain before taking the measurement lock. An unopposed continuation check can be run with `--continuation-only` on the calibration command; it exercises both stock origins and transfers six blocks.

```sh
python3 -m unittest discover -s qualification -p 'test_matched*.py'
mkdir -p "$STATE"
.native/py/bin/python qualification/matched_fixture.py "$STATE/calibration.json" \
  --binary "$BINARY" --build-manifest "$MAC_MANIFEST"
python3 qualification/verify_matched_calibration.py "$STATE/calibration.json"
REGISTRATION=docs/evidence/chronicle-matched-observer-v2-registration.json
python3 qualification/matched_study.py register --binary "$BINARY" \
  --manifest "$MAC_MANIFEST" --linux-manifest "$LINUX_MANIFEST" \
  --calibration "$STATE/calibration.json" --output "$REGISTRATION" \
  --preceding-interruption docs/evidence/chronicle-matched-interruption.json
# Publish the registration before the pilot.
.native/py/bin/python qualification/matched_study.py run "$STATE" "$REGISTRATION" \
  --binary "$BINARY" --phase pilot
python3 qualification/matched_study.py export "$STATE" "$REGISTRATION" --phase pilot \
  --output docs/evidence/chronicle-matched-observer-v2-pilot
SIZING=docs/evidence/chronicle-matched-observer-v2-pilot/summary.json
# Publish the complete pilot before the independently seeded evaluation.
.native/py/bin/python qualification/matched_study.py run "$STATE" "$REGISTRATION" \
  --binary "$BINARY" --phase evaluation --pilot-summary "$SIZING"
python3 qualification/matched_study.py export "$STATE" "$REGISTRATION" --phase evaluation \
  --pilot-summary "$SIZING" --output docs/evidence/chronicle-matched-observer-v2-evaluation
python3 qualification/matched_study.py check docs/evidence/chronicle-matched-observer-v2-evaluation \
  "$REGISTRATION" --phase evaluation --pilot-summary "$SIZING"
```

The export packs up to 64 individually hashed gzip members per shard. Standard gzip readers can stream each shard as newline-delimited JSON, and the member index allows independent per-session extraction. Its summary commits to every complete session and any zero-opportunity preflight failure. The checker regenerates each randomized schedule, verifies worker and native wire bytes, reconstructs eligible nonce attempts and three-header settlements, and recomputes the counters and paired intervals. It refuses a phase with missing registered sessions or an unresolved measured partial.

A separate stock-node readback qualification observed one stored body and three expected unknown-hash answers on two independent live processes. Both selected-tip checks succeeded. The transport regression also covers a truncated stock response and exhausted retries on the same hash. [Stock observer receipt](evidence/chronicle-matched-stock-observer.json)


The descriptive subgroup report first replays the entire fixed-phase archive and matches it to its registered summary. For each producer pairing and signed-delay assignment it tabulates all scheduled parents, every two-discovery race, ties certified at both independent receivers, and the remaining ties. Each table includes the two miners’ opportunities and discoveries, who settled at depth two, later ancestry, unavailable native bodies, and each stock site’s successful, unavailable or exhausted lookups. Signed stale-rate differences use whole independent sessions. Conditioning on actual discovery and measured timing can select different populations. The full/full controls and all-assigned results accompany each such comparison. These tables do not determine the evaluation sample size. [Descriptive replay](../qualification/matched_subgroups.py)

```sh
python3 qualification/matched_subgroups.py docs/evidence/chronicle-matched-observer-v2-pilot \
  "$REGISTRATION"
python3 qualification/matched_subgroups.py docs/evidence/chronicle-matched-observer-v2-evaluation \
  "$REGISTRATION" --pilot-summary "$SIZING"
```

## Closed replacement sizing pilot

All 16 independent sessions sealed, with 96 scheduled parents in each of the six randomized producer/delay conditions and 576 in total. No setup retry or measured interruption occurred in this replacement cohort. The equal-delay witnessed miner had work on 84 of 96 parents. Its full-state competitor had work on all 96. The table preserves unready parents in those exposure counts. Each stale fraction uses that miner’s own discoveries as its denominator. [Registered summary and per-receiver counts](evidence/chronicle-matched-observer-v2-pilot/summary.json) · [Individually hashed raw sessions](evidence/chronicle-matched-observer-v2-pilot/shards.json)

| Producer pair, left / right | Assigned downstream delay | Left stale / discoveries | Right stale / discoveries | Ties certified on both receivers / all ties |
| --- | --- | ---: | ---: | ---: |
| Witnessed / full | Right +184 ms | 0 / 56 | 34 / 74 | 34 / 34 |
| Witnessed / full | Equal | 8 / 57 | 27 / 74 | 35 / 35 |
| Witnessed / full | Left +184 ms | 24 / 60 | 0 / 60 | 24 / 24 |
| Full / full | Right +184 ms | 0 / 66 | 30 / 60 | 30 / 30 |
| Full / full | Equal | 13 / 65 | 18 / 62 | 30 / 31 |
| Full / full | Left +184 ms | 35 / 62 | 0 / 69 | 35 / 35 |

In the equal-assigned witnessed/full condition, stale rates were **14.04% and 36.49%**. The paired difference was **−22.45 percentage points**, with a session-clustered 95% interval from **−43.01 to −1.89 points**. Across all 96 equal-assigned opportunities, the witnessed and full miners settled **49 and 47 blocks** respectively. That per-opportunity difference was **+2.08 points**, with a 95% interval from **−18.90 to +23.07 points**. The corresponding full/full conditional stale-rate difference was **−9.03 points**, with a 95% interval from **−27.91 to +9.84 points**. Both arms met the registered 95% ingress-certification threshold for equal-assigned simultaneous discoveries. [Control results and descriptive tie subgroups](evidence/chronicle-matched-observer-v2-pilot-subgroups.json)

In each probe window, all 1,152 active-tip queries returned. Of 1,530 by-hash body lookups across the two receivers in that window, 320 exhausted both attempts and remained unknown. Their discoveries and scheduled parents stayed in the analysis. [Raw deadline measurements](evidence/chronicle-matched-observer-v2-pilot/sessions-00000.jsonl.gz)

The sizing half-width was **20.56 percentage points**. The preregistered formula uses the observed between-session influence standard deviation of **0.38586** to fix **6,765 fresh independent sessions**, or **243,540 scheduled parents**. Each of the six producer/delay conditions receives 40,590 scheduled parents. The evaluation targets a one-percentage-point half-width and will report the precision actually achieved. These 16 sizing sessions and the interrupted first pilot are excluded from that evaluation. [Published sizing inputs](evidence/chronicle-matched-observer-v2-pilot/summary.json)

## Which tip did each stock receiver choose?

The published sizing pilot records each isolated receiver’s active hash before the deadline, after gate closure, and immediately before successor generation. The session plan, saved before the first payment, designates which receiver will supply the continuation for every parent. In all 189 two-discovery races, each receiver reported the same candidate at all three reads. The post-gate queries finished before successor generation. In every race, the designated receiver extended its selected candidate, both receivers then reached that candidate at depth two, and it remained selected at the end of the session. The table shows the equal-assigned cases. Left is Scary in the first row and a full-state miner in the control. [Recorded tips, timing bounds and settlement](evidence/chronicle-matched-pilot-receiver-tips.json)

| Equal-assigned ties | Both chose left | Both chose right | Opposite tips, left settled | Opposite tips, right settled | Total ties |
| --- | ---: | ---: | ---: | ---: | ---: |
| Witnessed / full | 18 | 4 | 9 | 4 | 35 |
| Full / full | 15 | 8 | 3 | 5 | 31 |

In the witnessed/full arm, Scary won 27 ties. Both receivers had already selected its block in 18, and the designated receiver selected it in nine of the 13 split cases. The full miner won the other eight ties. Each of those 13 splits proves that **both competing blocks passed active-chain selection on a stock SV Node**. The eight split full/full races show the same short-lived disagreement between full-state miners. This describes how ties settled in the fixture. The earlier common-relay experiment used a different release and delivery path.

The equal-assigned condition permits differences of up to 23 ms under the certification rule. At the designated receiver, conservative native ingress bounds identified a strictly earlier candidate in ten witnessed/full ties and six full/full ties. The earlier candidate won all 16. Of the other 50 ties, the two receivers selected opposite branches in 14 and agreed in 36. The arrival measurements leave the order unresolved in those 50 cases, including 36 where both receivers preferred the same branch. On the four arms with an assigned 184 ms delay, the receivers agreed on the earlier candidate in **123 of 123** ties. This pilot supports an arrival-order effect under the imposed delay. It cannot assign a cause to the remaining equal-delay preference or establish a stale-risk difference on mainnet. [Per-treatment and per-order counts](evidence/chronicle-matched-pilot-receiver-tips.json)

Run the descriptive replay on the closed pilot archive. The same command accepts a closed evaluation archive with its published sizing file supplied through `--pilot-summary`.

~~~sh
python3 qualification/matched_subgroups.py docs/evidence/chronicle-matched-observer-v2-pilot \
  docs/evidence/chronicle-matched-observer-v2-registration.json --receiver-tips
~~~
