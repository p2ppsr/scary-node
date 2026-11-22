# An endorsed funding block at the start of native wallet traffic

A stock miner extended the height-484 funding block before the first payment arrived. The empty-state entrant had already witnessed those outputs and their first successor. The four wallets then announced signed payments from the registered finite source in the original order. The offer period called for 1,800 seconds, 10,800 batches of 512 payments, 225 Scary work opportunities and 75 stock SV Node opportunities. This is the only timed enrollment of the endorsed arm. [Preregistered design](witnessed-miner-registration.md)

The host measurement processes stopped after the last recorded inventory advertisement at 1,480.501 seconds. The eight Docker services stayed alive. The source and opportunity logs ended independently. Of 300 scheduled opportunities, 246 have recorded outcomes. The remaining 54 have no observed outcome. A post-interruption query found all four nodes still at the last audited parent, height 551. The experiment lacks its final offer, drain and settlement observations, so it supplies no estimate of full-period throughput. [Measured prefix, original log digests and subsequent queue readings](evidence/chronicle-sustained-endorsed-interruption.json.gz)

| Observed event | Count |
|---|---:|
| Native inventory batches announced | 8,884 |
| Payments advertised | 4,548,608 |
| Unique payments delivered at the final wallet counter sample | 4,546,560 |
| Independently read blocks / full stock body reads | 66 / 198 |
| Confirmed payments / witnessed input script checks | 4,498,432 / 4,498,432 |
| Selected Scary blocks / payments | 8 / 84,210 |
| Selected stock blocks / payments | 58 / 4,414,222 |
| Solves returned by stock outside the final audited chain | 3 |

Scary received 185 recorded opportunities. It solved eight, lacked an issuable job on 176 and observed one Stratum connection close. All 176 unready observations reported a connected receiver with contiguous coverage and working storage. On 172 slots there was no eligible pending transaction. On four it was waiting for independent observation after a solve on the selected parent.

| 50-opportunity window | Completed opportunities | Selected transaction-bearing Scary blocks |
|---:|---:|---:|
| 1 | 50 | 8 |
| 2 | 50 | 0 |
| 3 | 50 | 0 |
| 4 | 50 | 0 |
| 5 | 46 | 0 observed |
| 6 | 0 | Unobserved |

The initial endorsement let Scary start mining immediately. Three subsequent **fully observed** windows contained no Scary block, which rules out the registered requirement for one nonempty Scary block in each window. The last observed reference tip was `6bdadaef13a3c69736333fd69a6854454a6fd5cb8cbd11e22b880c94e7d87115`. All 66 audited Merkle commitments and 897,424,918 body bytes are bound to independent readbacks from SV Node and both Teranodes.

The signed-input replay checked all 5,529,600 source envelopes and reconstructed every observed previous outpoint. Scary’s 84,210 selected payments all used outputs created two to nine blocks before inclusion. In stock-mined blocks, 613,134 payments used a same-block parent, 3,743,743 spent an output from the immediately preceding block, and 57,345 spent an older output. Every fee was 200 satoshis. The eight Scary coinbases collected 16,842,000 satoshis above subsidy. Stock collected 882,844,400. The complement of the confirmed membership within the advertised prefix matched the stock mempool exactly by transaction ID. [Input ages, amounts and full-wire results](evidence/chronicle-sustained-endorsed-membership.json)

After the interruption, SV Node reported 50,176 pending transactions. Two further complete stock membership queries yielded the same ordered-ID commitment. On each reading, both Teranodes reported a candidate for height 552 containing 49,151 transactions. Scary had zero eligible transactions. Those later measurements describe pending work at the saved parent. The missing primary endpoints remain unknown.

## Replay

The first command verifies every complete original JSONL record against its source digest, checks the offer and opportunity schedules, header proof of work and linkage, three readback commitments per block, and the chronology of the later queue observations. The second command also decodes every selected transaction against the shared signed wallet corpus, checks Merkle roots, computes fees and parent ages from the actual inputs and outputs, and compares the remainder of the advertised set with the independent stock mempool commitment. Download the signed corpus from the earlier workload release. Unpack the publicly archived active bodies into a fresh directory before requesting the full wire comparison. The separate compressed stock mempool file preserves all 50,176 IDs in internal byte order. [Endorsed active bodies and pending IDs](https://github.com/p2ppsr/scary-node/releases/tag/native-workload-endorsed-prefix-v1) · [Common signed source](https://github.com/p2ppsr/scary-node/releases/tag/native-workload-regtest-v1)

~~~sh
receipt=docs/evidence/chronicle-sustained-endorsed-interruption.json.gz
.native/py/bin/python qualification/interrupted_workload.py verify "$receipt"
.native/py/bin/python qualification/sustained_block_archive.py unpack "$receipt" "$SIGNED_PAYMENTS" active-bodies.zip "$ENDORSED_BLOCKS"
.native/py/bin/python qualification/interrupted_workload.py verify "$receipt" --corpus "$SIGNED_PAYMENTS" --blocks "$ENDORSED_BLOCKS"
~~~
