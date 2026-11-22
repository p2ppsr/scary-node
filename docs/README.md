# Read the code and the evidence

Start with the [two-Teranode lab](regtest.md) to watch an empty entrant receive a payment, issue work, publish a block and see it extended. A **witnessed output** is one whose creation the node observed on its selected branch. An **eligible payment** has the evidence and approvals needed for the next template. **Settlement** in a lab report means reaching its stated depth on the observed branch. The reports identify the release, network and depth used.

The [November 22 alpha review](alpha-review.md) collects the current runtime fixes and their funded, signed and multi-node regression results.

| Question | Where to look | What was observed |
| --- | --- | --- |
| How much can an entrant learn? | [Validation model](validation.md) · [Active-chain experiment](scenario-results.md) | After entering at height 143, the entrant followed 12 reference blocks. Its next three solved blocks carried 3, 8 and 12 payments. At the final shared tip it knew 97 of 452 live outputs. |
| Do both peers see the solved bytes? | [Protocol](protocol.md) · [First regtest receipt](verification.md) · [Branch and job recovery](reliability-results.md) | Both Teranodes returned the same block. A separate run demonstrated a same-parent job surviving a refresh and a later solution being detached by a greater-work branch. |
| How do the current validation rules compare? | [Chronicle comparisons and DAR](chronicle-results.md) · [Reproduce the comparisons](chronicle-qualification.md) | Rust, Go and SV Node agreed on 124 funded policy and consensus decisions. The stock SV reference also selected a recovered owner’s spend at the maturity boundary. [Teranode directive interoperability](teranode-alert-interoperability.md) records its separately observed outcomes. |
| Does being ready to validate mean having work to mine? | [Cold funding](cold-workload-results.md) · [Mixed-age funding](witnessed-workload-results.md) | In the cold trial the network settled 5,529,600 payments and the entrant had zero eligible jobs in 225 opportunities. In the mixed-age trial its 30 selected blocks carried 235,937 payments. Each result depends on funding age, order and peer paths. |
| What is known about stale blocks? | [Eight-session comparison](reliability-results.md) · [Matched-ingress pilot and study disposition](propagation-control.md) | In 1,024 ready-miner opportunities on a common relay the paired difference was −4.11 percentage points, with a 95% interval of −9.65 to +1.60. Availability, propagation and the chosen denominator affect the estimate. Equal risk has not been established. |
| What happens on a failure? | [Operating and restore procedures](operations.md) · [Deep reorg test](deep-recovery.md) · [Directive and solution races](directive-overlap.md) | The reports identify the fault injected, the readiness response and which reference nodes later selected a solution. |

For implementation work, use the [source map and test commands](../CONTRIBUTING.md). For interpreting older measurements, use each report’s source commit, topology and denominator. Archived registrations retain their original targets so readers can compare them with the completed evidence.
