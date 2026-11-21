//! In-memory witnessed-output chain view and conservative candidate policy.
mod stratum;
pub(crate) mod wire;
pub use stratum::run_stratum;

use std::collections::{BTreeMap, HashMap, HashSet, VecDeque};
use std::time::{SystemTime, UNIX_EPOCH};

use anyhow::{ensure, Context, Result};
use bsv::primitives::hash::hash256;
use bsv::script::{LockingScript, Spend, SpendParams};
use bsv::transaction::transaction::Transaction;
use serde_json::{json, Value};
use wire::{Event, Hash};

const MAX_BODY: usize = 64 << 20;
const MAX_WATCHED: usize = 100_000;
const MAX_PENDING: usize = 128;
const MAX_CANDIDATE_TX_BYTES: usize = 4_000_000;
const MAX_MONEY: u64 = 21_000_000 * 100_000_000;
const HEADER_WINDOW: usize = 147;

type Outpoint = (Hash, u32);

struct Coin {
    script: [u8; 25],
    value: u64,
    height: u32,
    coinbase: bool,
}

#[derive(Clone)]
struct Entry {
    raw: Vec<u8>,
    inputs: Vec<Outpoint>,
    fee: u64,
}

pub struct Mining {
    regtest: bool,
    payout: Vec<u8>,
    height: Option<u32>,
    awaiting_height: Option<u32>,
    headers: VecDeque<[u8; 80]>,
    coins: HashMap<Outpoint, Coin>,
    pending: BTreeMap<Hash, Entry>,
    revision: u64,
    solved_on_tip: bool,
    suspended_reason: Option<String>,
    accepted_shares: u64,
    solved_blocks: u64,
    omitted_outputs: u64,
}

pub(super) fn now() -> Result<u32> {
    Ok(SystemTime::now()
        .duration_since(UNIX_EPOCH)?
        .as_secs()
        .try_into()?)
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
        ensure!(
            payout_script.is_empty() || payout_script.len() <= 10_000,
            "payout script too long"
        );
        Ok(Self {
            regtest,
            payout: payout_script,
            height: None,
            awaiting_height: None,
            headers: VecDeque::new(),
            coins: HashMap::new(),
            pending: BTreeMap::new(),
            revision: 0,
            solved_on_tip: false,
            suspended_reason: None,
            accepted_shares: 0,
            solved_blocks: 0,
            omitted_outputs: 0,
        })
    }

    pub fn suspend(&mut self) {
        self.height = None;
        self.awaiting_height = None;
        self.headers.clear();
        self.coins.clear();
        self.pending.clear();
        self.revision += 1;
        self.solved_on_tip = false;
        self.suspended_reason = Some("coverage interrupted".into());
    }

    /// Retire old work as soon as a peer reports a higher tip. Keep the witnessed
    /// coins until the arriving bodies either confirm continuity or reveal a gap.
    pub fn announced_height(&mut self, height: u32) {
        if self.height.is_some_and(|tip| height > tip)
            && self.awaiting_height.is_none_or(|waiting| height > waiting)
        {
            self.awaiting_height = Some(height);
            self.revision += 1;
        }
    }

    pub fn observe(&mut self, height: u32, raw: &[u8]) -> Result<()> {
        let result = self.connect(height, raw);
        if let Err(error) = &result {
            self.suspend();
            self.suspended_reason = Some(error.to_string());
        }
        result
    }

    fn connect(&mut self, height: u32, raw: &[u8]) -> Result<()> {
        ensure!(
            raw.len() <= MAX_BODY,
            "block exceeds the 64 MiB observation limit"
        );
        let mut r = raw;
        let header: [u8; 80] = wire::take(&mut r, 80)?.try_into()?;
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
        if let (Some(prev), Some(h)) = (self.headers.back(), self.height) {
            if prev == &header && h == height {
                return Ok(());
            }
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
        if self.regtest {
            ensure!(target == limit, "wrong regtest target");
        }
        let n = wire::varint(&mut r)?;
        ensure!(
            n != 0 && n <= r.len() / 60,
            "invalid block transaction count"
        );
        let mut txs = Vec::with_capacity(n);
        for i in 0..n {
            let mut null_inputs = 0;
            let mut inputs = 0;
            let mut coinbase_height = false;
            let raw_tx = wire::transaction(&mut r, |event| {
                if let Event::Input(txid, index, script) = event {
                    inputs += 1;
                    if txid == [0; 32] && index == u32::MAX {
                        null_inputs += 1;
                        coinbase_height = script.starts_with(&wire::height_push(height));
                    }
                }
            })?;
            if i == 0 {
                ensure!(null_inputs == 1 && inputs == 1, "missing first coinbase");
                if !self.regtest {
                    ensure!(coinbase_height, "coinbase height differs from announcement");
                }
            } else {
                ensure!(null_inputs == 0, "extra coinbase input");
            }
            txs.push((hash256(raw_tx), raw_tx));
        }
        ensure!(r.is_empty(), "trailing block bytes");
        let (root, _) = wire::merkle(txs.iter().map(|(id, _)| *id).collect())?;
        ensure!(header[36..68] == root, "block merkle root mismatch");

        // All framing, chain links and body commitments have been checked before
        // publishing any effects. Visit every input, even for untracked scripts.
        for (pos, (txid, body)) in txs.into_iter().enumerate() {
            wire::transaction(&mut &body[..], |event| match event {
                Event::Input(id, i, _) => {
                    self.coins.remove(&(id, i));
                }
                Event::Output(i, value, script) if wire::p2pkh(script) && value <= MAX_MONEY => {
                    if self.coins.len() < MAX_WATCHED {
                        self.coins.insert(
                            (txid, i),
                            Coin {
                                script: script.try_into().unwrap(),
                                value,
                                height,
                                coinbase: pos == 0,
                            },
                        );
                    } else {
                        self.omitted_outputs += 1;
                    }
                }
                _ => {}
            })?;
        }
        self.pending
            .retain(|_, e| e.inputs.iter().all(|p| self.coins.contains_key(p)));
        self.height = Some(height);
        self.awaiting_height = self.awaiting_height.filter(|waiting| *waiting > height);
        if self.headers.len() == HEADER_WINDOW {
            self.headers.pop_front();
        }
        self.headers.push_back(header);
        self.suspended_reason = None;
        self.solved_on_tip = false;
        self.revision += 1;
        Ok(())
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

    pub fn submit_tx(&mut self, raw: &[u8]) -> Result<String> {
        ensure!(
            raw.len() <= 100_000,
            "transaction exceeds alpha admission size"
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
                && raw.len() + self.pending.values().map(|e| e.raw.len()).sum::<usize>()
                    <= MAX_CANDIDATE_TX_BYTES,
            "candidate admission capacity reached"
        );

        let tx = Transaction::from_binary(&mut &raw[..])?;
        ensure!(
            tx.inputs.len() <= 64 && tx.outputs.len() <= 128,
            "alpha transaction input/output limit exceeded"
        );
        ensure!(
            tx.version == 1 && tx.lock_time == 0,
            "alpha admits only final version-one transactions"
        );
        ensure!(
            tx.to_bytes()? == raw,
            "SDK transaction does not round-trip canonically"
        );
        let mut used = HashSet::new();
        let mut inputs = Vec::new();
        let mut total_in = 0u64;
        for (i, input) in tx.inputs.iter().enumerate() {
            ensure!(input.sequence == u32::MAX, "non-final sequence");
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
                !self.pending.values().any(|p| p.inputs.contains(&outpoint)),
                "conflicting pending spend"
            );
            let coin = self
                .coins
                .get(&outpoint)
                .context("input missing from witnessed spendable set")?;
            ensure!(
                height > coin.height,
                "creation block has no observed successor"
            );
            ensure!(
                !coin.coinbase || height - coin.height >= 99,
                "immature coinbase"
            );
            let unlock = input
                .unlocking_script
                .clone()
                .context("missing unlocking script")?;
            let bytes = unlock.to_binary();
            ensure!(
                standard_unlock(&bytes),
                "expected canonical signature with ALL|FORKID and compressed key"
            );
            let other_inputs = tx
                .inputs
                .iter()
                .enumerate()
                .filter(|(j, _)| *j != i)
                .map(|(_, value)| value.clone())
                .collect();
            let mut spend = Spend::new(SpendParams {
                locking_script: LockingScript::from_binary(&coin.script),
                unlocking_script: unlock,
                source_txid: source_id.into(),
                source_output_index: input.source_output_index as usize,
                source_satoshis: coin.value,
                transaction_version: tx.version,
                transaction_lock_time: tx.lock_time,
                transaction_sequence: input.sequence,
                other_inputs,
                other_outputs: tx.outputs.clone(),
                input_index: i,
            });
            ensure!(spend.validate()?, "locking script rejected spend");
            total_in = total_in
                .checked_add(coin.value)
                .filter(|n| *n <= MAX_MONEY)
                .context("input value overflow")?;
            inputs.push(outpoint);
        }
        let mut total_out = 0u64;
        for output in &tx.outputs {
            ensure!(
                wire::p2pkh(&output.locking_script.to_binary()),
                "alpha admits only P2PKH outputs"
            );
            total_out = total_out
                .checked_add(output.satoshis.context("missing output value")?)
                .filter(|n| *n <= MAX_MONEY)
                .context("output value overflow")?;
        }
        let fee = total_in
            .checked_sub(total_out)
            .context("transaction creates money")?;
        ensure!(fee > 0, "candidate requires a fee-paying transaction");
        self.pending.insert(
            id,
            Entry {
                raw: raw.to_vec(),
                inputs,
                fee,
            },
        );
        self.revision += 1;
        Ok(wire::display_hash(id))
    }

    pub fn status(&self) -> Value {
        json!({
            "height": self.height, "tip_hash": self.headers.back().map(|h| wire::display_hash(hash256(h))),
            "headers": self.headers.len(), "awaiting_height": self.awaiting_height,
            "witnessed_outputs": self.coins.len(), "omitted_outputs": self.omitted_outputs,
            "pending_transactions": self.pending.len(), "accepted_shares": self.accepted_shares,
            "solved_blocks": self.solved_blocks, "suspended_reason": self.suspended_reason
        })
    }
}

fn standard_unlock(bytes: &[u8]) -> bool {
    // Two minimal direct pushes. Enforce strict DER lengths before running the
    // SDK's interpreter (which also checks the cryptographic signature and low-S).
    let Some(&siglen) = bytes.first() else {
        return false;
    };
    let n = siglen as usize;
    if !(9..=73).contains(&n)
        || bytes.len() != n + 35
        || bytes[n] != 0x41
        || bytes[n + 1] != 33
        || !matches!(bytes[n + 2], 2 | 3)
    {
        return false;
    }
    let d = &bytes[1..n]; // DER only, dropping the sighash byte.
    if d[0] != 0x30 || d[1] as usize != d.len() - 2 || d[2] != 2 {
        return false;
    }
    let nr = d[3] as usize;
    if nr == 0
        || 6 + nr >= d.len()
        || d[4] & 0x80 != 0
        || (nr > 1 && d[4] == 0 && d[5] & 0x80 == 0)
        || d[4 + nr] != 2
    {
        return false;
    }
    let ns = d[5 + nr] as usize;
    ns > 0
        && nr + ns + 6 == d.len()
        && d[6 + nr] & 0x80 == 0
        && !(ns > 1 && d[6 + nr] == 0 && d[7 + nr] & 0x80 == 0)
}

#[cfg(test)]
mod tests;
