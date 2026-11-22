# A payment received before its input has a successor

Scary Node requires a successor to the creating block before it will mine an ordinary spend of a witnessed output. This experiment tests what happens to a signed payment already received over SV P2P when that successor arrives.

A stock SV Node v1.2.3 regtest wallet funds an ordinary pay-to-public-key output. Its block reaches the entrant before the wallet submits the signed child to stock. Stock remains the entrant's only SV peer. The driver records the stock peer's outgoing transaction byte counter and the entrant's intake counters. There is one wallet submission of the child. An authorized worker listens for work while the input has no successor. The stock miner then produces a valid coinbase-only successor, keeping the payment in its mempool. The driver reads stock's transaction byte counter again to distinguish admission of the original offer from a fresh delivery. This counter includes the 24-byte SV message header.

The offline verifier checks the funding and successor Merkle roots, proof of work, the exact previous outpoint and value, and the ECDSA signature against the public key in the funding script. For a selected block it also checks the worker's header, fee, payout and the stock-mined direct successor.

~~~sh
.native/py/bin/python qualification/sv_transaction_intake.py .native/early-baseline.json --early baseline --binary /path/to/frozen/8c41/scary-node
.native/py/bin/python qualification/sv_transaction_intake.py .native/early-selected.json --early reconsidered --binary target/release/scary-node
.native/py/bin/python qualification/verify_peer_intake.py .native/early-baseline.json .native/early-selected.json
~~~

On the frozen recovery build (8c41f89), the payment was 200 bytes and stock's counter moved from zero to 224 bytes. The counter stayed at 224 after the empty successor. Scary recorded one ineligible offer and zero pending transactions, and the authorized worker received zero notifications on each side of the successor. The payment remained in the stock mempool. [Frozen baseline receipt](evidence/chronicle-early-sv-offer-baseline.json)

The archive-built macOS executable from [c2a36d5](https://github.com/p2ppsr/scary-node/commit/c2a36d5c6a83f2262dffd596f5eecb421db6b132) retained one 200-byte payment while the worker received zero notifications. When the empty successor arrived, the held count fell to zero, the promoted count increased by one, and one transaction became eligible for the worker. Stock’s TX byte counter stayed at 224 through selection of the worker’s block. The stock-selected block collected 5,000,001,100 satoshis in its coinbase, including the 1,100-satoshi fee. Stock mined its direct successor. The recorded executable SHA-256 is `da0703697a1974574fddcbf54d5c1dfcec74f53bb93313a90b352a3ad764e882`. Each run used fresh spending keys and its own regtest chain. These observations cover continuous operation between the offer and its successor. [Selected solution and native intake counters](evidence/chronicle-early-sv-offer-selected.json)

Both archive-built release manifests record the native BDK provenance, compiler and executable SHA-256. [macOS build](evidence/chronicle-waiting-macos-build.json) · [Linux build](evidence/chronicle-waiting-linux-build.json)

## Restart before the funding block has a successor

Each run starts two independent stock SV Node v1.2.3 instances. Only the first receives the signed wallet payment. After stopping the entrant with SIGINT or SIGTERM, the driver reconnects it exclusively to the second stock node. Both stock nodes validated the funding block. The second has an empty mempool and sends zero transaction bytes during the measurement. It mines the first successor to the funding block. This isolates the payment the entrant saved before shutdown.

| Payment at shutdown | Source | Runs | Waiting after restart | Selected and extended by stock | Fee per selected block |
|---|---|---:|---:|---:|---:|
| Waiting for a successor | `10b93b4` | 2 | 0 of 2 | 0 of 2 | — |
| Waiting for a successor | `5148c31` | 2 | 2 of 2 | 2 of 2 | 1,100 sat |
| Already eligible | `5148c31` | 2 | — | 2 of 2 | 1,100 sat |

On both release runs with a waiting payment, the worker received zero jobs before the successor. Scary then promoted precisely one payment, and stock accepted the worker’s block and mined directly on top of it. Each signal used a fresh chain and wallet. These six experiments cover graceful stops and a payment offered once. [Original waiting runs](evidence/chronicle-waiting-shutdown-original.json) · [Restored waiting runs](evidence/chronicle-waiting-shutdown-fixed.json) · [Eligible controls](evidence/chronicle-waiting-shutdown-eligible.json) · [Release build](evidence/chronicle-waiting-shutdown-macos-build.json)

Replay all six observations offline. To repeat them live, set `OLD_BINARY` and `SCARY_BINARY` to the respective archive-built executables and use fresh output paths.

~~~sh
p=.native/py/bin/python
receipts=docs/evidence/chronicle-waiting-shutdown
"$p" qualification/verify_pending_shutdown.py "$receipts-original.json" --baseline
"$p" qualification/verify_pending_shutdown.py "$receipts-fixed.json"
"$p" qualification/verify_pending_shutdown.py "$receipts-eligible.json"

"$p" qualification/pending_shutdown.py "$OLD_BINARY" .native/old-waiting.json --baseline --awaiting-successor
"$p" qualification/pending_shutdown.py "$SCARY_BINARY" .native/restored-waiting.json --awaiting-successor
"$p" qualification/pending_shutdown.py "$SCARY_BINARY" .native/restored-eligible.json
~~~

## Apply a freeze issued during a controlled stop

The regression uses two isolated stock SV Node v1.2.3 instances and one Scary data directory. The first stock peer sends a signed ordinary payment after its funding block reaches the entrant. Scary receives SIGTERM before any successor arrives. While Scary is down, three distinct disposable signers authorize a freeze of that funding outpoint. The fixture applies the corresponding consensus blacklist interval to the second stock node. That node has validated the same 106 blocks and has an empty mempool when Scary reconnects.

The second stock node mines the funding block’s first successor while the freeze is in force. An authorized Stratum worker then listens for work. A second signed message gives the freeze an exclusive stop at the next candidate height with policy expiry enabled. The fixture reoffers the exact original transaction to the second stock node. The recorded stock TX byte counts distinguish that fresh relay from reconsideration of the saved offer. The exercise uses an authenticated lab feed with disposable public keys. Its receipt includes both signed envelopes, the entrant’s durable application results, the independent stock RPC dispositions and the wire data for verifying funding, selection and extension.

On release `5148c31`, the first peer sent one 224-byte TX frame. SIGTERM preserved its 200-byte payment in the waiting queue and zero payments in the eligible pool. On restart, Scary durably applied the signed freeze at height 106 and discarded exactly that saved SV offer. After the height-107 successor, the authorized worker received zero job notifications. Stock rejected the payment with `bad-txns-inputs-frozen`. Its outgoing TX byte count stayed at zero through the signed unfreeze, and Scary still had zero eligible payments. A fresh 224-byte native relay then supplied the same payment. Stock selected Scary’s height-108 block containing that payment and its 1,100-satoshi fee, and mined its direct successor `162b87e564efebed4a03d73a6595001f852dc37f3971c7caefcaea0991bf385f`. [Signed freeze, stock wire and worker receipt](evidence/chronicle-waiting-freeze.json.gz)

Replay the published receipt with the project’s qualification Python dependencies. For a fresh run, supply the archive-built macOS executable identified by the manifest and a new local receipt path.

~~~sh
manifest=docs/evidence/chronicle-waiting-shutdown-macos-build.json
.native/py/bin/python qualification/waiting_freeze.py verify "$manifest" docs/evidence/chronicle-waiting-freeze.json.gz
.native/py/bin/python qualification/waiting_freeze.py run "$SCARY_BINARY" "$manifest" .native/waiting-freeze-fresh.json.gz
~~~

## Ordered subtree fetches on a stock Teranode

On regtest, a fresh stock Teranode v0.16.1 received a signed payment from its wallet RPC and announced the subtree over native gossip. Scary Node `9901c89` received the 191-byte subtree with four fetch workers and a 256-root waiting queue. Its completion event contained the exact advertised root and one payment. The SV intake count stayed at zero. On the same parent, a Stratum worker solved Scary's candidate. Stock selected block `7fb9ca4bc770466f7c4a59c8c7b27fb6c3a813ed5ec7e048a08d98943073f6c9`, which included the payment and its 1,000-satoshi fee, and mined direct successor `4f672bfd55a68ce0660a6e6995eaa7cf006e13eaed5cdcc0eeac197dc3900ee4`. The receipt contains the creating block, subtree bytes, intake counters, solved block and successor header. The offline verifier checks the payment signature against its witnessed funding output, both Merkle commitments, coinbase amount and proof of work. [Stock run and exact bytes](evidence/chronicle-ordered-subtree-stock.json) · [Controlled scheduler trials](subtree-scheduler-study.md)

Build the release and install the qualification Python dependencies. Run the live fixture with a new output path or verify the published receipt offline.

~~~sh
.native/py/bin/python qualification/teranode_subtree_intake.py \
  --binary "$PWD/target/release/scary-node" .native/ordered-subtree.json
.native/py/bin/python qualification/verify_peer_intake.py \
  docs/evidence/chronicle-ordered-subtree-stock.json
~~~
