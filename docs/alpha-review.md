# November 22 alpha review

This review exercised the public node at runtime source `1ba337e5f630beb4fa943770a48a01ab03465781` on isolated regtest. Its focus was what happens to a real worker, peer or restarted process when the node’s evidence changes. Operator commands and expected responses are in the [two-Teranode lab](regtest.md) and [operating guide](operations.md).

| Behavior | Correction | How it was checked |
| --- | --- | --- |
| Restoring signed directives | A signed record must occupy its own numbered slot. A higher record beyond a gap holds mining until the receiver supplies a contiguous verified sequence. The adapter follows the opened log file across rotation and keeps delegated RPC credentials on their configured endpoint. | Misfiled records, gap and replay, rotated and partial log lines, and local HTTP adapter tests. The signed DAR lifecycle below exercised replay with a real receiver. |
| Issuing replacement work | If any issued template becomes ineligible, the next Stratum notification tells the worker to replace its work. The server still accepts valid submissions in flight and preserves duplicate-share checks. Ordinary same-parent refreshes keep valid jobs. | Session-state tests and stock-node trials with a signed freeze and a real Stratum solution. |
| Validating a regtest branch | When default Chronicle regtest rules protect an already-unspent outpoint, the check uses witnessed outputs from that block’s actual parent. | Rust tests cover candidate and block verification, later spends and competing branches. |
| Reading from peers | The progress deadline applies from the first byte of a partial SV header. A slow TCP reply releases the mining lock. Teranode ancestor repair can refetch a body evicted after its first delivery. | Local socket and ancestor-fetch regressions cover partial ordinary and large-message framing, slow progress and delivery deduplication. |
| Reopening a checkpoint | Startup distinguishes an absent store from an unreadable one, requires a single decoded checkpoint, and replaces a stale temporary path without truncating the old inode. | Rust storage regressions and a separate [stock-selected checkpoint upgrade](checkpoint-encoding-results.md#buffered-release-restart-and-stock-acceptance) identify their respective runtime versions. |

## What the reference nodes selected

**Ordinary transactions.** Scary Node, Rust BDK, exact-source GoBDK and SV Node v1.2.3 agreed on all 124 decisions for 62 funded cases. Stock SV Node admitted 46 to its mempool and accepted 50 under consensus. All 46 policy-eligible cases reached stock in Scary-originated Stratum blocks. [Funded transactions, node and oracle digests](evidence/alpha-chronicle-vectors.json.gz)

**Digital Asset Recovery.** The maintained receiver processed ten signed directives across all eight message types. The reference rejected the recovered owner’s spend at height 1,107 and selected it at 1,108, exactly 1,000 blocks after confiscation in block 108. It selected the spend again at 1,112 on the replacement branch. A fresh SV Node replayed all 1,115 active and three detached blocks and reached the identical final hash, `740f272cbbb3bfabe5e2fcc34e01c01fb1f7a565c3dd9c3a72bab0e6ede4a42f`. [Signed history and block bytes](evidence/alpha-dar-lifecycle.json.gz) · [Teranode comparison and its scope](teranode-alert-interoperability.md)

**Freeze and solution timing.** In the ordered controls the freeze-first submission produced no solved block, and the solution-first submission reached the stock tip before the freeze. Two further trials overlapped the worker submission with delivery of the alert sequence or the signed freeze bytes. Both durably applied the freeze, rejected the submission and recorded zero locally solved blocks. These observations cover the event orderings exercised. [Ordered controls](evidence/alpha-directive-order.json) · [Overlap receipts](evidence/alpha-directive-overlap.json)

**Two-Teranode propagation.** An empty entrant joined at 101. Both reference miners selected its block at 104 and served identical bytes. The entrant and both references then selected the same successor at 105. The receipt binds the Linux executable to the source inputs used to build it. [Receipt](evidence/chronicle-two-teranode-quickstart.json) · [Reproduce](regtest.md)

## Replay the evidence

From the repository root, run the following. The funded-vector and Teranode readers use the Python standard library. Install the contributor Python environment for the signed DAR and directive checks. Use a full-history Git checkout for the optional source-identity check.

```sh
python3 regtest/verify_receipt.py --check-source docs/evidence/chronicle-two-teranode-quickstart.json
python3 qualification/verify_entrant_chronicle.py docs/evidence/alpha-chronicle-vectors.json.gz
.native/py/bin/python qualification/verify_dar_history.py docs/evidence/alpha-dar-lifecycle.json.gz
.native/py/bin/python qualification/verify_directive_overlap.py docs/evidence/alpha-directive-overlap.json \
  --ordered docs/evidence/alpha-directive-order.json
```

To repeat the stock-node replay, add `--replay-sv` to the DAR verifier with Docker running. To generate new transactions and block hashes, follow the [fresh Chronicle and receiver setup](chronicle-qualification.md) and [timing test](directive-overlap.md). The Chronicle, DAR and overlap tests used a macOS ARM64 binary with SHA-256 `e01e37f5876fc7056adc0858aba99c98645c8badb35cd6c00ae85b313370a77e`. The two-Teranode lab used the Linux ARM64 binary identified in its receipt. Each result applies to the named release, inputs and peer implementations.
