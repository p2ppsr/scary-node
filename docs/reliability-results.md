# Reliability release results

Scary Node 0.1.0-alpha.3 recovered its witnessed outputs across a short reorg, kept valid same-parent Stratum work, and tracked both settled and displaced solutions. Two fresh live proof runs produced the same recovery and settlement outcomes. The enlarged matched-work sample recorded **1,024 opportunities**, four times the original cohort. [Reproduce the proof and the sample](reliability.md)

## What happened on the live Teranode fixture

- **Older job settled.** The worker kept job `6` after receiving job `7` for the same parent with `clean_jobs=false`. It solved the old template. Both Teranodes served the exact candidate containing only the original payment. The second payment remained available to the entrant. Two Teranode-mined successors put block `26343737e6fc1434b566effa631d6136b539fbef2dbfe043e92f87f8b72dc35d` at the local settlement endpoint.
- **A greater-work branch replaced the next solution.** Rust initially observed block `1ca0c9457cb22cebdbb8d5b4eb46fccce5010a77bbb7c8a8d04a64dc107972eb` at height 107. Its payment consumed a watched input. The reference miners subsequently agreed on the independently mined competing branch. The node restored the source to eligible, removed the detached output and retained 8 contiguous observed headers. The detached solution ended in `stale_at_depth`. Its local history records the earlier active observation. [Live proof and both branches](evidence/reliability-proof.json.gz) · [Fresh repeat](evidence/reliability-proof-repeat.json.gz)
- **Additional deterministic cases.** The Rust suite exercised short fork depths one through eight over 256 ordered and out-of-order delivery cases. A separate spend test checks paid-output removal, tombstone removal and re-admission. Another test checks that an eight-block replacement retains a full 147-header history.

## Expanded stale-risk comparison

Each role performed 1,415 SHA256d attempts. Scary Node had **176/717 (24.55%)** stale blocks and the full-state miner had **194/677 (28.66%)**. The difference was **−4.11 percentage points**, with a paired 95% interval of **−9.65 to +1.60 points**. The interval includes zero and allows an increase up to 1.60 points under this design. A second bootstrap that resamples whole sessions gives −11.11 to +3.05 points. Those intervals describe local fixture variability over eight fresh sessions.

| Seed | Simultaneous discoveries | Scary stale / found | Full-state stale / found | Difference (pp) |
| --- | ---: | ---: | ---: | ---: |
| 2701 | 48 | 23/97 (23.71%) | 25/79 (31.65%) | −7.93 |
| 2702 | 48 | 26/83 (31.33%) | 22/93 (23.66%) | +7.67 |
| 2703 | 53 | 30/95 (31.58%) | 23/86 (26.74%) | +4.83 |
| 2704 | 43 | 27/87 (31.03%) | 16/84 (19.05%) | +11.99 |
| 2705 | 42 | 18/85 (21.18%) | 24/85 (28.24%) | −7.06 |
| 2706 | 41 | 17/89 (19.10%) | 24/80 (30.00%) | −10.90 |
| 2707 | 48 | 18/86 (20.93%) | 30/90 (33.33%) | −12.40 |
| 2708 | 47 | 17/95 (17.89%) | 30/80 (37.50%) | −19.61 |
| Pooled | 370 | 176/717 (24.55%) | 194/677 (28.66%) | −4.11 |

The simultaneous-discovery denominator was 370. Scary lost 176 of those races and the full-state miner lost 194. The conditional difference was −4.86 points with an exact 95% interval of −15.24 to +5.59. 347 simultaneous pairs arrived in the same millisecond-resolution proxy bucket. The remaining 654 solo discoveries all survived.

The verifier reconstructed every recorded header attempt, payment and branch endpoint. Of the selected solutions, **541** originated from Rust over Stratum. All appear in the entrant’s local lifecycle and on the agreed ancestry of both Teranodes and the neutral judge at depth two. The original study’s +1.45-point first session remains one historical observation. The larger cohort supplies an updated estimate with more precision. It still starts with ready templates on a common parent and uses a single receiver for tie arbitration. The native branch proof and lifecycle measurements address separate operational risks. [Complete cohort and settlement telemetry](evidence/reliability-cohort.json.gz) · [Original 256-opportunity study](race-results.md)
