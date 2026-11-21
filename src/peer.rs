//! Teranode v0.16.1 native peer, including go-p2p-message-bus envelope framing.
mod assets;
mod fetch;
use assets::{AssetService, Assets};
pub(crate) use fetch::public_ip;
use fetch::valid_base;
use fetch::Request;

use crate::mining::wire::display_hash;
use anyhow::{ensure, Context, Result};
use base64::{engine::general_purpose::STANDARD, Engine};
use bsv::primitives::hash::hash256;
use futures::StreamExt;
use hickory_resolver::TokioAsyncResolver;
use libp2p::{
    gossipsub, identify, identity,
    multiaddr::Protocol,
    noise,
    swarm::{NetworkBehaviour, SwarmEvent},
    tcp, yamux, Multiaddr, PeerId, Swarm, SwarmBuilder,
};
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::{
    collections::{HashMap, HashSet},
    fs::{self, OpenOptions},
    io::Write,
    net::SocketAddr,
    path::{Path, PathBuf},
    sync::Arc,
    time::{Duration, Instant},
};
use tokio::sync::mpsc;

use crate::mining::body_limit;
const CLIENT: &str = "scary-node-alpha";

pub struct Config {
    pub allow_sv_only: bool,
    pub network: String,
    pub peers: Vec<String>,
    pub listen: String,
    pub key_file: PathBuf,
    pub asset_bind: Option<SocketAddr>,
    pub asset_url: Option<String>,
    pub fetch_bodies: bool,
}

#[derive(Debug)]
pub enum Event {
    Connected(String),
    Disconnected(String),
    Delivery {
        peer: String,
        hash: String,
        direction: &'static str,
    },
    Status {
        peer: String,
        height: u32,
        hash: String,
    },
    Block {
        source: String,
        height: u32,
        raw: crate::body::BlockBody,
        validated: tokio::sync::oneshot::Sender<bool>,
    },
    Notice {
        peer: String,
        height: u32,
        hash: String,
        header: Vec<u8>,
    },
    Subtree {
        peer: String,
        hash: String,
    },
    Gap(String),
    PublicationFailed {
        hash: String,
        error: String,
    },
    Published {
        hash: String,
    },
}

#[derive(NetworkBehaviour)]
struct Behaviour {
    gossip: gossipsub::Behaviour,
    identify: identify::Behaviour,
}

fn load_key(path: &Path) -> Result<identity::Keypair> {
    match fs::read(path) {
        Ok(bytes) => {
            return identity::Keypair::from_protobuf_encoding(&bytes)
                .context("reading existing peer identity")
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => return Err(e.into()),
    }
    if let Some(p) = path.parent() {
        fs::create_dir_all(p)?;
    }
    let key = identity::Keypair::generate_ed25519();
    let mut opts = OpenOptions::new();
    opts.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        opts.mode(0o600);
    }
    let mut file = opts
        .open(path)
        .context("creating unique private peer identity")?;
    file.write_all(&key.to_protobuf_encoding()?)?;
    file.sync_all()?;
    Ok(key)
}

struct Topics {
    status: gossipsub::IdentTopic,
    block: gossipsub::IdentTopic,
    subtree: gossipsub::IdentTopic,
}
impl Topics {
    fn new(network: &str) -> Self {
        let topic = |suffix| {
            gossipsub::IdentTopic::new(format!("teranode/bitcoin/1.0.0/{network}-{suffix}"))
        };
        Self {
            status: topic("node_status"),
            block: topic("block"),
            subtree: topic("subtree"),
        }
    }
}

#[derive(Deserialize, Serialize)]
struct Envelope {
    name: String,
    data: String,
}
#[derive(Deserialize)]
struct Status {
    peer_id: String,
    best_height: u32,
    best_block_hash: String,
    base_url: String,
}
#[derive(Deserialize)]
#[serde(rename_all = "PascalCase")]
struct Notice {
    #[serde(rename = "PeerID")]
    peer_id: String,
    hash: String,
    #[serde(rename = "DataHubURL")]
    data_hub_url: String,
    #[serde(default)]
    header: String,
    #[serde(default)]
    height: u32,
}
fn hash_text(s: &str) -> bool {
    s.len() == 64 && s.bytes().all(|b| b.is_ascii_hexdigit())
}

// go-libp2p-pubsub keys its IHAVE/IWANT cache by the binary author and
// big-endian protobuf sequence number, including on relayed messages.
fn go_message_id(message: &gossipsub::Message) -> gossipsub::MessageId {
    let mut id = message
        .source
        .map(|peer| peer.to_bytes())
        .unwrap_or_default();
    if let Some(sequence) = message.sequence_number {
        id.extend_from_slice(&sequence.to_be_bytes());
    }
    gossipsub::MessageId::from(id)
}

fn bus_payload(frame: &[u8]) -> Result<Option<Vec<u8>>> {
    // Some mainnet listeners run binary libp2p peer discovery on these same
    // topics. The Teranode message bus publishes a JSON object on each topic.
    if frame.iter().find(|b| !b.is_ascii_whitespace()) != Some(&b'{') {
        return Ok(None);
    }
    let envelope: Envelope = serde_json::from_slice(frame).context("invalid bus envelope")?;
    Ok(Some(
        STANDARD
            .decode(envelope.data)
            .context("invalid base64 payload")?,
    ))
}

async fn dispatch(
    message: gossipsub::Message,
    topics: &Topics,
    events: &mpsc::Sender<Event>,
    fetch: Option<&fetch::Queue>,
    sources: &mut HashMap<String, String>,
) -> Result<()> {
    let author = message.source.context("unsigned message")?.to_string();
    ensure!(
        message.data.len() <= 128 * 1024,
        "GossipSub envelope too large"
    );
    let Some(bytes) = bus_payload(&message.data)? else {
        return Ok(());
    };
    let request = if message.topic == topics.status.hash() {
        let status: Status = serde_json::from_slice(&bytes)?;
        ensure!(
            status.peer_id == author && hash_text(&status.best_block_hash),
            "invalid peer status"
        );
        let hash = status.best_block_hash.to_ascii_lowercase();
        events
            .send(Event::Status {
                peer: author.clone(),
                height: status.best_height,
                hash: hash.clone(),
            })
            .await?;
        Request {
            hash,
            height: status.best_height,
            base: status.base_url,
            header: None,
        }
    } else {
        let notice: Notice = serde_json::from_slice(&bytes)?;
        ensure!(
            notice.peer_id == author && hash_text(&notice.hash),
            "invalid peer notice"
        );
        let hash = notice.hash.to_ascii_lowercase();
        if message.topic == topics.subtree.hash() {
            events
                .send(Event::Subtree {
                    peer: author.clone(),
                    hash,
                })
                .await?;
            return Ok(());
        }
        ensure!(message.topic == topics.block.hash(), "unsupported topic");
        let header = hex::decode(notice.header)?;
        ensure!(
            header.len() == 80 && display_hash(hash256(&header)) == hash,
            "invalid announced header"
        );
        events
            .send(Event::Notice {
                peer: author.clone(),
                height: notice.height,
                hash: hash.clone(),
                header: header.clone(),
            })
            .await?;
        Request {
            hash,
            height: notice.height,
            base: notice.data_hub_url,
            header: Some(header),
        }
    };
    if let Some(queue) = fetch {
        if sources.contains_key(&author) || sources.len() < 128 {
            sources.insert(author, request.base.clone());
        }
        let hash = request.hash.clone();
        if queue.offer(request).is_err() {
            events
                .send(Event::Gap(format!(
                    "block download queue unavailable for {hash}"
                )))
                .await?;
        }
    }
    Ok(())
}

fn publish(
    swarm: &mut Swarm<Behaviour>,
    topic: &gossipsub::IdentTopic,
    value: serde_json::Value,
) -> Result<()> {
    let envelope = Envelope {
        name: CLIENT.into(),
        data: STANDARD.encode(serde_json::to_vec(&value)?),
    };
    swarm
        .behaviour_mut()
        .gossip
        .publish(topic.clone(), serde_json::to_vec(&envelope)?)?;
    Ok(())
}

async fn announce(
    swarm: &mut Swarm<Behaviour>,
    topics: &Topics,
    service: &AssetService,
    base: &str,
    height: u32,
    raw: Vec<u8>,
) -> Result<String> {
    let asset = Arc::new(Assets::new(height, raw)?);
    service.insert(asset.clone()).await;
    let peer_id = swarm.local_peer_id().to_string();
    if let Some(hash) = &asset.subtree {
        // A receiver can also fetch the subtree directly from the block notice.
        let _ = publish(
            swarm,
            &topics.subtree,
            json!({"PeerID":peer_id,"ClientName":CLIENT,"DataHubURL":base,"Hash":hash}),
        );
    }
    publish(
        swarm,
        &topics.block,
        json!({"PeerID":peer_id,"ClientName":CLIENT,"DataHubURL":base,"Hash":asset.hash,"Height":height,"Header":asset.header,"Coinbase":""}),
    )?;
    Ok(asset.hash.clone())
}

async fn bootstrap() -> Result<Vec<Multiaddr>> {
    let resolver = TokioAsyncResolver::tokio_from_system_conf()?;
    let records = tokio::time::timeout(
        Duration::from_secs(8),
        resolver.txt_lookup("_dnsaddr.mainnet.bootstrap.teranode.bsvb.tech."),
    )
    .await??;
    let mut addresses = Vec::new();
    for record in records.iter().take(32) {
        let text = record
            .txt_data()
            .iter()
            .flat_map(|part| part.iter().copied())
            .collect::<Vec<_>>();
        if let Some(address) = std::str::from_utf8(&text)
            .ok()
            .and_then(|r| r.strip_prefix("dnsaddr="))
            .and_then(|s| s.parse::<Multiaddr>().ok())
        {
            addresses.push(address);
        }
    }
    ensure!(
        !addresses.is_empty(),
        "bootstrap contained no peer addresses"
    );
    Ok(addresses)
}

fn endpoint_ip(addr: &Multiaddr) -> Option<std::net::IpAddr> {
    addr.iter().find_map(|p| match p {
        Protocol::Ip4(ip) => Some(ip.into()),
        Protocol::Ip6(ip) => Some(ip.into()),
        _ => None,
    })
}

// Dial a numeric endpoint so the IP evaluated by a ban is the one used by TCP.
async fn resolve_endpoint(addr: &Multiaddr) -> Result<Vec<Multiaddr>> {
    for (index, proto) in addr.iter().enumerate() {
        let (host, family) = match proto {
            Protocol::Dns(name) => (name, 0),
            Protocol::Dns4(name) => (name, 4),
            Protocol::Dns6(name) => (name, 6),
            Protocol::Dnsaddr(name) => {
                // Match the supplied suffix (including peer identity) when expanding
                // the TXT records. Nested indirection is bounded by the caller.
                let qname = format!("_dnsaddr.{}.", name.trim_end_matches('.'));
                let resolver = TokioAsyncResolver::tokio_from_system_conf()?;
                let txt = tokio::time::timeout(Duration::from_secs(3), resolver.txt_lookup(qname))
                    .await??;
                let suffix: Multiaddr = addr.iter().skip(index + 1).collect();
                let mut results = Vec::new();
                for record in txt.iter().take(32) {
                    let mut bytes = Vec::new();
                    for part in record.txt_data() {
                        bytes.extend_from_slice(part);
                    }
                    if let Some(resolved) = std::str::from_utf8(&bytes)
                        .ok()
                        .and_then(|s| s.strip_prefix("dnsaddr="))
                        .and_then(|s| s.parse::<Multiaddr>().ok())
                        .filter(|a| a.as_ref().ends_with(suffix.as_ref()))
                    {
                        results.push(resolved);
                    }
                }
                return Ok(results);
            }
            _ => continue,
        };
        let ips = tokio::time::timeout(
            Duration::from_secs(3),
            tokio::net::lookup_host((host.as_ref(), 0)),
        )
        .await??;
        let endpoints = ips
            .filter_map(|ip| {
                let replacement = match ip.ip() {
                    std::net::IpAddr::V4(v) if family != 6 => Protocol::Ip4(v),
                    std::net::IpAddr::V6(v) if family != 4 => Protocol::Ip6(v),
                    _ => return None,
                };
                addr.replace(index, |_| Some(replacement))
            })
            .collect();
        return Ok(endpoints);
    }
    Ok(vec![addr.clone()])
}

async fn dial_addresses(addr: &Multiaddr) -> Result<Vec<Multiaddr>> {
    let mut pending = vec![addr.clone()];
    let mut addresses = Vec::new();
    let mut seen = HashSet::new();
    while let Some(next) = pending.pop() {
        if !seen.insert(next.clone()) {
            continue;
        }
        ensure!(seen.len() <= 64, "too many DNS peer records");
        if endpoint_ip(&next).is_some() {
            addresses.push(next);
            continue;
        }
        pending.extend(resolve_endpoint(&next).await?);
    }
    Ok(addresses)
}

pub async fn run(
    config: Config,
    events: mpsc::Sender<Event>,
    state: Arc<tokio::sync::Mutex<crate::mining::Mining>>,
    mut solved: mpsc::Receiver<(u32, Vec<u8>)>,
) -> Result<()> {
    ensure!(
        matches!(config.network.as_str(), "mainnet" | "regtest"),
        "unknown peer network"
    );
    ensure!(
        config.network != "regtest" || !config.peers.is_empty() || config.allow_sv_only,
        "regtest needs an explicit peer"
    );
    let key = load_key(&config.key_file)?;
    let protocol = format!("/teranode/bitcoin/{}/1.0.0", config.network);
    let topics = Topics::new(&config.network);
    let mut swarm = SwarmBuilder::with_existing_identity(key)
        .with_tokio()
        .with_tcp(
            tcp::Config::default().nodelay(true),
            noise::Config::new,
            yamux::Config::default,
        )?
        .with_dns()?
        .with_behaviour(|key| {
            let cfg = gossipsub::ConfigBuilder::default()
                .validation_mode(gossipsub::ValidationMode::Strict)
                .message_id_fn(go_message_id)
                .max_transmit_size(128 * 1024)
                .build()?;
            let gossip =
                gossipsub::Behaviour::new(gossipsub::MessageAuthenticity::Signed(key.clone()), cfg)
                    .map_err(std::io::Error::other)?;
            let identify = identify::Behaviour::new(
                identify::Config::new(protocol.clone(), key.public())
                    .with_agent_version(CLIENT.into()),
            );
            Ok(Behaviour { gossip, identify })
        })?
        .with_swarm_config(|s| s.with_idle_connection_timeout(Duration::from_secs(120)))
        .build();
    for t in [&topics.status, &topics.block, &topics.subtree] {
        swarm.behaviour_mut().gossip.subscribe(t)?;
    }
    swarm.listen_on(config.listen.parse::<Multiaddr>()?)?;
    let mut addresses = config
        .peers
        .iter()
        .map(|s| s.parse::<Multiaddr>())
        .collect::<Result<Vec<_>, _>>()?;
    let use_dns = addresses.is_empty() && config.network == "mainnet";
    ensure!(
        config.asset_bind.is_some() == config.asset_url.is_some(),
        "Asset bind and advertised URL must be supplied together"
    );
    let assets = AssetService::default();
    if let (Some(bind), Some(base)) = (config.asset_bind, config.asset_url.as_ref()) {
        assets.serve(bind, base).await?;
    }
    let fetch = if config.fetch_bodies {
        Some(fetch::Queue::start(config.network.clone(), events.clone()))
    } else {
        None
    };
    let mut identified = HashSet::<PeerId>::new();
    let mut retry = tokio::time::interval(Duration::from_secs(25));
    retry.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    let mut last_discovery = None::<Instant>;
    let mut last_gossip_error = None::<Instant>;
    let mut endpoints = HashMap::new();
    let mut asset_sources = HashMap::<String, String>::new();
    let mut enforce = tokio::time::interval(Duration::from_secs(1));
    let mut last_attempt = None::<(String, Instant)>;
    loop {
        tokio::select! {
            _ = enforce.tick() => {
                let s = state.lock().await;
                for (id, ip) in endpoints.values() {
                    if s.directives.peer_banned(ip) { let _ = swarm.disconnect_peer_id(*id); }
                }
                if let (Some(base),Some(id)) = (config.asset_url.as_deref(), s.relayable_solution()) {
                    let hash = display_hash(id);
                    if last_attempt.as_ref().is_none_or(|(h,t)| *h != hash || t.elapsed() >= Duration::from_secs(15)) {
                        let raw = s.solution_bytes(&id).expect("matched stored solution").to_vec();
                        let height = s.solution_height(id).expect("selected stored solution");
                        last_attempt = Some((hash.clone(),Instant::now()));
                        let event = match announce(&mut swarm,&topics,&assets,base,height,raw).await {
                            Ok(hash) => Event::Published{hash},
                            Err(e) => Event::PublicationFailed{hash,error:e.to_string()},
                        };
                        drop(s);
                        events.send(event).await?;
                    }
                }
            }
            _ = events.closed() => return Ok(()),
            _ = retry.tick() => {
                if use_dns && last_discovery.is_none_or(|t| t.elapsed() > Duration::from_secs(300)) {
                    match bootstrap().await {
                        Ok(found) => {
                            addresses = found;
                            last_discovery = Some(Instant::now());
                        }
                        Err(error) => eprintln!("mainnet bootstrap lookup: {error}"),
                    }
                }
                let (blocked,missing)={let m=state.lock().await;(m.directives.banned.clone(),m.missing_recent())};
                if let Some(queue)=&fetch{
                    for (id,height) in missing {
                        for base in asset_sources.values(){
                            let _=queue.offer(Request{hash:display_hash(id),height,base:base.clone(),header:None});
                        }
                    }
                }
                for address in &addresses {

                    if matches!(address.iter().last(), Some(Protocol::P2p(id)) if swarm.is_connected(&id)) {
                        continue;
                    }
                    match dial_addresses(address).await {
                        Ok(targets) => {
                            for target in targets {
                                if endpoint_ip(&target).is_some_and(|ip| blocked.iter().any(|net|net.contains(&ip))) {continue;}
                                if let Err(error)=swarm.dial(target){eprintln!("peer dial: {error}");}
                            }
                        }
                        Err(error)=>eprintln!("peer address resolution: {error}"),
                    }
                }
            }
            work = solved.recv() => {
                let Some((height, raw)) = work else { return Ok(()) };
                let base = config.asset_url.as_deref().context("solved block requires an advertised Asset URL")?;
                let hash = display_hash(hash256(&raw[..80]));
                let guard = state.lock().await;
                if guard.relayable_solution() != Some(hash256(&raw[..80])) {continue;}
                if last_attempt.as_ref().is_some_and(|(h,_)| h==&hash) {continue;}
                last_attempt=Some((hash.clone(),Instant::now()));
                let event = match announce(&mut swarm, &topics, &assets, base, height, raw).await {
                    Ok(hash) => Event::Published { hash },
                    Err(error) => Event::PublicationFailed { hash, error: error.to_string() },
                };
                drop(guard);
                events.send(event).await?;
            }
            event = swarm.select_next_some() => match event {
                SwarmEvent::ConnectionEstablished { connection_id,peer_id,endpoint,.. } => {
                    if let Some(ip)=endpoint_ip(endpoint.get_remote_address()) {
                        endpoints.insert(connection_id,(peer_id,ip));
                        if state.lock().await.directives.peer_banned(&ip){let _=swarm.disconnect_peer_id(peer_id);}
                    }
                }
                SwarmEvent::Behaviour(BehaviourEvent::Identify(identify::Event::Received { peer_id, info, .. })) => {
                    if info.protocol_version != protocol {
                        let _ = swarm.disconnect_peer_id(peer_id);
                    } else if identified.insert(peer_id) {
                        events.send(Event::Connected(peer_id.to_string())).await?;
                    }
                }
                SwarmEvent::Behaviour(BehaviourEvent::Gossip(gossipsub::Event::Message { message, .. })) => {
                    if let Err(error) = dispatch(message, &topics, &events, fetch.as_ref(),&mut asset_sources).await {
                        if last_gossip_error.is_none_or(|t| t.elapsed() >= Duration::from_secs(30)) {
                            eprintln!("ignored invalid Teranode message: {error}");
                            last_gossip_error = Some(Instant::now());
                        }
                    }
                }
                SwarmEvent::ConnectionClosed { connection_id,peer_id, num_established, .. } => {
                    endpoints.remove(&connection_id);
                    if num_established==0 && identified.remove(&peer_id) { events.send(Event::Disconnected(peer_id.to_string())).await?; }
                }
                SwarmEvent::NewListenAddr { address, .. } => {
                    eprintln!("peer listener {address}/p2p/{}", swarm.local_peer_id());
                }
                SwarmEvent::OutgoingConnectionError { error, .. } => eprintln!("peer connection failed: {error}"),
                _ => {}
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn decodes_bus_json_and_ignores_binary_discovery_frames() {
        let json = br#" {"name":"ref","data":"e30="}"#;
        assert_eq!(
            bus_payload(json).unwrap().as_deref(),
            Some(b"{}".as_slice())
        );
        assert_eq!(bus_payload(&[0x0a, 0x02, 0x08, 0x01]).unwrap(), None);
        assert!(bus_payload(br#"{"name":"ref","data":"!"}"#).is_err());
    }
}
