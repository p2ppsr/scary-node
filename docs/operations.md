# Operating the alpha

Loss of all eligible data-peer connections withdraws outstanding work. The node reconfirms the current tip after reconnecting. SV peers can return bounded recent headers and bodies from the restored contemporary boundary. The status reports `connected_data_peers` and `tip_confirmed_since_disconnect`.

Synchronize the host clock before starting the node. The status fields `clock_step_seconds` and `clock_step_limit_seconds` compare wall time against monotonic elapsed time since startup. A step exceeding 120 seconds in either direction withholds Stratum work and solved publication until the clock comes back into range. A failed durable checkpoint stops the process. Preserve the committed checkpoint and free storage before restarting. A leftover temporary snapshot can be ignored. [Measured clock and storage recovery](chronicle-results.md)

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

Mainnet Stratum requires an authenticated alert gateway and worker password. A mainnet operator API exposed beyond loopback requires a bearer token. The public Asset server is read-only. Use a trusted mining LAN or authenticated encrypted tunnel for plaintext Stratum. Issue distinct operator and worker credentials. The data-directory lock fences concurrent writers on the same filesystem. Distributed failover requires external fencing of the active identity and an intact extranonce watermark.

The body capacity defaults to 256 MiB. Set `SCARY_MAX_BLOCK_BYTES` according to host resources. `SCARY_MAX_TRANSACTION_BYTES` defaults to 16 MiB or the smaller body limit. `SCARY_MAX_WITNESSED_OUTPUTS` defaults to one million. An exhausted witnessed-output budget rolls back the body and withholds work. Increase capacity and restart to retry. The operator status reports the effective settings. The receiver keeps up to 1 MiB of a body in memory during transfer. Larger wire bodies spill into an anonymous file on the node data volume. Validation and the reorg journal share the completed read-only mapping. The snapshot embeds the body bytes for restore. Transaction indexes, witnessed scripts and undo entries still use memory. The recovery journal retains up to eight active bodies within its byte and item limits. A missing body or failed fetch interrupts work until coverage recovers. The node checkpoints its branch in the network data directory and verifies the checkpoint digest and retained bodies on restart. An explicit invalidation outside reversible ancestry withholds work. Inspect disk space, connectivity, signed sequence and selected parent before restarting a worker. Mainnet work needs 147 contiguous headers and at least one eligible spend.

Use `--sv-peer IP:PORT` to add an independent current-tip Bitcoin SV path. Mainnet body observation with no static SV peers uses the three SV Node v1.2.3 mainnet DNS seeds and up to four simultaneous discovery sessions. With explicit peers, `--sv-discovery` also enables rotating native SV paths. [SV Node DNS seeds](https://github.com/bitcoin-sv/bitcoin-sv/blob/6504a3aff65ba97c0f6c80962b033e35ecbfed4b/src/chainparams.cpp) Ordinary inputs need a witnessed creation and successor on the active branch or a validated in-template parent. BDK checks both policy and candidate consensus. Authorized confiscations are bound to exact bytes and height. Their recovered outputs can be spent 1,000 blocks after inclusion. [Build, receiver setup and operating contract](chronicle-qualification.md)

## Inspect what the entrant knows

`--min-observed-blocks N` sets the minimum number of contiguous observed bodies before Stratum issues work. It accepts 1 through 147 and defaults to 1. The realistic regtest sets it to 13. Mainnet still requires the complete difficulty window. A ready observation window by itself supplies neither a payout script nor eligible transactions.

`GET /status` exposes the recent `mining.validation_window`. Each row binds its counters to a height and hash. Look at `known_inputs`, `unknown_inputs`, `script_checks_passed`, `amount_checks_passed`, `transactions_determinate`, `transactions_deferred` and `coinbase_reward_checked`. A row with deferred inputs can still have successful checks for the inputs the node knows.

Use `GET /outpoints/<display-txid>/<vout>` to inspect a single outpoint in the local working set. Use `POST /check-transaction` with raw transaction bytes to assess a payment against that state. The response explains the determinate, deferred or invalid result. This read-only call leaves the candidate pool untouched. Submit work through `POST /transactions` when you want the miner to include it. All three endpoints belong on the operator’s trusted interface. [Lab and metric definitions](scenario.md)
