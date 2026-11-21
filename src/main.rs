mod alerts;
mod difficulty;
mod mining;
mod peer;
mod sv_peer;

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
    /// Current-tip SV Node TCP peer. Repeat for independent paths.
    #[arg(long)]
    sv_peer: Vec<SocketAddr>,
    /// Native P2P listen multiaddr.
    #[arg(long, default_value = "/ip4/0.0.0.0/tcp/0")]
    listen: String,
    /// Base directory for the persistent peer identity (one subdirectory per network).
    #[arg(long, default_value = ".scary-node")]
    data_dir: PathBuf,
    /// Local maintained Alert System API base, protected by a bearer-authenticated gateway.
    #[arg(long, requires = "alert_token_file")]
    alert_service_url: Option<String>,
    #[arg(long, requires = "alert_service_url")]
    alert_token_file: Option<PathBuf>,
    /// Isolated regtest bootstrap key set (JSON array of five compressed public keys).
    #[arg(long, requires = "alert_service_url")]
    regtest_alert_keys: Option<PathBuf>,
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
    /// File containing the operator API bearer token. Required for a mainnet non-loopback API.
    #[arg(long)]
    operator_token_file: Option<PathBuf>,
    /// File containing the Stratum worker password. Required for mainnet mining.
    #[arg(long, requires = "stratum_bind")]
    worker_password_file: Option<PathBuf>,
    /// Append unsigned candidate decisions after each new active tip. Incompatible with Stratum.
    #[arg(long, conflicts_with = "stratum_bind", requires = "payout_script")]
    shadow_log: Option<PathBuf>,
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
    tips: std::collections::BTreeMap<String, Value>,
    last_block: Option<Value>,
    last_published: Option<String>,
}

#[derive(Clone)]
struct App {
    network: &'static str,
    mining: Arc<Mutex<mining::Mining>>,
    peer: Arc<Mutex<PeerStats>>,
    shadow: Option<Arc<std::sync::Mutex<std::fs::File>>>,
    operator_token: Option<Arc<str>>,
}

fn secret_file(path: Option<&PathBuf>) -> Result<Option<Arc<str>>> {
    let Some(path) = path else {
        return Ok(None);
    };
    let raw = std::fs::read_to_string(path)?;
    let value = raw.trim_end_matches(['\r', '\n']);
    ensure!(
        !value.is_empty() && !value.chars().any(char::is_control),
        "invalid credential file"
    );
    Ok(Some(Arc::from(value)))
}

async fn authenticate(
    State(token): State<Option<Arc<str>>>,
    req: axum::extract::Request,
    next: axum::middleware::Next,
) -> axum::response::Response {
    use axum::response::IntoResponse;
    if let Some(expected) = token {
        let actual = req
            .headers()
            .get("authorization")
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.strip_prefix("Bearer "));
        if !actual.is_some_and(|v| credentials_match(v, &expected)) {
            return StatusCode::UNAUTHORIZED.into_response();
        }
    }
    next.run(req).await
}

pub(crate) fn credentials_match(actual: &str, expected: &str) -> bool {
    use sha2::{Digest, Sha256};
    use subtle::ConstantTimeEq;
    bool::from(Sha256::digest(actual.as_bytes()).ct_eq(&Sha256::digest(expected.as_bytes())))
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

async fn alert_receipt(State(app): State<App>, Json(call): Json<Value>) -> Json<Value> {
    let m = app.mining.lock().await;
    let result = match call["method"].as_str() {
        Some("getbestblockhash") => m
            .tip()
            .map(|h| json!(mining::wire::display_hash(h)))
            .context("awaiting first contemporary tip"),
        Some(method) => m.directive_receipt(method, &call["params"]),
        None => Err(anyhow::anyhow!("missing RPC method")),
    };
    Json(match result {
        Ok(result) => json!({"result":result,"error":null,"id":call["id"]}),
        Err(e) => {
            json!({"result":null,"error":{"code":-1,"message":e.to_string()},"id":call["id"]})
        }
    })
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
            Event::Delivery {
                peer,
                hash,
                direction,
            } => {
                let mut s = app.mining.lock().await;
                if s.record_delivery(&hash, &peer, direction) {
                    s.save()?;
                }
            }
            Event::Status { peer, height, hash } => {
                {
                    let mut s = app.mining.lock().await;
                    if s.reported_tip(&peer, &hash, height) {
                        s.save()?;
                    }
                }
                let v = json!({"event":"peer_status","peer":peer,"height":height,"hash":hash});
                let mut p = app.peer.lock().await;
                if p.tips.contains_key(&peer) || p.tips.len() < 128 {
                    p.tips
                        .insert(peer.clone(), json!({"height":height,"hash":hash}));
                }
                p.statuses += 1;
                p.last_status = Some(v.clone());
                log(v);
            }
            Event::Notice {
                peer,
                height,
                hash,
                header,
            } => {
                app.mining.lock().await.announced_header(height, &header);
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
                source,
                height,
                raw,
                validated,
            } => {
                let mut m = app.mining.lock().await;
                let previous = m.tip();
                let result = m.observe(height, &raw);
                m.save().context("committing witnessed branch")?;
                if result.is_ok() && m.tip() != previous {
                    if let Some(ref file) = app.shadow {
                        use std::io::Write;
                        let mut row = m.shadow_decision()?;
                        row["event"] = json!("tip");
                        row["delivery_source"] = json!(source);
                        row["reported_tips"] = json!(app.peer.lock().await.tips);
                        row["receipt_trigger_hash"] = json!(mining::wire::display_hash(
                            bsv::primitives::hash::hash256(&raw[..80])
                        ));

                        let mut file = file
                            .lock()
                            .map_err(|_| anyhow::anyhow!("shadow journal unavailable"))?;
                        serde_json::to_writer(&mut *file, &row)?;
                        file.write_all(b"\n")?;
                        file.sync_data()?;
                    }
                }
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
                {
                    let mut m = app.mining.lock().await;
                    m.wait_for_body(reason.clone());
                    m.save()?;
                }
                app.peer.lock().await.gaps += 1;
                log(json!({"event":"coverage_reset","reason":reason}));
            }
            Event::PublicationFailed { hash, error } => {
                {
                    let mut s = app.mining.lock().await;
                    s.publication(&hash, Some(error.clone()));
                    s.save()?;
                }
                log(json!({"event":"publication_failed","hash":hash,"reason":error}));
            }
            Event::Published { hash } => {
                {
                    let mut m = app.mining.lock().await;
                    m.publication(&hash, None);
                    m.save()?;
                }
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
        !regtest || !args.peer.is_empty() || !args.sv_peer.is_empty(),
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
    let dir = args.data_dir.join(network);
    tokio::fs::create_dir_all(&dir).await?;
    let lock = std::fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .open(dir.join("node.lock"))?;
    lock.try_lock().context("node identity already in use")?;
    ensure!(
        regtest || args.regtest_alert_keys.is_none(),
        "mainnet alert keys are fixed"
    );
    ensure!(
        regtest || args.stratum_bind.is_none() || args.alert_service_url.is_some(),
        "mainnet mining requires the Alert System"
    );
    let mut mining = mining::Mining::new(regtest, payout)?;
    if args.alert_service_url.is_some() {
        let keys = match &args.regtest_alert_keys {
            Some(path) => serde_json::from_slice(&std::fs::read(path)?)?,
            None if regtest => bail!("supply isolated regtest public keys"),
            None => alerts::MAIN_KEYS.map(String::from).to_vec(),
        };
        mining.directives = alerts::Book::new(&keys, true)?;
    }
    mining.set_min_observed_blocks(args.min_observed_blocks as usize)?;
    mining.open_store(&dir)?;
    if args.alert_service_url.is_some() {
        alerts::recover(&dir.join("alerts"), &mut mining)?;
    }
    ensure!(
        regtest || args.api_bind.ip().is_loopback() || args.operator_token_file.is_some(),
        "non-loopback mainnet operator API needs a token"
    );
    ensure!(
        regtest || args.stratum_bind.is_none() || args.worker_password_file.is_some(),
        "mainnet Stratum needs a worker password"
    );
    let worker_password = secret_file(args.worker_password_file.as_ref())?;
    let shadow = args
        .shadow_log
        .as_ref()
        .map(|p| {
            std::fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(p)
        })
        .transpose()?
        .map(|f| Arc::new(std::sync::Mutex::new(f)));
    let app = App {
        shadow,
        operator_token: secret_file(args.operator_token_file.as_ref())?,
        network,
        mining: Arc::new(Mutex::new(mining)),
        peer: Arc::new(Mutex::new(PeerStats::default())),
    };
    if let Some(file) = &app.shadow {
        use std::io::Write;
        let m = app.mining.lock().await;
        let mut file = file
            .lock()
            .map_err(|_| anyhow::anyhow!("shadow journal unavailable"))?;
        serde_json::to_writer(
            &mut *file,
            &json!({"event":"start","network":network,"at":mining::now()?,"restored_tip":m.tip().map(mining::wire::display_hash),"binary_version":env!("CARGO_PKG_VERSION"),"interpreter":m.status()["interpreter"]}),
        )?;
        file.write_all(b"\n")?;
        file.sync_data()?;
    }
    let api = tokio::net::TcpListener::bind(args.api_bind)
        .await
        .context("operator API bind")?;
    let router = Router::new()
        .route("/status", get(status))
        .route("/alert-receipt", post(alert_receipt))
        .route("/outpoints/{id}/{index}", get(outpoint))
        .route(
            "/check-transaction",
            post(check_transaction).layer(DefaultBodyLimit::max(100_000)),
        )
        .route("/transactions", post(submit))
        .layer(DefaultBodyLimit::max(4_000_000))
        .layer(axum::middleware::from_fn_with_state(
            app.operator_token.clone(),
            authenticate,
        ))
        .with_state(app.clone());
    let fetch_bodies =
        args.observe_bodies || args.stratum_bind.is_some() || args.shadow_log.is_some();
    let cfg = peer::Config {
        network: network.into(),
        allow_sv_only: !args.sv_peer.is_empty(),
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
    if let (Some(url), Some(token_file)) = (args.alert_service_url, args.alert_token_file) {
        let token = std::fs::read_to_string(token_file)?;
        tasks.spawn(alerts::follow(
            url,
            token,
            dir.join("alerts"),
            app.mining.clone(),
        ));
    }
    for addr in args.sv_peer {
        tasks.spawn(sv_peer::run(
            addr,
            regtest,
            app.mining.clone(),
            events.clone(),
        ));
    }
    tasks.spawn(peer::run(cfg, events, app.mining.clone(), solved));
    tasks.spawn(consume(app.clone(), stream));
    tasks.spawn(async move { axum::serve(api, router).await.context("operator API") });
    if let Some(bind) = args.stratum_bind {
        tasks.spawn(mining::run_stratum(
            bind,
            app.mining,
            solutions.clone(),
            worker_password,
        ));
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
