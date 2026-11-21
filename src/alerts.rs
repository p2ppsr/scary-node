//! Replicate the maintained Alert System's signed history into local state.
//! The network-specific key roots are immutable on mainnet.
use crate::mining::wire::{self, Event, Hash};
use anyhow::{ensure, Context, Result};
use bsv::{
    compat::bsm::BSM,
    primitives::{
        big_number::{BigNumber, Endian},
        curve::Curve,
        hash::hash256,
        public_key::PublicKey,
        signature::Signature,
    },
};
use serde_json::{json, Value};
use std::{
    collections::{HashMap, HashSet},
    fs,
    io::Write,
    net::IpAddr,
    path::Path,
};

pub const MAIN_KEYS: [&str; 5] = [
    "02a1589f2c8e1a4e7cbf28d4d6b676aa2f30811277883211027950e82a83eb2768",
    "03aec1d40f02ac7f6df701ef8f629515812f1bcd949b6aa6c7a8dd778b748b2433",
    "03ddb2806f3cc48aa36bd4aea6b9f1c7ed3ffc8b9302b198ca963f15beff123678",
    "036846e3e8f4f944af644b6a6c6243889dd90d7b6c3593abb9ccf2acb8c9e606e2",
    "03e45c9dd2b34829c1d27c8b5d16917dd0dc2c88fa0d7bad7bffb9b542229a9304",
];

type Outpoint = (Hash, u32);
const MARKER: &[u8] = b"\0\x6a\x04cftx";

pub fn confiscation(raw: &[u8]) -> Result<Option<Vec<Outpoint>>> {
    let mut reader = raw;
    let mut inputs = Vec::new();
    let mut marked = false;
    let mut valid = true;
    wire::transaction(&mut reader, |event| match event {
        Event::Input(hash, n, _) => inputs.push((hash, n)),
        Event::Output(0, _, s) if s.starts_with(MARKER) => {
            marked = true;
            valid = (29..=83).contains(&s.len())
                && s[7] < 76
                && s[7] as usize == s.len() - 8
                && s[8] == 1;
        }
        Event::Output(i, _, s) if marked && i > 0 && s.starts_with(&[0, 0x6a]) => valid = false,
        _ => {}
    })?;
    ensure!(
        reader.is_empty() && valid,
        "invalid confiscation transaction contents"
    );
    Ok(marked.then_some(inputs))
}

#[derive(Clone, PartialEq, Eq)]
struct Freeze {
    start: u32,
    stop: u32,
    policy_expires: bool,
}
#[derive(Clone)]
pub struct Book {
    keys: Vec<String>,
    actions: HashMap<Hash, u32>,
    results: Vec<Value>,
    failed_action: Option<(Hash, Value)>,
    sealed: Vec<Hash>, // envelope digests in contiguous sequence order, starting at one
    pub verified: u32,
    pub pending: Option<String>,
    pub connected: bool,
    pub reported_sequence: Option<u32>,
    pub supervised: bool,
    pub require_peer_sync: bool,
    peer_sync: Option<(u32, u64)>,
    genesis_verified: bool,
    frozen: HashMap<Outpoint, Freeze>,
    confiscated: HashSet<Outpoint>,
    whitelist: HashMap<Hash, u32>,
    banned: HashMap<ipnet::IpNet, u64>,
    pub invalid: HashSet<Hash>,
    pub last_notice: Option<String>,
}

fn action_key(method: &str, params: &Value) -> Result<Hash> {
    Ok(hash256(&serde_json::to_vec(&json!([method, params]))?))
}

impl Book {
    pub fn new(keys: &[String], supervised: bool) -> Result<Self> {
        check_keys(keys)?;
        Ok(Self {
            keys: keys.to_vec(),
            actions: HashMap::new(),
            results: vec![],
            failed_action: None,
            sealed: vec![],
            verified: 0,
            pending: None,
            connected: false,
            reported_sequence: None,
            supervised,
            require_peer_sync: false,
            peer_sync: None,
            genesis_verified: false,
            frozen: HashMap::new(),
            confiscated: HashSet::new(),
            whitelist: HashMap::new(),
            banned: HashMap::new(),
            invalid: HashSet::new(),
            last_notice: None,
        })
    }
    pub fn applied(&self) -> u32 {
        self.sealed.len() as u32
    }
    fn fresh_sync(&self, current: u64) -> bool {
        !self.require_peer_sync
            || self.peer_sync.is_some_and(|(seq, at)| {
                self.reported_sequence == Some(seq)
                    && current.checked_sub(at).is_some_and(|age| age <= 1200)
            })
    }
    pub fn ready(&self) -> bool {
        !self.supervised
            || (self.genesis_verified
                && self.connected
                && self.fresh_sync(
                    std::time::SystemTime::now()
                        .duration_since(std::time::UNIX_EPOCH)
                        .unwrap_or_default()
                        .as_secs(),
                )
                && self.pending.is_none()
                && self.reported_sequence == Some(self.applied())
                && self.verified == self.applied())
    }
    pub fn status(&self) -> Value {
        json!({"last_verified":self.verified,"last_applied":self.applied(),
        "peer_sync_required":self.require_peer_sync,
        "last_peer_sync":self.peer_sync.map(|(sequence,unix_s)|json!({"sequence":sequence,"unix_s":unix_s})),
        "genesis_verified":self.genesis_verified,"reported_sequence":self.reported_sequence,"connected":self.connected,"supervised":self.supervised,
        "outstanding_action":self.pending,"ready":self.ready(),"last_notice":self.last_notice,
        "frozen_records":self.frozen.len(),"authorized_confiscations":self.whitelist.len(),
        "invalidated_blocks":self.invalid.len(),"banned_peers":self.active_bans().len()})
    }
    pub fn frozen(&self, out: &Outpoint, height: u32, policy: bool) -> bool {
        self.confiscated.contains(out)
            || self.frozen.get(out).is_some_and(|f| {
                if policy {
                    !f.policy_expires || (f.start < f.stop && height < f.stop)
                } else {
                    f.start <= height && height < f.stop
                }
            })
    }
    pub fn active_bans(&self) -> HashSet<ipnet::IpNet> {
        let at = unix_now();
        self.banned
            .iter()
            .filter_map(|(net, &until)| (at < until).then_some(*net))
            .collect()
    }
    pub fn peer_banned(&self, ip: &IpAddr) -> bool {
        self.peer_banned_at(ip, unix_now())
    }
    fn peer_banned_at(&self, ip: &IpAddr, at: u64) -> bool {
        self.banned
            .iter()
            .any(|(net, &until)| at < until && net.contains(ip))
    }
    pub fn has_freezes(&self) -> bool {
        !self.frozen.is_empty() || !self.confiscated.is_empty()
    }
    pub fn authorized(&self, id: &Hash, height: u32) -> bool {
        self.whitelist.get(id).is_some_and(|h| height >= *h)
    }
    pub fn restore_through(&mut self, dir: &Path, through: u32) -> Result<()> {
        fs::create_dir_all(dir)?;
        let mut sequences = Vec::new();
        for ent in fs::read_dir(dir)? {
            let path = ent?.path();
            if path.extension().is_some_and(|e| e == "signed") {
                let n = path
                    .file_stem()
                    .and_then(|s| s.to_str())
                    .context("alert journal filename")?
                    .parse::<u32>()?;
                sequences.push((n, path));
            }
        }
        sequences.sort_by_key(|(n, _)| *n);
        for (seq, path) in sequences {
            if seq > through {
                break;
            }
            ensure!(seq == self.applied() + 1, "alert journal sequence gap");
            let bytes = fs::read(path)?;
            if let Err(e) = self.receive(&bytes, Some(dir)) {
                if self.verified == seq && self.pending.is_some() {
                    break;
                }
                return Err(e);
            }
        }
        Ok(())
    }
    pub fn receive(&mut self, raw: &[u8], dir: Option<&Path>) -> Result<bool> {
        ensure!(raw.len() >= 20 + 2 + 3 * 65, "short signed alert");
        let seq = u32::from_le_bytes(raw[4..8].try_into()?);
        ensure!(seq > 0, "genesis is fixed by network");
        let digest = hash256(raw);
        if seq <= self.applied() {
            ensure!(
                self.sealed[seq as usize - 1] == digest,
                "conflicting alert records at sequence {seq}"
            );
            return Ok(false);
        }
        ensure!(
            seq == self
                .applied()
                .checked_add(1)
                .context("alert sequence exhausted")?,
            "alert sequence gap"
        );
        let end = raw.len() - 3 * 65;
        let data = &raw[..end];
        // Upstream v0.3.5 verifies compact BSM signatures over lowercase hex
        // of the signed header and payload, not the binary message digest.
        let message = hex::encode(data);
        let hash = BigNumber::from_bytes(&BSM::magic_hash(message.as_bytes()), Endian::Big);
        let mut seen = HashSet::new();
        for sig in raw[end..].as_chunks::<65>().0 {
            let (s, r, compressed) = Signature::from_compact_bsm(sig)?;
            ensure!(
                compressed
                    && !s.r().is_zero()
                    && !s.s().is_zero()
                    && s.r().cmp(&Curve::secp256k1().n) < 0
                    && s.s().cmp(&Curve::secp256k1().n) < 0,
                "invalid alert signature"
            );
            let key = s.recover_public_key(r, &hash)?.to_der_hex();
            ensure!(
                self.keys.contains(&key) && seen.insert(key),
                "invalid or repeated alert signer"
            );
        }
        self.verified = seq;
        if let Some(dir) = dir {
            persist(dir, seq, raw)?;
        }
        let mut next = self.clone();
        let kind = u32::from_le_bytes(raw[16..20].try_into()?);
        let result = (|| {
            ensure!(
                u32::from_le_bytes(raw[..4].try_into()?) == 1,
                "unknown alert version"
            );
            let ban_start = if kind == 5 {
                match dir {
                    Some(dir) => local_ban_start(dir, seq)?,
                    None => unix_now(),
                }
            } else {
                0
            };
            next.apply(kind, &raw[20..end], ban_start)
        })();
        if let Err(e) = result {
            self.pending = Some(format!("sequence {seq}: {e}"));
            if let Some(dir) = dir {
                use sha2::{Digest, Sha256};
                let failure = json!({
                    "sequence": seq, "kind": kind, "signed_sha256": hex::encode(Sha256::digest(raw)),
                    "error": e.to_string(), "rpc_result": next.failed_action.as_ref().map(|(_, r)| r),
                });
                durable_file(
                    dir,
                    &format!("{seq:010}.failure"),
                    &serde_json::to_vec(&failure)?,
                )?;
                self.failed_action = next.failed_action;
            }
            return Err(e);
        }
        next.sealed.push(digest);
        next.pending = None;
        *self = next;
        Ok(true)
    }
    pub fn receipt(&self, method: &str, params: &Value, durable: u32) -> Result<Value> {
        let digest = action_key(method, params)?;
        if let Some((_, result)) = self
            .failed_action
            .as_ref()
            .filter(|(key, _)| *key == digest)
        {
            return Ok(result.clone());
        }
        let sequence = self
            .actions
            .get(&digest)
            .context("signed action awaiting durable application")?;
        ensure!(*sequence <= durable, "action has not reached disk");
        Ok(self.results[*sequence as usize - 1].clone())
    }

    fn apply(&mut self, kind: u32, mut body: &[u8], ban_start: u64) -> Result<()> {
        let mut rpc = None::<(&str, Value)>;
        let mut result = Value::Null;
        self.failed_action = None;
        match kind {
            1 => self.last_notice = Some(String::from_utf8_lossy(field(&mut body)?).into_owned()),
            2 | 3 => {
                ensure!(
                    !body.is_empty() && body.len().is_multiple_of(57),
                    "invalid freeze record length"
                );
                let mut funds = Vec::new();
                let mut not_processed = Vec::new();
                let mut item = 0;
                while !body.is_empty() {
                    item += 1;
                    let mut hash: Hash = wire::take(&mut body, 32)?.try_into()?;
                    hash.reverse(); // alert field is display-order TXID
                    let vout = integer(&mut body)?;
                    let start = integer(&mut body)?;
                    let stop = integer(&mut body)?;
                    ensure!(
                        start <= i32::MAX as u32 && stop <= i32::MAX as u32,
                        "height out of range at item {item}"
                    );
                    let policy_expires = wire::take(&mut body, 1)?[0] != 0;

                    funds.push(json!({"txOut":{"txId":wire::display_hash(hash),"vout":vout},
                        "enforceAtHeight":[{"start":start,"stop":stop}],"policyExpiresWithConsensus":policy_expires}));
                    let record = Freeze {
                        start,
                        stop,
                        policy_expires,
                    };
                    if self.frozen.get(&(hash, vout)) == Some(&record) {
                        not_processed.push(
                            json!({"txOut": {"txId":wire::display_hash(hash), "vout":vout},
                            "reason":"already in consensus"}),
                        );
                    } else {
                        self.frozen.insert((hash, vout), record);
                    }
                }
                rpc = Some(("addToConsensusBlacklist", json!([{"funds":funds}])));
                result = json!({"notProcessed":not_processed});
            }
            4 => {
                let h = integer(&mut body)?;
                ensure!(h <= i32::MAX as u32, "confiscation height out of range");
                let tx = field(&mut body)?;
                let id = hash256(tx);
                let method = "addToConfiscationTxidWhitelist";
                let params = json!([{"confiscationTxs":[{"confiscationTx":{"enforceAtHeight":h,"hex":hex::encode(tx)}}]}]);
                let inputs = confiscation(tx).ok().flatten();
                let reason = match &inputs {
                    None => Some("confiscation transaction is not valid"),
                    Some(inputs) if inputs.iter().any(|p| !self.frozen(p, h, false)) => {
                        Some("confiscated TXO is not consensus frozen")
                    }
                    _ => None,
                };
                if let Some(reason) = reason {
                    let reply = json!({"notProcessed":[{
                        "confiscationTx":{"txId":wire::display_hash(id)}, "reason":reason
                    }]});
                    self.failed_action = Some((action_key(method, &params)?, reply));
                    anyhow::bail!("{reason}");
                }
                let inputs = inputs.context("missing confiscation marker")?;
                rpc = Some((method, params));
                result = json!({"notProcessed":[]});
                self.confiscated.extend(inputs);
                let entry = self.whitelist.entry(id).or_insert(h);
                *entry = (*entry).min(h);
            }
            5 | 6 => {
                let peer_text = std::str::from_utf8(field(&mut body)?)?;
                let peer = if let Ok(ip) = peer_text.parse::<IpAddr>() {
                    ipnet::IpNet::from(ip)
                } else {
                    peer_text
                        .parse::<ipnet::IpNet>()
                        .context("alert IP address or CIDR")?
                        .trunc()
                };
                let _reason = field(&mut body)?;
                rpc = Some((
                    "setban",
                    json!([peer_text, if kind == 5 { "add" } else { "remove" }]),
                ));
                if kind == 5 {
                    let until = self.banned.entry(peer).or_default();
                    if *until <= ban_start {
                        *until = ban_start
                            .checked_add(86_400)
                            .context("ban expiry overflow")?;
                    }
                } else {
                    self.banned.remove(&peer);
                }
            }
            7 => {
                let hash = wire::take(&mut body, 32)?.try_into()?;
                ensure!(!field(&mut body)?.is_empty(), "missing invalidation reason");
                rpc = Some(("invalidateblock", json!([wire::display_hash(hash)])));
                self.invalid.insert(hash);
            }
            8 => {
                let keys: Vec<_> = body.as_chunks::<33>().0.iter().map(hex::encode).collect();
                ensure!(body.len() == 165, "wrong key set length");
                check_keys(&keys)?;
                self.keys = keys;
                body = &[];
            }
            _ => anyhow::bail!("unsupported signed alert type {kind}"),
        }
        ensure!(body.is_empty(), "trailing alert payload");
        if let Some((method, params)) = rpc {
            self.actions
                .insert(action_key(method, &params)?, self.verified);
        }
        self.results.push(result);
        Ok(())
    }
}
fn check_keys(keys: &[String]) -> Result<()> {
    ensure!(
        keys.len() == 5 && keys.iter().collect::<HashSet<_>>().len() == 5,
        "expected five distinct compressed genesis or rotation keys"
    );
    for key in keys {
        ensure!(
            key.len() == 66 && PublicKey::from_string(key)?.to_der_hex() == *key,
            "invalid compressed alert key"
        );
    }
    Ok(())
}
fn integer(reader: &mut &[u8]) -> Result<u32> {
    Ok(u64::from_le_bytes(wire::take(reader, 8)?.try_into()?).try_into()?)
}
fn field<'a>(reader: &mut &'a [u8]) -> Result<&'a [u8]> {
    let n = wire::varint(reader)?;
    wire::take(reader, n)
}
fn unix_now() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

// SV Node's default ban duration runs from the local application, including
// when a new entrant applies a pre-entry alert. Preserve the original anchor
// on restart. Earlier journals have the first local verification in their
// signed file timestamp; migrate it once into the durable ban record.
fn local_ban_start(dir: &Path, seq: u32) -> Result<u64> {
    let name = format!("{seq:010}.ban-start");
    let path = dir.join(&name);
    if path.exists() {
        return Ok(u64::from_le_bytes(fs::read(path)?.as_slice().try_into()?));
    }
    let first = fs::metadata(dir.join(format!("{seq:010}.signed")))?
        .modified()?
        .duration_since(std::time::UNIX_EPOCH)?
        .as_secs();
    durable_file(dir, &name, &first.to_le_bytes())?;
    Ok(first)
}

fn persist(dir: &Path, seq: u32, raw: &[u8]) -> Result<()> {
    durable_file(dir, &format!("{seq:010}.signed"), raw)
}
fn durable_file(dir: &Path, name: &str, raw: &[u8]) -> Result<()> {
    let dest = dir.join(name);
    if dest.exists() {
        ensure!(fs::read(&dest)? == raw, "conflicting durable directive");
        return Ok(());
    }
    let tmp = dir.join(format!("{name}.tmp"));
    let mut f = fs::File::create(&tmp)?;
    f.write_all(raw)?;
    f.sync_all()?;
    fs::rename(tmp, dest)?;
    fs::File::open(dir)?.sync_all()?;
    Ok(())
}

fn apply_signed(dir: &Path, state: &mut crate::mining::Mining, raw: &[u8]) -> Result<()> {
    use sha2::{Digest, Sha256};
    state.directives.receive(raw, Some(dir))?;
    let seq = u32::from_le_bytes(raw[4..8].try_into()?);
    if seq <= state.durable_sequence() {
        return Ok(());
    }
    ensure!(
        seq == state.durable_sequence() + 1,
        "directive effects sequence gap"
    );
    let previous = state.work_revision();
    let result = (|| -> Result<()> {
        state.directives_changed(raw)?;
        let file = format!("{seq:010}.receipt");
        let digest = hex::encode(Sha256::digest(raw));
        if dir.join(&file).exists() {
            let receipt: Value = serde_json::from_slice(&fs::read(dir.join(&file))?)?;
            ensure!(
                receipt["signed_sha256"] == digest && receipt["sequence"] == seq,
                "conflicting prepared directive receipt"
            );
        } else {
            let receipt = json!({"sequence":seq,"signed_sha256":digest,"kind":u32::from_le_bytes(raw[16..20].try_into()?),
                "prior_revision":previous,"applied_revision":state.work_revision(),"active_height":state.height_hint(),
                "rpc_result":state.directives.results[seq as usize - 1]});
            durable_file(dir, &file, &serde_json::to_vec(&receipt)?)?;
        }
        state.commit_directive_sequence(seq)?;
        Ok(())
    })();
    if let Err(ref e) = result {
        state.directives.pending = Some(format!("sequence {seq}: {e}"));
    } else {
        state.directives.pending = None;
    }
    result
}

pub fn recover(dir: &Path, state: &mut crate::mining::Mining) -> Result<()> {
    fs::create_dir_all(dir)?;
    while let Some(next) = state.durable_sequence().checked_add(1) {
        let path = dir.join(format!("{next:010}.signed"));
        if !path.exists() {
            break;
        }
        let bytes = fs::read(path)?;
        if let Err(e) = apply_signed(dir, state, &bytes) {
            if state.directives.pending.is_some() {
                eprintln!("outstanding signed directive: {e}");
                break;
            }
            return Err(e);
        }
    }
    Ok(())
}

/// The upstream /health synced flag is advisory. Check the complete signed
/// prefix, active peers and the advertised sequence before making work ready.
pub async fn follow(
    url: String,
    token: String,
    dir: std::path::PathBuf,
    state: std::sync::Arc<tokio::sync::Mutex<crate::mining::Mining>>,
) -> Result<()> {
    use std::time::Duration;
    let url = reqwest::Url::parse(&url)?;
    ensure!(
        url.scheme() == "https"
            || (url.scheme() == "http"
                && url
                    .host_str()
                    .and_then(|h| h.parse::<IpAddr>().ok())
                    .is_some_and(|ip| ip.is_loopback())),
        "use HTTPS or a literal loopback address for the alert gateway"
    );
    ensure!(!token.trim().is_empty(), "empty alert gateway token");
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(4))
        .redirect(reqwest::redirect::Policy::none())
        .build()?;
    let base = url.as_str().trim_end_matches('/');
    let get = |path: &str| {
        client
            .get(format!("{base}/{path}"))
            .bearer_auth(token.trim())
    };
    loop {
        let res: Result<()> = async {
            let health: Value = get("health")
                .send()
                .await?
                .error_for_status()?
                .json()
                .await?;
            let seq: u32 = health["sequence"]
                .as_u64()
                .context("receiver sequence")?
                .try_into()?;
            let peers = health["active_peers"]
                .as_u64()
                .context("receiver active peer count")?;
            let mut s = state.lock().await;
            let was_ready = s.directives.ready();
            let sync = &health["peer_sync"];
            s.directives.peer_sync = sync["sequence"]
                .as_u64()
                .and_then(|n| u32::try_from(n).ok())
                .zip(sync["unix_s"].as_u64());
            s.directives.reported_sequence = Some(seq);
            s.directives.connected = peers > 0;
            if was_ready != s.directives.ready() {
                s.refresh_work();
            }
            if s.directives.genesis_verified
                && s.directives.applied() == seq
                && s.durable_sequence() == seq
                && s.directives.pending.is_none()
            {
                return Ok(());
            }
            ensure!(
                seq >= s.directives.applied(),
                "receiver lost verified history"
            );
            drop(s);
            let history: Value = get("alerts")
                .send()
                .await?
                .error_for_status()?
                .json()
                .await?;
            let records = history["alerts"]
                .as_array()
                .context("receiver alert history")?;
            let mut s = state.lock().await;
            let genesis = records.first().context("receiver omitted alert genesis")?;
            ensure!(
                genesis["sequence_number"] == 0,
                "receiver history lacks sequence zero"
            );
            let anchor = hex::decode(genesis["raw"].as_str().context("missing genesis bytes")?)?;
            use sha2::{Digest, Sha256};
            ensure!(
                hex::encode(Sha256::digest(&anchor))
                    == "9915b1987b80e4373a08a65b285a172055b1d042bb477011989a254882de2ac1",
                "receiver genesis differs from upstream v0.3.5"
            );
            if !s.directives.genesis_verified {
                s.directives.genesis_verified = true;
                s.refresh_work();
            }
            for record in records {
                if record["sequence_number"].as_u64() == Some(0) {
                    continue;
                }
                let raw = hex::decode(record["raw"].as_str().context("missing signed record")?)?;
                apply_signed(&dir, &mut s, &raw)?;
            }
            ensure!(
                s.directives.applied() >= seq,
                "receiver returned an incomplete history"
            );
            Ok(())
        }
        .await;
        if let Err(e) = res {
            let mut s = state.lock().await;
            if s.directives.connected {
                s.refresh_work();
            }
            s.directives.connected = false;
            eprintln!("alert receiver unavailable: {e}");
        }
        tokio::time::sleep(Duration::from_secs(2)).await;
    }
}

pub fn unconfigured() -> Book {
    Book::new(&MAIN_KEYS.map(String::from), true).expect("built-in alert roots")
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use bsv::primitives::private_key::PrivateKey;
    pub(crate) fn keys(offset: u8) -> Vec<PrivateKey> {
        (1..=5)
            .map(|i| PrivateKey::from_hex(&format!("{:02x}", i + offset)).unwrap())
            .collect()
    }
    pub(crate) fn public(keys: &[PrivateKey]) -> Vec<String> {
        keys.iter()
            .map(|k| k.to_public_key().to_der_hex())
            .collect()
    }
    pub(crate) fn signed(seq: u32, kind: u32, body: &[u8], keys: &[PrivateKey]) -> Vec<u8> {
        let mut data = 1u32.to_le_bytes().to_vec();
        data.extend(seq.to_le_bytes());
        data.extend(7u64.to_le_bytes());
        data.extend(kind.to_le_bytes());
        data.extend(body);
        let msg = hex::encode(&data);
        for k in keys.iter().take(3) {
            data.extend(BSM::sign(msg.as_bytes(), k).unwrap());
        }
        data
    }
    #[test]
    fn peer_sync_requires_the_signed_tip_and_a_recent_clock() {
        let mut book = unconfigured();
        book.require_peer_sync = true;
        book.reported_sequence = Some(5);
        assert!(!book.fresh_sync(10000));
        book.peer_sync = Some((5, 10000));
        assert!(!book.fresh_sync(9999));
        assert!(book.fresh_sync(11200));
        assert!(!book.fresh_sync(11201));
        book.reported_sequence = Some(6);
        assert!(!book.fresh_sync(10000));
    }

    #[test]
    fn signed_actions_bind_receipts_and_effective_intervals() {
        let signing = keys(18);
        let mut book = Book::new(&public(&signing), true).unwrap();
        let mut out = vec![7; 32];
        out.extend(2u64.to_le_bytes());
        let freeze = |stop: u64, expires: u8| {
            [
                out.as_slice(),
                &100u64.to_le_bytes(),
                &stop.to_le_bytes(),
                &[expires],
            ]
            .concat()
        };
        book.receive(&signed(1, 2, &freeze(120, 0), &signing), None)
            .unwrap();
        let point = ([7; 32], 2);
        assert!(book.frozen(&point, 90, true));
        assert!(!book.frozen(&point, 90, false));
        assert!(book.frozen(&point, 119, false));
        assert!(!book.frozen(&point, 120, false));
        book.receive(&signed(2, 3, &freeze(108, 1), &signing), None)
            .unwrap();
        assert!(!book.frozen(&point, 108, true));
        assert!(book.frozen(&point, 107, false));
        let peer = b"\x09127.0.0.1\x03lab";
        book.receive(&signed(3, 5, peer, &signing), None).unwrap();
        assert!(book.peer_banned(&"127.0.0.1".parse().unwrap()));
        let args = json!(["127.0.0.1", "add"]);
        assert!(book.receipt("setban", &args, 2).is_err());
        book.receipt("setban", &args, 3).unwrap();
        book.receive(&signed(4, 6, peer, &signing), None).unwrap();
        assert!(book.banned.is_empty());
        let mut invalid = vec![29; 32];
        invalid.extend(b"\x03lab");
        book.receive(&signed(5, 7, &invalid, &signing), None)
            .unwrap();
        assert!(book.invalid.contains(&[29; 32]));
        let mut repeated = signed(6, 1, b"\x02ok", &signing);
        let end = repeated.len();
        repeated.copy_within(end - 195..end - 130, end - 65);
        assert!(book.receive(&repeated, None).is_err());
        assert_eq!(book.applied(), 5);
    }

    #[test]
    fn default_sv_ban_expires_from_the_first_local_application_after_replay() {
        let signing = keys(12);
        let mut book = Book::new(&public(&signing), true).unwrap();
        let dir = std::env::temp_dir().join(format!("scary-ban-time-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let subnet = b"192.0.2.0/24";
        let mut body = vec![subnet.len() as u8];
        body.extend(subnet);
        body.extend(b"\x03lab");
        let ban = signed(1, 5, &body, &signing);
        book.receive(&ban, Some(&dir)).unwrap();
        let applied = local_ban_start(&dir, 1).unwrap();
        let ip = "192.0.2.42".parse().unwrap();
        assert!(book.peer_banned_at(&ip, applied + 86_399));
        assert!(!book.peer_banned_at(&ip, applied + 86_400));
        assert!(!book.receive(&ban, Some(&dir)).unwrap());
        let mut restored = Book::new(&public(&signing), true).unwrap();
        restored.restore_through(&dir, 1).unwrap();
        assert_eq!(restored.banned, book.banned);
        assert!(!restored.peer_banned_at(&ip, applied + 86_400));
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn contiguous_signed_log_survives_rotation_and_restart() {
        let initial = keys(0);
        let rotated = keys(5);
        let mut b = Book::new(&public(&initial), true).unwrap();
        let root = std::env::temp_dir().join(format!("scary-alert-{}", std::process::id()));
        fs::create_dir_all(&root).unwrap();
        let info = signed(1, 1, b"\x02hi", &initial);
        assert!(b.receive(&info, Some(&root)).unwrap());
        assert!(!b.receive(&info, Some(&root)).unwrap());
        let rotate = signed(
            2,
            8,
            &hex::decode(public(&rotated).concat()).unwrap(),
            &initial,
        );
        assert!(b.receive(&rotate, Some(&root)).unwrap());
        assert!(b.receive(&signed(3, 1, b"\x02no", &initial), None).is_err());
        let mut r = Book::new(&public(&initial), true).unwrap();
        r.restore_through(&root, 2).unwrap();
        assert_eq!(r.applied(), 2);
        assert!(!r.ready());
        assert!(r
            .receive(&signed(3, 1, b"\x02ok", &rotated), Some(&root))
            .unwrap());
        assert!(r.receive(&signed(5, 1, b"\x02hi", &rotated), None).is_err());
        fs::remove_dir_all(root).unwrap();
    }
}
