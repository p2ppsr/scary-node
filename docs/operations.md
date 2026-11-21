# Operating the alpha

The default network is mainnet. The default API listens only on loopback. The peer identity persists in the data directory. Store that directory privately. Run one identity per node. A fresh identity has no special admission rights. Operators of other peers choose which announcements and assets they will consume.

| Option | Purpose |
| --- | --- |
| --network mainnet or regtest | Selects the BSV network, peer namespace and difficulty rule. |
| --peer MULTIADDR | Repeated static peers. Mainnet uses DNS bootstrap when this is omitted. Regtest uses explicit peers. |
| --listen MULTIADDR | Local libp2p bind address. |
| --data-dir PATH | Directory for the local peer identity. |
| --observe-bodies | Fetch available full block bodies and build the working set. Mining implies body observation. |
| --api-bind IP:PORT | Status and raw transaction intake. Default 127.0.0.1:8080. Keep this on a trusted interface. |
| --asset-bind IP:PORT and --asset-url URL | Bind the Asset service and advertise its externally reachable base including /api/v1. Required for mining. |
| --stratum-bind IP:PORT and --payout-script HEX | Serve mining work and specify the coinbase payout locking script. Enable only on a trusted network. |

GET /status reports observations and the local mining state. POST /transactions accepts raw BSV transaction bytes. Rejected transactions return a reason. The initial intake supports ordinary signed, final P2PKH transactions and requires witnessed unspent inputs on the current branch. The node does not hold wallet keys or create a funding payment for you.

Stratum uses plain TCP, a worker name as its label, and a conventional placeholder password such as x. Worker labels identify connections and work. Restrict the Stratum endpoint to trusted miners at the network boundary. Inspect job IDs, accepted shares, network solutions and the downstream block status separately. The HTTP intake also belongs on a trusted interface. Only the read-only Asset service and native P2P need to be reachable by remote peers when publishing.

A mainnet listener may wait for new announcements. A body observer can wait for data availability. Mining can wait for a complete difficulty window, a successor to the funding block and an eligible spend. A clean restart learns outputs anew. Use an ordinary wallet for funding, and reserve its signed child for this experiment. The host’s resource limit determines which block sizes the alpha can process. Rejections and missing data are evidence of incomplete coverage.

The first release caps an observed body at 64 MiB, tracks at most 100,000 P2PKH outputs and accepts up to 128 pending transactions with a 4 MB transaction-data budget. These are local capacity controls. Mainnet mining needs 147 contiguous headers for the modern difficulty rule. The job service waits for at least one eligible spend. Mainnet shares use a fixed difficulty of 65,536. Regtest shares use its short target. The asset-serving window holds recent locally mined data; it is not a public archive.

Use final version-one transactions with zero locktime and final sequence numbers. Every input must spend a witnessed P2PKH output with a compressed public key and an `ALL | FORKID` signature. Every output must use P2PKH, and input value must exceed output value. The intake returns the admitted transaction ID. A higher advertised tip retires the previous work until its body arrives. During that interval `/status` reports `mining.awaiting_height`. A gap clears the working set and pending transactions. A worker reconnects when its former job is retired without a replacement.

## Inspect what the entrant knows

`--min-observed-blocks N` sets the minimum number of contiguous observed bodies before Stratum issues work. It accepts 1 through 147 and defaults to 1. The realistic regtest sets it to 13. Mainnet still requires the complete difficulty window. A ready observation window by itself supplies neither a payout script nor eligible transactions.

`GET /status` exposes the recent `mining.validation_window`. Each row binds its counters to a height and hash. Look at `known_inputs`, `unknown_inputs`, `script_checks_passed`, `amount_checks_passed`, `transactions_determinate`, `transactions_deferred` and `coinbase_reward_checked`. A row with deferred inputs can still have successful checks for the inputs the node knows.

Use `GET /outpoints/<display-txid>/<vout>` to inspect a single outpoint in the local working set. Use `POST /check-transaction` with raw transaction bytes to assess a payment against that state. The response explains the determinate, deferred or invalid result. This read-only call leaves the candidate pool untouched. Submit work through `POST /transactions` when you want the miner to include it. All three endpoints belong on the operator’s trusted interface. [Lab and metric definitions](scenario.md)
