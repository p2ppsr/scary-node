# Recovering a witnessed boundary beyond the undo journal

Scary Node compares competing branches using verified proof of work. Within the available undo journal it rolls back the old outputs. A deeper fork can establish a fresh witnessed set when its replacement bodies connect to a known common ancestor, at least two replacement bodies link together, and their work exceeds the competing eligible suffix. Equal work keeps the incumbent. The node checks the replacement in an isolated state before installing it. Available common-prefix headers supply difficulty and median-time context. Output knowledge and spend coverage start at the first newly checked body. Mainnet mining waits for the full contemporary difficulty window to fill again.

A signed invalidation removes the named block and its known descendants from consideration. If the named block has left the undo journal, the node retains the signed action, receipt, active hash-to-height index and known forbidden descendants on disk. Work stops. Re-entry requires a verified replacement path to an observed ancestor strictly below the earliest invalidated height. If that ancestor precedes the last eligible parent, the replacement also has to overtake the intervening eligible work. Two high linked blocks with an unknown parent leave the established branch and work hold intact, with or without a signed invalidation. A restart checks the durable boundary and reapplies the directive.

## Operator procedure

1. Inspect `mining.readiness_reasons`, `tip_hash`, `height`, `recoverable_blocks`, `buffered_bodies`, `coverage_start` and `durable_directive_sequence` in the authenticated status endpoint.
2. Compare the signed root, sequence and saved application receipt with the receiver. Obtain the active reference tip and the hash of the last agreed eligible ancestor. Keep the signed history and checkpoint together when restarting or restoring.
3. Keep an independent stock data peer available to serve linked bodies. On reconnect or an unresolved gap, the SV transport sends a locator drawn from observed eligible headers. It checks that the response begins at an observed hash and that each subsequent header links and meets its advertised proof of work. It requests missing full bodies in groups of 24. The ordinary work comparison can retain up to 147 competing bodies, subject to the configured journal byte budget. Contextual checks run when the complete branch is connected. A later credible height keeps jobs withheld during partial catch-up. A signed alternative can also attach to the indexed last eligible parent. When the ancestor, intervening work or bodies cannot be established, retain the hold. Restore a verified checkpoint with the necessary ancestry if one is available.
4. After re-entry, check that coverage starts on the replacement branch, the directive sequence is durable and peer status confirms the selected parent. A wallet can offer a spend of an output witnessed on the recovered branch once its creating block has a successor.

## Reproduce the stock-node comparison

Use the release binary, Docker and the Python environment from the [Chronicle qualification guide](chronicle-qualification.md). The driver creates three fresh isolated SV Node v1.2.3 regtest stores. In the ordinary case it presents a twelve-block fork and checks the equal-work decision before the alternative overtakes it. In the signed case it delivers a three-signer type-seven alert, restarts the miner, and temporarily disconnects the reference while it continues mining the previously selected branch. The reference announces linked tips beyond the miner’s ancestry evidence before it switches to a valid alternative. The third case takes the entrant offline at a 32-block incumbent suffix and reconnects it to a stock peer serving a 34-block fork. The operator acknowledges the displaced fork through the stock node’s `ignoresafemodeforblock` RPC so its wallet can create the post-recovery payment. The RPC suppresses that fork’s stock wallet alarm. The signed case uses isolated lab keys. Each case concludes with an ordinary peer-delivered payment, a Stratum solution selected by the reference, a direct reference-mined successor and a second entrant restart.

```sh
.native/py/bin/python qualification/deep_recovery.py .native/deep-recovery.json
.native/py/bin/python qualification/verify_deep_recovery.py .native/deep-recovery.json
```

## Recorded results

The frozen `af9b605` release stayed at height 119 when the stock reference reached 121 on each twelve-block fork. The ordinary case reported missing newer bodies. The signed case preserved sequence 1 across restart and reported that the invalidated ancestry lay beyond its undo journal. Both withheld jobs. [Before-change snapshots](evidence/chronicle-deep-recovery-baseline.json)
