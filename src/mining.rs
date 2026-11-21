//! In-memory witnessed-output chain view and conservative candidate policy.
mod chain;
mod interpreter;
mod lifecycle;
mod store;
mod stratum;
mod validation;
pub(crate) mod wire;
pub use stratum::run_stratum;

use std::collections::{BTreeMap, HashMap, HashSet, VecDeque};
use std::sync::Arc;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use anyhow::{ensure, Context, Result};
use bsv::primitives::hash::hash256;
use bsv::transaction::transaction::Transaction;
use serde_json::{json, Value};
use wire::{Event, Hash};

#[derive(serde::Serialize)]
pub(crate) struct Capacity {
    pub block_bytes: usize,
    pub transaction_bytes: usize,
    pub witnessed_outputs: usize,
}
impl Capacity {
    fn load() -> Result<Self> {
        fn read(key: &str, default: usize) -> Result<usize> {
            let n = match std::env::var(key) {
                Ok(v) => v.parse().with_context(|| format!("invalid {key}"))?,
                Err(std::env::VarError::NotPresent) => default,
                Err(e) => return Err(e.into()),
            };
            ensure!(n > 0, "{key} must be positive");
            Ok(n)
        }
        let block_bytes = read("SCARY_MAX_BLOCK_BYTES", 256 << 20)?;
        ensure!(
            block_bytes >= 1024,
            "block capacity must be at least 1024 bytes"
        );
        let transaction_bytes = read("SCARY_MAX_TRANSACTION_BYTES", (16 << 20).min(block_bytes))?;
        ensure!(
            transaction_bytes <= block_bytes,
            "transaction capacity exceeds block capacity"
        );
        Ok(Self {
            block_bytes,
            transaction_bytes,
            witnessed_outputs: read("SCARY_MAX_WITNESSED_OUTPUTS", 1_000_000)?,
        })
    }
}
static CAPACITY: std::sync::OnceLock<Capacity> = std::sync::OnceLock::new();
pub(crate) fn configure_capacity() -> Result<()> {
    if CAPACITY.get().is_none() {
        let cap = Capacity::load()?;
        let _ = CAPACITY.set(cap);
    }
    Ok(())
}
fn capacity() -> &'static Capacity {
    CAPACITY.get_or_init(|| Capacity::load().expect("checked process capacity"))
}
pub(crate) fn body_limit() -> usize {
    capacity().block_bytes
}
pub(crate) fn transaction_limit() -> usize {
    capacity().transaction_bytes
}
const MAX_WATCHED: usize = 100_000;
const MAX_PENDING: usize = 65_536;
const MAX_CANDIDATE_TX_BYTES: usize = 128 << 20;
const MAX_MONEY: u64 = 21_000_000 * 100_000_000;
const HEADER_WINDOW: usize = 147;

type Outpoint = (Hash, u32);
type ParsedBody<'a> = (Vec<(Hash, &'a [u8])>, Option<&'static str>);

#[derive(Clone, serde::Serialize, serde::Deserialize)]
struct Coin {
    #[serde(with = "store::arc_bytes")]
    script: Arc<[u8]>,
    value: u64,
    height: u32,
    coinbase: bool,
    confiscation: bool,
}

#[derive(Clone, serde::Serialize, serde::Deserialize)]
struct Entry {
    #[serde(with = "store::arc_bytes")]
    raw: Arc<[u8]>,
    inputs: Vec<Outpoint>,
    fee: u64,
    #[serde(default)]
    order: u64,
}

#[derive(serde::Serialize, serde::Deserialize)]
struct SolvedBody {
    #[serde(default)]
    verified_sequence: Option<u32>,
    height: u32,
    hash: Hash,
    #[serde(with = "store::arc_bytes")]
    raw: Arc<[u8]>,
}

#[derive(serde::Serialize, serde::Deserialize)]
pub struct Mining {
    #[serde(skip)]
    path: Option<std::path::PathBuf>,
    #[serde(skip)]
    pub(crate) mining_identity: [u8; 32],
    #[serde(skip)]
    storage_failure: Option<String>,
    #[serde(skip, default = "solution_watch")]
    solution_watch: tokio::sync::watch::Sender<()>,
    #[serde(skip)]
    live_peers: HashSet<String>,
    #[serde(skip)]
    supervise_peers: bool,
    #[serde(skip)]
    sv_bodies_only: bool,
    #[serde(skip)]
    confirmed_since_disconnect: bool,
    applied_sequence: u32,
    solution_body: Option<SolvedBody>,
    #[serde(skip, default = "crate::alerts::unconfigured")]
    pub(crate) directives: crate::alerts::Book,
    regtest: bool,
    payout: Vec<u8>,
    height: Option<u32>,
    #[serde(default)]
    active_since_entry: HashMap<Hash, u32>,
    #[serde(default)]
    invalid_descendants: HashSet<Hash>,
    #[serde(default)]
    soft_freezes: HashSet<Hash>,
    #[serde(default)]
    invalidated_boundary: bool,
    awaiting_height: Option<u32>,
    #[serde(with = "store::headers")]
    headers: VecDeque<[u8; 80]>,
    coins: HashMap<Outpoint, Coin>,
    pending: BTreeMap<Hash, Entry>,
    #[serde(skip)]
    pending_inputs: HashSet<Outpoint>,
    #[serde(skip)]
    pending_outputs: HashMap<Outpoint, Coin>,
    #[serde(skip)]
    pending_bytes: usize,
    // Prefix commitments follow the same admission order as ordered_pending().
    #[serde(skip)]
    pending_prefixes: Vec<Hash>,
    #[serde(skip)]
    worker_template: Option<Arc<stratum::Job>>,
    revision: u64,
    solved_on_tip: bool,
    suspended_reason: Option<String>,
    accepted_shares: u64,
    solved_blocks: u64,
    omitted_outputs: u64,
    spent: validation::Spent,
    recent_invalid: VecDeque<(Hash, u32, String)>,
    chain: chain::RecentChain,
    lifecycle: lifecycle::Lifecycle,
    parent_epoch: u64,
    blocked: bool,
    validation_window: VecDeque<validation::Observation>,
    min_observed_blocks: usize,
}

fn solution_watch() -> tokio::sync::watch::Sender<()> {
    tokio::sync::watch::channel(()).0
}

pub(super) fn now() -> Result<u32> {
    Ok(SystemTime::now()
        .duration_since(UNIX_EPOCH)?
        .as_secs()
        .try_into()?)
}

// Hold locally originated work during a wall-clock step. The monotonic anchor
// is process-local. Operators separately synchronize the host before startup.
const MAX_CLOCK_STEP_SECONDS: i64 = 120;
static CLOCK_ANCHOR: std::sync::OnceLock<(Instant, u32)> = std::sync::OnceLock::new();

fn clock_step() -> Option<i64> {
    let wall = now().ok()?;
    let (mono, initial) = CLOCK_ANCHOR.get_or_init(|| (Instant::now(), wall));
    let elapsed = i64::try_from(mono.elapsed().as_secs()).ok()?;
    Some(i64::from(wall) - i64::from(*initial) - elapsed)
}

fn clock_ready() -> bool {
    clock_step().is_some_and(|step| step.abs() <= MAX_CLOCK_STEP_SECONDS)
}

fn timestamp(header: &[u8; 80]) -> u32 {
    u32::from_le_bytes(header[68..72].try_into().unwrap())
}

fn bits(header: &[u8; 80]) -> u32 {
    u32::from_le_bytes(header[72..76].try_into().unwrap())
}

fn subsidy(height: u32, regtest: bool) -> u64 {
    let interval = if regtest { 150 } else { 210_000 };
    5_000_000_000u64.checked_shr(height / interval).unwrap_or(0)
}

impl Mining {
    pub fn new(regtest: bool, payout_script: Vec<u8>) -> Result<Self> {
        clock_step().context("read initial wall clock")?;
        ensure!(
            payout_script.is_empty() || payout_script.len() <= 10_000,
            "payout script too long"
        );
        Ok(Self {
            path: None,
            mining_identity: [0; 32],
            storage_failure: None,
            solution_watch: solution_watch(),
            live_peers: HashSet::new(),
            supervise_peers: false,
            sv_bodies_only: false,
            confirmed_since_disconnect: false,
            applied_sequence: 0,
            solution_body: None,
            directives: crate::alerts::Book::new(
                &crate::alerts::MAIN_KEYS.map(String::from),
                !regtest,
            )?,
            regtest,
            payout: payout_script,
            height: None,
            active_since_entry: HashMap::new(),
            invalid_descendants: HashSet::new(),
            soft_freezes: HashSet::new(),
            invalidated_boundary: false,
            awaiting_height: None,
            headers: VecDeque::new(),
            coins: HashMap::new(),
            pending: BTreeMap::new(),
            pending_inputs: HashSet::new(),
            pending_outputs: HashMap::new(),
            pending_bytes: 0,
            pending_prefixes: Vec::new(),
            worker_template: None,
            revision: 0,
            solved_on_tip: false,
            suspended_reason: None,
            accepted_shares: 0,
            solved_blocks: 0,
            omitted_outputs: 0,
            spent: validation::Spent::default(),
            recent_invalid: VecDeque::new(),
            chain: chain::RecentChain::default(),
            lifecycle: lifecycle::Lifecycle::default(),
            parent_epoch: 0,
            blocked: false,
            validation_window: VecDeque::new(),
            min_observed_blocks: 1,
        })
    }

    pub fn set_min_observed_blocks(&mut self, minimum: usize) -> Result<()> {
        ensure!(
            (1..=HEADER_WINDOW).contains(&minimum),
            "minimum observation window must be 1..147"
        );
        self.min_observed_blocks = minimum;
        Ok(())
    }

    fn observation_window_ready(&self) -> bool {
        self.peer_ready()
            && self.storage_failure.is_none()
            && clock_ready()
            && self.directives.ready()
            && self.applied_sequence == self.directives.applied()
            && !self.blocked
            && !self.invalidated_boundary
            && self.awaiting_height.is_none()
            && self.headers.len() >= self.min_observed_blocks
            && (self.regtest || self.headers.len() >= HEADER_WINDOW)
    }

    pub(crate) fn supervise_data_peers(&mut self, sv_bodies_only: bool) {
        self.supervise_peers = true;
        self.sv_bodies_only = sv_bodies_only;
    }
    fn peer_ready(&self) -> bool {
        !self.supervise_peers || (!self.live_peers.is_empty() && self.confirmed_since_disconnect)
    }
    pub(crate) fn peer_connection(&mut self, peer: &str, connected: bool) {
        if self.sv_bodies_only && !peer.starts_with("sv:") {
            return;
        }
        if connected {
            self.live_peers.insert(peer.to_owned());
        } else if self.live_peers.remove(peer) && self.live_peers.is_empty() {
            self.confirmed_since_disconnect = false;
            self.parent_epoch += 1;
            self.revision += 1;
        }
    }
    pub(crate) fn confirm_current_tip(&mut self, hash: &str, height: u32) {
        if !self.live_peers.is_empty()
            && self.height == Some(height)
            && self.tip().is_some_and(|id| wire::display_hash(id) == hash)
            && !self.confirmed_since_disconnect
        {
            self.confirmed_since_disconnect = true;
            self.revision += 1;
        }
    }
    pub(crate) fn header_probe(&self) -> Option<(Hash, Hash, u32)> {
        let header = self.headers.back()?;
        Some((
            header[4..36].try_into().ok()?,
            hash256(header),
            self.height?,
        ))
    }

    pub fn suspend(&mut self) {
        self.chain = chain::RecentChain::default();
        self.lifecycle.lost_view();
        self.clear_active();
    }

    fn clear_active(&mut self) {
        self.blocked = false;
        self.parent_epoch += 1;
        self.height = None;
        self.awaiting_height = None;
        self.headers.clear();
        self.active_since_entry.clear();
        self.coins.clear();
        self.spent = validation::Spent::default();
        self.validation_window.clear();
        self.clear_pending();
        self.revision += 1;
        self.solved_on_tip = false;
        self.suspended_reason = Some("coverage interrupted".into());
    }

    /// Retire old work as soon as a peer reports a higher tip. Keep the witnessed
    /// coins until the arriving bodies either confirm continuity or reveal a gap.
    fn announced_height(&mut self, height: u32) {
        if self.height.is_some_and(|tip| height > tip)
            && self.awaiting_height.is_none_or(|waiting| height > waiting)
        {
            self.awaiting_height = Some(height);
            self.revision += 1;
        }
    }

    pub fn announced_header(&mut self, height: u32, header: &[u8]) {
        if self.inspect_header(height, header).is_ok() {
            self.announced_height(height);
        }
    }

    fn remember_invalid(&mut self, hash: Hash, height: u32, reason: String) {
        if self.recent_invalid.len() == HEADER_WINDOW {
            self.recent_invalid.pop_front();
        }
        self.recent_invalid.push_back((hash, height, reason));
    }

    fn check_header_version(&self, height: u32, header: &[u8; 80]) -> Result<()> {
        let version = i32::from_le_bytes(header[..4].try_into()?);
        // Mainnet Chronicle follows both version-floor activations. Regtest
        // can activate Chronicle at height 2 while these floors are pending.
        ensure!(
            version >= 4 || (self.regtest && (height < 1251 || (height < 1351 && version >= 3))),
            "obsolete block version"
        );
        Ok(())
    }

    fn inspect_header(&self, height: u32, raw: &[u8]) -> Result<[u8; 80]> {
        ensure!(
            raw.len() <= body_limit(),
            "block exceeds configured observation capacity"
        );
        let mut r = raw;
        let header: [u8; 80] = wire::take(&mut r, 80)?.try_into()?;
        self.check_header_version(height, &header)?;
        let hash = hash256(&header);
        let target = bits(&header);
        let limit = if self.regtest { 0x207fffff } else { 0x1d00ffff };
        ensure!(
            wire::meets_target(wire::as_le_target(target)?, limit)?,
            "target above network limit"
        );
        ensure!(
            wire::meets_target(hash, target)?,
            "header fails proof of work"
        );
        ensure!(
            timestamp(&header) <= now()?.saturating_add(7200),
            "header time is too far in the future"
        );
        if self.regtest {
            ensure!(target == limit, "wrong regtest target");
        }
        Ok(header)
    }

    fn connect(
        &mut self,
        height: u32,
        raw: crate::body::BlockBody,
        header: [u8; 80],
    ) -> Result<()> {
        let hash = hash256(&header);
        let target = bits(&header);
        if let (Some(prev), Some(h)) = (self.headers.back(), self.height) {
            ensure!(
                h.checked_add(1) == Some(height) && header[4..36] == hash256(prev),
                "block does not extend the observed tip"
            );
            if self.regtest || self.headers.len() >= HEADER_WINDOW {
                ensure!(
                    target == self.next_bits()?,
                    "unexpected target for extension"
                );
            }
            if self.headers.len() >= 11 {
                ensure!(
                    timestamp(&header) > self.median_time(),
                    "header time below median boundary"
                );
            }
        }
        let (txs, coinbase_error) = self.inspect_body(height, &header, &raw)?;
        let applied = match coinbase_error {
            Some(reason) => Err(validation::InvalidBody(reason).into()),
            None => self.apply_observed(self.block_context(height, timestamp(&header)), hash, txs),
        };
        let applied = applied.inspect_err(|error| {
            if self.height.is_some() && error.downcast_ref::<validation::InvalidBody>().is_some() {
                self.remember_invalid(hash, height, error.to_string());
            }
        })?;
        let previous_header = if self.headers.len() == HEADER_WINDOW {
            self.headers.pop_front()
        } else {
            None
        };
        self.chain.record(chain::AppliedBlock {
            height,
            raw,
            applied,
            previous_header,
        });
        self.height = Some(height);
        self.active_since_entry.insert(hash, height);
        self.headers.push_back(header);
        Ok(())
    }

    fn inspect_body<'a>(
        &self,
        height: u32,
        header: &[u8; 80],
        raw: &'a [u8],
    ) -> Result<ParsedBody<'a>> {
        let mut r = &raw[80..];
        let n = wire::varint(&mut r)?;
        ensure!(
            n != 0 && n <= r.len() / 60,
            "invalid block transaction count"
        );
        let mut txs = Vec::with_capacity(n);
        let mut coinbase_error = None;
        for i in 0..n {
            let mut null_inputs = 0;
            let mut inputs = 0;
            let mut coinbase_height = false;
            let mut coinbase_size_ok = false;
            let raw_tx = wire::transaction(&mut r, |event| {
                if let Event::Input(txid, index, script) = event {
                    inputs += 1;
                    if txid == [0; 32] && index == u32::MAX {
                        null_inputs += 1;
                        coinbase_size_ok = (2..=100).contains(&script.len());
                        coinbase_height = script.starts_with(&wire::height_push(height));
                    }
                }
            })?;
            if i == 0 {
                if null_inputs != 1 || inputs != 1 {
                    coinbase_error = Some("missing first coinbase");
                } else if !coinbase_size_ok {
                    coinbase_error = Some("invalid coinbase script size");
                } else if (!self.regtest || height >= 100_000_000) && !coinbase_height {
                    coinbase_error = Some("coinbase height differs from announcement");
                }
            } else if null_inputs != 0 {
                coinbase_error = Some("extra coinbase input");
            }
            if raw_tx.len() > 1_000_000_000 {
                coinbase_error = Some("transaction exceeds consensus size");
            }
            txs.push((hash256(raw_tx), raw_tx));
        }
        ensure!(r.is_empty(), "trailing block bytes");
        let (root, _) = wire::merkle(txs.iter().map(|(id, _)| *id).collect())?;
        ensure!(header[36..68] == root, "block merkle root mismatch");

        Ok((txs, coinbase_error))
    }

    pub(super) fn verify_own_block(&self, height: u32, raw: &[u8], solved: bool) -> Result<()> {
        ensure!(
            self.observation_window_ready()
                && self.height.and_then(|h| h.checked_add(1)) == Some(height),
            "candidate context changed"
        );
        let header: [u8; 80] = raw.get(..80).context("short candidate")?.try_into()?;
        let parent = self.tip().context("no candidate parent")?;
        ensure!(
            header[4..36] == parent && bits(&header) == self.next_bits()?,
            "candidate parent or difficulty mismatch"
        );
        ensure!(
            timestamp(&header) > self.median_time()
                && timestamp(&header) <= now()?.saturating_add(7200),
            "candidate time out of range"
        );
        ensure!(
            raw.len() <= body_limit(),
            "candidate exceeds configured body capacity"
        );
        if solved {
            let hash = hash256(&header);
            ensure!(
                !self.directives.invalid.contains(&hash)
                    && !self.invalid_descendants.contains(&hash),
                "explicitly invalidated candidate"
            );
            self.inspect_header(height, raw)?;
        }
        if !solved {
            self.check_header_version(height, &header)?;
        }
        let (txs, error) = self.inspect_body(height, &header, raw)?;
        ensure!(error.is_none(), "invalid candidate coinbase");
        let context = self.block_context(height, timestamp(&header));
        ensure!(
            wire::final_under(txs[0].1, height, context.time_cutoff)? == Some(true),
            "candidate coinbase finality unresolved or non-final"
        );
        let mut fees = 0u64;
        let mut used = HashSet::new();
        let mut created = HashMap::new();
        for (id, tx) in txs.iter().skip(1) {
            ensure!(
                self.pending.contains_key(id),
                "candidate transaction retired"
            );
            let assessment = self.assess_candidate(tx, context, &mut used, &created)?;
            Self::collect_outputs(&mut created, *id, tx, height)?;
            fees = fees
                .checked_add(assessment)
                .context("candidate fee overflow")?;
        }
        let mut reward = Some(0u64);
        let mut coinbase_script_valid = false;
        let mut input = txs[0].1;
        wire::transaction(&mut input, |e| match e {
            Event::Output(_, value, _) => reward = reward.and_then(|r| r.checked_add(value)),
            Event::Input(_, _, s) => {
                coinbase_script_valid =
                    (2..=100).contains(&s.len()) && s.starts_with(&wire::height_push(height))
            }
        })?;
        ensure!(
            coinbase_script_valid
                && reward
                    .is_some_and(|v| v <= MAX_MONEY
                        && v <= subsidy(height, self.regtest).saturating_add(fees)),
            "invalid candidate coinbase height, script or amount"
        );
        Ok(())
    }

    fn parent_median(&self) -> Option<u32> {
        (self.headers.len() >= 11).then(|| self.median_time())
    }

    fn median_time(&self) -> u32 {
        let mut times: Vec<_> = self.headers.iter().rev().take(11).map(timestamp).collect();
        times.sort_unstable();
        times.get(times.len() / 2).copied().unwrap_or(0)
    }

    fn next_bits(&self) -> Result<u32> {
        let ordered: Vec<_> = self.headers.iter().copied().collect();
        crate::difficulty::next_bits(&ordered, self.regtest)
    }

    fn ordered_pending(&self) -> Vec<(Hash, &Entry)> {
        let mut entries: Vec<_> = self.pending.iter().map(|(id, e)| (*id, e)).collect();
        entries.sort_by_key(|(_, e)| e.order);
        entries
    }
    fn clear_pending(&mut self) {
        self.pending.clear();
        self.pending_prefixes.clear();
        self.pending_inputs.clear();
        self.pending_outputs.clear();
        self.pending_bytes = 0;
    }
    fn collect_outputs(
        outputs: &mut HashMap<Outpoint, Coin>,
        id: Hash,
        raw: &[u8],
        height: u32,
    ) -> Result<()> {
        let confiscation = crate::alerts::confiscation(raw)?.is_some();
        wire::transaction(&mut &raw[..], |ev| {
            if let Event::Output(n, value, script) = ev {
                if !script.starts_with(&[0, 0x6a]) {
                    outputs.insert(
                        (id, n),
                        Coin {
                            value,
                            script: Arc::from(script),
                            height,
                            coinbase: false,
                            confiscation,
                        },
                    );
                }
            }
        })?;
        Ok(())
    }
    fn recheck_pending(&mut self) {
        let offers: Vec<_> = self
            .ordered_pending()
            .into_iter()
            .map(|(_, e)| e.raw.clone())
            .collect();
        self.clear_pending();
        for raw in offers {
            let _ = self.admit(&raw);
        }
    }
    pub fn submit_tx(&mut self, raw: &[u8]) -> Result<String> {
        ensure!(self.storage_failure.is_none(), "durable state write failed");
        ensure!(self.directives.ready(), "directive state is not ready");
        ensure!(
            !self.blocked && self.awaiting_height.is_none(),
            "incomplete active branch coverage"
        );
        self.admit(raw)
    }

    fn admit(&mut self, raw: &[u8]) -> Result<String> {
        ensure!(
            raw.len() <= transaction_limit(),
            "transaction exceeds configured admission size"
        );
        let mut framing = raw;
        wire::transaction(&mut framing, |_| {})?;
        ensure!(framing.is_empty(), "trailing transaction data");
        let height = self.height.context("no observed tip")?;
        let id = hash256(raw);
        if self.pending.contains_key(&id) {
            return Ok(wire::display_hash(id));
        }
        ensure!(
            self.pending.len() < MAX_PENDING
                && raw.len() + self.pending_bytes <= MAX_CANDIDATE_TX_BYTES,
            "candidate admission capacity reached"
        );

        let tx = Transaction::from_binary(&mut &raw[..])?;
        ensure!(tx.to_bytes()? == raw, "transaction serialization mismatch");
        let candidate_height = height.checked_add(1).context("height overflow")?;
        ensure!(
            wire::final_under(raw, candidate_height, self.parent_median())? == Some(true),
            "transaction finality unresolved or non-final under mempool policy"
        );
        let is_confiscation = crate::alerts::confiscation(raw)?.is_some();
        if is_confiscation {
            ensure!(
                self.directives.authorized(&id, candidate_height),
                "unauthorized confiscation"
            );
        }
        let mut prevouts = Vec::with_capacity(tx.inputs.len());
        let mut used = HashSet::new();
        let mut inputs = Vec::new();
        let mut total_in = 0u64;
        for input in &tx.inputs {
            let source_id = input
                .source_txid
                .as_deref()
                .context("missing source transaction ID")?;
            let mut source: Hash = hex::decode(source_id)?
                .try_into()
                .map_err(|_| anyhow::anyhow!("bad txid"))?;
            source.reverse();
            let outpoint = (source, input.source_output_index);
            ensure!(used.insert(outpoint), "duplicate input");
            ensure!(
                !self.pending_inputs.contains(&outpoint),
                "conflicting pending spend"
            );
            let coin = self
                .coins
                .get(&outpoint)
                .or_else(|| self.pending_outputs.get(&outpoint))
                .context("input missing from witnessed spendable set")?;
            ensure!(
                height > coin.height || self.pending_outputs.contains_key(&outpoint),
                "creation block has no observed successor"
            );
            ensure!(
                !coin.coinbase || height - coin.height >= 99,
                "immature coinbase"
            );
            ensure!(
                coin.height >= interpreter::activation(self.regtest),
                "pre-Chronicle source output"
            );
            ensure!(
                !coin.confiscation || candidate_height - coin.height >= 1000,
                "immature confiscation"
            );
            ensure!(
                is_confiscation || !self.directives.frozen(&outpoint, candidate_height, true),
                "frozen input"
            );
            prevouts.push(coin);
            total_in = total_in
                .checked_add(coin.value)
                .filter(|n| *n <= MAX_MONEY)
                .context("input value overflow")?;
            inputs.push(outpoint);
        }
        let mut total_out = 0u64;
        for output in &tx.outputs {
            ensure!(
                !wire::p2sh(&output.locking_script.to_binary()),
                "post-Genesis P2SH output"
            );
            total_out = total_out
                .checked_add(output.satoshis.context("missing output value")?)
                .filter(|n| *n <= MAX_MONEY)
                .context("output value overflow")?;
        }
        let fee = total_in
            .checked_sub(total_out)
            .context("transaction creates money")?;
        ensure!(
            is_confiscation || fee > 0,
            "candidate requires a fee-paying transaction"
        );
        if !is_confiscation {
            let (ef, heights) = interpreter::extended(raw, &prevouts)?;
            interpreter::transaction(self.regtest, &ef, &heights, height, false)??;
            interpreter::transaction(self.regtest, &ef, &heights, candidate_height, true)??;
        }
        Self::collect_outputs(&mut self.pending_outputs, id, raw, candidate_height)?;
        self.pending_inputs.extend(inputs.iter().copied());
        self.pending_bytes += raw.len();
        self.pending_prefixes.push(wire::joined(
            self.pending_prefixes.last().copied().unwrap_or_default(),
            id,
        ));
        self.pending.insert(
            id,
            Entry {
                raw: Arc::from(raw),
                inputs,
                fee,
                order: self.revision,
            },
        );
        self.revision += 1;
        Ok(wire::display_hash(id))
    }

    pub(crate) fn durable_sequence(&self) -> u32 {
        self.applied_sequence
    }
    pub(crate) fn refresh_work(&mut self) {
        self.revision += 1;
    }
    pub(crate) fn work_revision(&self) -> u64 {
        self.revision
    }
    pub(crate) fn directive_receipt(&self, method: &str, params: &Value) -> Result<Value> {
        ensure!(self.path.is_some(), "durable directive storage unavailable");
        self.directives
            .receipt(method, params, self.applied_sequence)
    }
    pub(crate) fn height_hint(&self) -> u32 {
        self.height.unwrap_or(0)
    }
    pub(crate) fn subscribe_solutions(&self) -> tokio::sync::watch::Receiver<()> {
        self.solution_watch.subscribe()
    }

    pub(crate) fn solution_bytes(&self, hash: &Hash) -> Option<&[u8]> {
        self.solution_body
            .as_ref()
            .filter(|s| s.hash == *hash)
            .map(|b| b.raw.as_ref())
    }
    pub(crate) fn solution_height(&self, id: Hash) -> Option<u32> {
        self.solution_body
            .as_ref()
            .filter(|s| s.hash == id)
            .map(|s| s.height)
    }

    pub(crate) fn relayable_solution(&self) -> Option<Hash> {
        let s = self.solution_body.as_ref()?;
        if !self.observation_window_ready()
            || self.applied_sequence != self.directives.applied()
            || self.directives.invalid.contains(&s.hash)
        {
            return None;
        }
        let height = self.height?;
        let tip = self.tip()?;
        if s.hash == tip && height == s.height && s.verified_sequence == Some(self.applied_sequence)
        {
            // Another transport may have delivered and adopted these exact bytes first.
            return Some(s.hash);
        }
        (s.height == height.checked_add(1)?
            && s.raw[4..36] == tip
            && self.verify_own_block(s.height, &s.raw, true).is_ok())
        .then_some(s.hash)
    }
    pub fn status(&self) -> Value {
        let readiness: Vec<&str> = [
            (
                !self.peer_ready(),
                "awaiting current tip from a connected data peer",
            ),
            (self.storage_failure.is_some(), "durable state write failed"),
            (
                !clock_ready(),
                "wall clock differs from monotonic elapsed time",
            ),
            (!self.directives.ready(), "directive state uncertain"),
            (
                self.invalidated_boundary,
                "directive invalidated ancestry beyond undo coverage",
            ),
            (self.blocked, "body coverage interrupted"),
            (
                self.awaiting_height.is_some(),
                "missing newer announced bodies",
            ),
            (
                self.headers.len() < self.min_observed_blocks,
                "observation window incomplete",
            ),
            (
                !self.regtest && self.headers.len() < HEADER_WINDOW,
                "difficulty history incomplete",
            ),
        ]
        .into_iter()
        .filter_map(|(blocked, reason)| blocked.then_some(reason))
        .collect();
        let window_work = self
            .headers
            .iter()
            .try_fold(num_bigint::BigUint::default(), |sum, h| {
                chain::work(h).map(|w| sum + w)
            })
            .ok()
            .map(|n| format!("{n:x}"));
        let first = self
            .height
            .and_then(|h| h.checked_sub(self.headers.len().saturating_sub(1) as u32));

        let (mapped_count, mapped_bytes) = self.chain.mapped_bodies();
        json!({
            "connected_data_peers":self.live_peers.len(),"tip_confirmed_since_disconnect":self.confirmed_since_disconnect,"storage_failure":self.storage_failure,"clock_step_seconds":clock_step(),"clock_step_limit_seconds":MAX_CLOCK_STEP_SECONDS,
            "mapped_bodies":mapped_count,"mapped_body_bytes":mapped_bytes,"capacity":capacity(),"coverage_start":first,"coverage_end":self.height,"window_chainwork":window_work,"expected_next_bits":self.next_bits().ok(),"readiness_reasons":readiness,"durable_directive_sequence":self.applied_sequence,"directives":self.directives.status(),"interpreter": interpreter::build(), "height": self.height, "tip_hash": self.headers.back().map(|h| wire::display_hash(hash256(h))),
            "retired_prefix_height": self.height.filter(|_| self.blocked), "reorgs": self.chain.reorgs, "recoverable_blocks": self.chain.applied.len(), "buffered_bodies": self.chain.buffered_len(), "recent_invalid_blocks": self.recent_invalid.len(), "explicit_soft_freezes": self.soft_freezes.len(), "validation_window": self.validation_window, "min_observed_blocks": self.min_observed_blocks,
            "observation_window_ready": self.observation_window_ready(), "headers": self.headers.len(), "awaiting_height": self.awaiting_height,
            "witnessed_outputs": self.coins.len(), "omitted_outputs": self.omitted_outputs,
            "pending_transactions": self.pending.len(), "accepted_shares": self.accepted_shares,
            "solved_blocks": self.solved_blocks, "suspended_reason": self.suspended_reason, "solutions": self.lifecycle.status()
        })
    }
}

#[cfg(test)]
mod tests;
