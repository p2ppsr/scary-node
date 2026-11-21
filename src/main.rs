mod difficulty;
mod mining;
mod peer;

use anyhow::{bail, ensure, Context, Result};
use axum::{
    body::Bytes,
    extract::{DefaultBodyLimit, Path, State},
    http::StatusCode,
    routing::{get, post},
    Json, Router,
};
use clap::{Parser, ValueEnum};
use serde_json::{json, Value};
use std::{collections::BTreeSet, net::SocketAddr, path::PathBuf, sync::Arc};
use tokio::sync::{mpsc, Mutex};

#[derive(Copy, Clone, Debug, ValueEnum)]
enum Network {
    Mainnet,
    Regtest,
}
impl Network {
    fn name(self) -> &'static str {
        match self {
            Self::Mainnet => "mainnet",
            Self::Regtest => "regtest",
        }
    }
}

#[derive(Parser, Debug)]
#[command(
    version,
    about = "Listen to native Teranode peers and mine witnessed BSV outputs"
)]
struct Args {
    #[arg(long, value_enum, default_value = "mainnet")]
    network: Network,
    /// Explicit libp2p peer multiaddr including /p2p/<peer-id>. Repeat for additional peers.
    #[arg(long)]
    peer: Vec<String>,
    /// Native P2P listen multiaddr.
    #[arg(long, default_value = "/ip4/0.0.0.0/tcp/0")]
    listen: String,
    /// Base directory for the persistent peer identity (one subdirectory per network).
    #[arg(long, default_value = ".scary-node")]
    data_dir: PathBuf,
    /// Retrieve complete block bodies to build the witnessed working set.
    #[arg(long)]
    observe_bodies: bool,
    /// Minimum contiguous observed block bodies before issuing mining work (1..147).
    #[arg(long, default_value_t = 1, value_parser = clap::value_parser!(u32).range(1..=147))]
    min_observed_blocks: u32,
    /// Trusted operator HTTP endpoint.
    #[arg(long, default_value = "127.0.0.1:8080")]
    api_bind: SocketAddr,
    /// Bind address for serving public block and subtree assets.
    #[arg(long, requires = "asset_url")]
    asset_bind: Option<SocketAddr>,
    /// Reachable base URL advertised for local assets, ending in /api/v1.
    #[arg(long, requires = "asset_bind")]
    asset_url: Option<String>,
    /// Enable Stratum V1 on this trusted interface.
    #[arg(long, requires_all = ["payout_script", "asset_bind", "asset_url"])]
    stratum_bind: Option<SocketAddr>,
    /// Hex-encoded output locking script for the coinbase.
    #[arg(long)]
    payout_script: Option<String>,
}

#[derive(Default)]
struct PeerStats {
    seen: BTreeSet<String>,
    statuses: u64,
    notices: u64,
    subtrees: u64,
    gaps: u64,
    last_status: Option<Value>,
    last_block: Option<Value>,
    last_published: Option<String>,
}

#[derive(Clone)]
struct App {
    network: &'static str,
    mining: Arc<Mutex<mining::Mining>>,
    peer: Arc<Mutex<PeerStats>>,
}

async fn status(State(app): State<App>) -> Json<Value> {
    let mining = app.mining.lock().await.status();
    let p = app.peer.lock().await;
    Json(json!({
        "network":app.network, "mining":mining, "peer":{
            "connected_peer_ids_seen":p.seen.len(), "status_messages":p.statuses,
            "block_notices":p.notices, "subtree_notices":p.subtrees, "coverage_gaps":p.gaps,
            "last_peer_status":p.last_status, "last_block_notice":p.last_block,
            "last_published":p.last_published
        }
    }))
}

async fn submit(State(app): State<App>, raw: Bytes) -> (StatusCode, Json<Value>) {
    match app.mining.lock().await.submit_tx(&raw) {
        Ok(txid) => (StatusCode::ACCEPTED, Json(json!({"txid":txid}))),
        Err(err) => (
            StatusCode::UNPROCESSABLE_ENTITY,
            Json(json!({"error":err.to_string()})),
        ),
    }
}

async fn outpoint(
    State(app): State<App>,
    Path((id, index)): Path<(String, u32)>,
) -> (StatusCode, Json<Value>) {
    match app.mining.lock().await.outpoint(&id, index) {
        Ok(result) => (StatusCode::OK, Json(result)),
        Err(err) => (
            StatusCode::BAD_REQUEST,
            Json(json!({"error":err.to_string()})),
        ),
    }
}

async fn check_transaction(State(app): State<App>, raw: Bytes) -> (StatusCode, Json<Value>) {
    match app.mining.lock().await.check_transaction(&raw) {
        Ok(result) => (StatusCode::OK, Json(result)),
        Err(err) => (
            StatusCode::UNPROCESSABLE_ENTITY,
            Json(json!({"error":err.to_string()})),
        ),
    }
}

fn log(value: Value) {
    println!("{value}");
}

async fn consume(app: App, mut events: mpsc::Receiver<peer::Event>) -> Result<()> {
    use peer::Event;
    while let Some(event) = events.recv().await {
        match event {
            Event::Connected(id) => {
                let mut p = app.peer.lock().await;
                if p.seen.len() < 4096 {
                    p.seen.insert(id.clone());
                }
                log(json!({"event":"peer_connected","peer":id}));
            }
            Event::Status { peer, height, hash } => {
                app.mining.lock().await.announced_height(height);
                let v = json!({"event":"peer_status","peer":peer,"height":height,"hash":hash});
                let mut p = app.peer.lock().await;
                p.statuses += 1;
                p.last_status = Some(v.clone());
                log(v);
            }
            Event::Notice { peer, height, hash } => {
                app.mining.lock().await.announced_height(height);
                let v = json!({"event":"block_notice","peer":peer,"height":height,"hash":hash});
                let mut p = app.peer.lock().await;
                p.notices += 1;
                p.last_block = Some(v.clone());
                log(v);
            }
            Event::Subtree { peer, hash } => {
                app.peer.lock().await.subtrees += 1;
                log(json!({"event":"subtree_notice","peer":peer,"hash":hash}));
            }
            Event::Block {
                height,
                raw,
                validated,
            } => {
                let mut m = app.mining.lock().await;
                let result = m.observe(height, &raw);
                let _ = validated.send(result.is_ok());
                match result {
                    Ok(()) => log(json!({"event":"body_observed","height":height})),
                    Err(err) => {
                        drop(m);
                        app.peer.lock().await.gaps += 1;
                        log(
                            json!({"event":"coverage_reset","height":height,"reason":err.to_string()}),
                        );
                    }
                }
            }
            Event::Gap(reason) => {
                app.mining.lock().await.suspend();
                app.peer.lock().await.gaps += 1;
                log(json!({"event":"coverage_reset","reason":reason}));
            }
            Event::Published { hash } => {
                app.peer.lock().await.last_published = Some(hash.clone());
                log(json!({"event":"gossip_published","hash":hash}));
            }
        }
    }
    bail!("peer event stream closed")
}

#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();
    let network = args.network.name();
    let regtest = matches!(args.network, Network::Regtest);
    ensure!(
        !regtest || !args.peer.is_empty(),
        "regtest requires an explicit --peer"
    );
    if let Some(ref url) = args.asset_url {
        let u = reqwest::Url::parse(url).context("invalid --asset-url")?;
        ensure!(
            matches!(u.scheme(), "http" | "https")
                && u.host().is_some()
                && u.path().trim_end_matches('/') == "/api/v1"
                && u.query().is_none()
                && u.fragment().is_none()
                && u.username().is_empty()
                && u.password().is_none(),
            "--asset-url must be an HTTP(S) base URL ending in /api/v1"
        );
    }
    let payout = args
        .payout_script
        .as_deref()
        .map(hex::decode)
        .transpose()
        .context("invalid hex in --payout-script")?
        .unwrap_or_default();
    ensure!(
        args.stratum_bind.is_none() || !payout.is_empty(),
        "mining requires a payout script"
    );
    let mut mining = mining::Mining::new(regtest, payout)?;
    mining.set_min_observed_blocks(args.min_observed_blocks as usize)?;
    let app = App {
        network,
        mining: Arc::new(Mutex::new(mining)),
        peer: Arc::new(Mutex::new(PeerStats::default())),
    };
    let api = tokio::net::TcpListener::bind(args.api_bind)
        .await
        .context("operator API bind")?;
    let router = Router::new()
        .route("/status", get(status))
        .route("/outpoints/{id}/{index}", get(outpoint))
        .route(
            "/check-transaction",
            post(check_transaction).layer(DefaultBodyLimit::max(100_000)),
        )
        .route("/transactions", post(submit))
        .layer(DefaultBodyLimit::max(4_000_000))
        .with_state(app.clone());
    let dir = args.data_dir.join(network);
    tokio::fs::create_dir_all(&dir).await?;
    let fetch_bodies = args.observe_bodies || args.stratum_bind.is_some();
    let cfg = peer::Config {
        network: network.into(),
        peers: args.peer,
        listen: args.listen,
        key_file: dir.join("peer.key"),
        asset_bind: args.asset_bind,
        asset_url: args.asset_url,
        fetch_bodies,
    };
    let (events, stream) = mpsc::channel(16);
    let (solutions, solved) = mpsc::channel(4);
    let mut tasks = tokio::task::JoinSet::new();
    tasks.spawn(peer::run(cfg, events, solved));
    tasks.spawn(consume(app.clone(), stream));
    tasks.spawn(async move { axum::serve(api, router).await.context("operator API") });
    if let Some(bind) = args.stratum_bind {
        tasks.spawn(mining::run_stratum(bind, app.mining, solutions.clone()));
    }
    // Keep the solution channel open in listen-only mode.
    log(
        json!({"event":"started","network":network,"observe_bodies":fetch_bodies,
               "api":args.api_bind,"stratum":args.stratum_bind}),
    );
    tokio::select! {
        _ = tokio::signal::ctrl_c() => {},
        result = tasks.join_next() => {
            result.context("no services running")?.context("service task terminated")??;
            bail!("service stopped unexpectedly");
        }
    }
    tasks.abort_all();
    Ok(())
}
