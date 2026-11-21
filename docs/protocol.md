# Wire protocol and mining path

The reference target is Teranode v0.16.1. Scary Node uses Rust libp2p to connect using native peer IDs, Identify and signed GossipSub on the network-qualified status, block and subtree topics. A block announcement gives a header and a data service from which the body can be retrieved. The operator’s stable identity belongs to the peer service. It is independent of the payout script.

In observation mode the node records peer notices. When processing bodies it obtains the ordinary serialized BSV block from the peer’s Asset service, binds the header to the announcement, parses transactions and checks the Merkle root and PoW. At publication it keeps the native block and subtree representations retrievable at its own Asset URL before announcing their hashes. A local publish event and acceptance by an independent reference node are separate results. The regtest procedure checks both through the final block hash.

The Stratum V1 job commits to a previous hash, coinbase prefix and suffix, assigned extranonce space, ordered Merkle branches, block version, target bits and time. A worker subscribes, authorizes with a label, receives difficulty and work, and submits an extranonce, timestamp and nonce. Scary Node rebuilds and checks the header. A share that meets the job’s share target records worker effort. A network-target solution triggers block publication. A tip or job replacement invalidates stale work.

The Rust BSV SDK supplies transaction serialization and hashing utilities. Ordinary scripts use the pinned BDK interpreter. The SV TCP transport receives contemporary blocks, retrieves recent missing ancestors and delivers verified local solutions independently from Teranode. [Mixed peer proof](evidence/chronicle-mixed-peers.json) Current-mainnet mining uses the post-fork 144-block chainwork difficulty rule and enough header ancestry to select the endpoint medians. Regtest uses its fixed target. The alpha starts on contemporary BSV and does not implement a historical consensus replay. [Teranode release](https://github.com/bsv-blockchain/teranode/tree/807f1bea39f52797c72fd157a543e8bb64622510) · [Teranode block assets](https://github.com/bsv-blockchain/teranode/blob/807f1bea39f52797c72fd157a543e8bb64622510/docs/howto/miners/minersHowToInteractWithAssetServer.md) · [BSV Rust SDK](https://github.com/bsv-blockchain/rs-sdk)

## Following a block through the implementation

| Step | Source | Evidence |
| --- | --- | --- |
| Discover peers and receive a signed announcement | [Peer service](../src/peer.rs) | Identified peer ID, topic, announced height and hash |
| Retrieve the advertised body | [Asset client](../src/peer/fetch.rs) | Header hash and delivered serialized bytes |
| Connect the body and update the watched outputs | [Mining state](../src/mining.rs) and [wire parser](../src/mining/wire.rs) | PoW, linkage, Merkle root, ordered input consumption and output creation |
| Admit a transaction | [Operator HTTP API](../src/main.rs) and [mining state](../src/mining.rs) | Outpoint, amount, signature, fee and resulting txid |
| Construct and solve a candidate | [Difficulty](../src/difficulty.rs) and [Stratum](../src/mining/stratum.rs) | Job ID, extranonces, parent, coinbase, Merkle branches, bits, timestamp and nonce |
| Serve the result and check independent acceptance | [Native assets](../src/peer/assets.rs) and [regtest driver](../regtest/prove.py) | Served block and subtree representations, two active-chain receipts and a linked successor |
