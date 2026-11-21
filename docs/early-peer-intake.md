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
