//! Local checks against the current contiguous witnessed output set.
use super::*;
use bsv::primitives::transaction_signature::TransactionSignature;
use serde::Serialize;

pub(super) const SCRIPT_BUDGET: usize = 4096;

#[derive(Default, Copy, Clone, Serialize)]
pub(super) struct Checks {
    pub noncoinbase_transactions: u64,
    pub known_inputs: u64,
    pub unknown_inputs: u64,
    pub consumed_inputs: u64,
    pub same_block_inputs: u64,
    pub script_checks_passed: u64,
    pub script_checks_deferred: u64,
    pub script_checks_failed: u64,
    pub amount_checks_passed: u64,
    pub amount_checks_deferred: u64,
    pub amount_checks_failed: u64,
    pub transactions_determinate: u64,
    pub transactions_deferred: u64,
}

#[derive(Serialize)]
pub(super) struct Observation {
    pub height: u32,
    pub hash: String,
    pub coinbase_reward_checked: bool,
    #[serde(flatten)]
    pub checks: Checks,
}

#[derive(Default)]
pub(super) struct Assessment {
    pub checks: Checks,
    pub fee: Option<u64>,
    pub violation: Option<&'static str>,
}

// An entry survives for at most MAX_WATCHED later consumptions. Removing an
// entry before its FIFO turn (a recreated output) must not evict its new entry.
#[derive(Default)]
pub(super) struct Spent {
    ids: HashMap<Outpoint, u64>,
    order: VecDeque<(Outpoint, u64)>,
    serial: u64,
}

pub(super) enum SpendUndo {
    Record {
        previous: Option<u64>,
        evicted: Option<(Outpoint, u64, bool)>,
    },
    Forget(Outpoint, Option<u64>),
}

pub(super) struct Applied {
    pub coins: Vec<(Outpoint, Option<Coin>)>,
    pub spends: Vec<SpendUndo>,
    pub previous_row: Option<Observation>,
    pub omitted: u64,
}

impl Spent {
    pub fn has(&self, p: &Outpoint) -> bool {
        self.ids.contains_key(p)
    }
    pub fn record(&mut self, p: Outpoint) -> SpendUndo {
        let evicted = if self.order.len() == MAX_WATCHED {
            let (old, serial) = self.order.pop_front().unwrap();
            let present = self.ids.get(&old) == Some(&serial);
            if present {
                self.ids.remove(&old);
            }
            Some((old, serial, present))
        } else {
            None
        };
        self.serial = self.serial.wrapping_add(1);
        let previous = self.ids.insert(p, self.serial);
        self.order.push_back((p, self.serial));
        SpendUndo::Record { previous, evicted }
    }
    pub fn forget(&mut self, p: &Outpoint) -> SpendUndo {
        SpendUndo::Forget(*p, self.ids.remove(p))
    }

    pub fn undo(&mut self, undo: SpendUndo) {
        match undo {
            SpendUndo::Record { previous, evicted } => {
                let (p, _) = self
                    .order
                    .pop_back()
                    .expect("undoing an applied consumption");
                restore_entry(&mut self.ids, p, previous);
                self.serial = self.serial.wrapping_sub(1);
                if let Some((old, serial, present)) = evicted {
                    self.order.push_front((old, serial));
                    if present {
                        self.ids.insert(old, serial);
                    }
                }
            }
            SpendUndo::Forget(p, previous) => restore_entry(&mut self.ids, p, previous),
        }
    }
}

pub(super) fn restore_entry<K: std::hash::Hash + Eq, V>(
    map: &mut HashMap<K, V>,
    key: K,
    previous: Option<V>,
) {
    if let Some(value) = previous {
        map.insert(key, value);
    } else {
        map.remove(&key);
    }
}

impl Checks {
    fn merge(&mut self, other: Self) {
        self.noncoinbase_transactions += other.noncoinbase_transactions;
        self.known_inputs += other.known_inputs;
        self.unknown_inputs += other.unknown_inputs;
        self.consumed_inputs += other.consumed_inputs;
        self.same_block_inputs += other.same_block_inputs;
        self.script_checks_passed += other.script_checks_passed;
        self.script_checks_deferred += other.script_checks_deferred;
        self.script_checks_failed += other.script_checks_failed;
        self.amount_checks_passed += other.amount_checks_passed;
        self.amount_checks_deferred += other.amount_checks_deferred;
        self.amount_checks_failed += other.amount_checks_failed;
        self.transactions_determinate += other.transactions_determinate;
        self.transactions_deferred += other.transactions_deferred;
    }
}

pub(super) fn check_script(tx: &Transaction, i: usize, coin: &Coin) -> Result<bool> {
    let input = &tx.inputs[i];
    let mut spend = Spend::new(SpendParams {
        locking_script: LockingScript::from_binary(&coin.script),
        unlocking_script: input
            .unlocking_script
            .clone()
            .context("missing unlocking script")?,
        source_txid: input
            .source_txid
            .clone()
            .context("missing source transaction ID")?,
        source_output_index: input.source_output_index as usize,
        source_satoshis: coin.value,
        transaction_version: tx.version,
        transaction_lock_time: tx.lock_time,
        transaction_sequence: input.sequence,
        other_inputs: tx
            .inputs
            .iter()
            .enumerate()
            .filter(|(j, _)| *j != i)
            .map(|(_, item)| item.clone())
            .collect(),
        other_outputs: tx.outputs.clone(),
        input_index: i,
    });
    Ok(spend.validate()?)
}

fn checked_sum(sum: Option<u64>, value: u64) -> Option<u64> {
    sum?.checked_add(value).filter(|v| *v <= MAX_MONEY)
}

fn supported_signature(unlock: &[u8]) -> bool {
    standard_unlock(unlock)
        && TransactionSignature::from_checksig_format(&unlock[1..=unlock[0] as usize], false)
            .is_ok_and(|s| s.has_low_s())
}

impl Mining {
    fn assess(
        &self,
        raw: &[u8],
        height: u32,
        seen: &mut HashSet<Outpoint>,
        budget: &mut usize,
    ) -> Result<Assessment> {
        let mut tx_inputs = 0;
        let mut tx_outputs = 0;
        let mut total_in = Some(0);
        let mut total_out = Some(0);
        let mut supported = Vec::new();
        let mut a = Assessment::default();
        a.checks.noncoinbase_transactions = 1;
        let mut bytes = raw;
        wire::transaction(&mut bytes, |event| match event {
            Event::Input(id, index, unlock) => {
                let p = (id, index);
                if p == ([0; 32], u32::MAX) {
                    a.violation.get_or_insert("unexpected coinbase prevout");
                }
                if !seen.insert(p) {
                    a.violation
                        .get_or_insert("duplicate input in transaction or block");
                }
                if let Some(coin) = self.coins.get(&p) {
                    a.checks.known_inputs += 1;
                    a.checks.same_block_inputs += u64::from(coin.height == height);
                    total_in = checked_sum(total_in, coin.value);
                    if coin.coinbase && height.saturating_sub(coin.height) < 100 {
                        a.violation.get_or_insert("immature witnessed coinbase");
                    }
                    if raw.len() <= 100_000
                        && tx_inputs < 64
                        && *budget > 0
                        && supported_signature(unlock)
                    {
                        supported.push((tx_inputs, coin));
                    }
                } else if self.spent.has(&p) {
                    a.checks.consumed_inputs += 1;
                    a.violation
                        .get_or_insert("witnessed outpoint already consumed");
                } else {
                    a.checks.unknown_inputs += 1;
                }
                tx_inputs += 1;
            }
            Event::Output(_, value, _) => {
                total_out = checked_sum(total_out, value);
                tx_outputs += 1;
            }
        })?;
        ensure!(bytes.is_empty(), "trailing transaction data");
        if total_out.is_none() || total_in.is_none() {
            a.checks.amount_checks_failed = 1;
            a.violation
                .get_or_insert("transaction value exceeds money range");
        } else if a.checks.unknown_inputs + a.checks.consumed_inputs == 0 {
            a.fee = total_in.zip(total_out).and_then(|(i, o)| i.checked_sub(o));
            if a.fee.is_some() {
                a.checks.amount_checks_passed = 1;
            } else {
                a.checks.amount_checks_failed = 1;
                a.violation.get_or_insert("transaction creates money");
            }
        } else {
            a.checks.amount_checks_deferred = 1;
        }
        // The SDK is used only for bounded, byte-preserving supported transactions.
        // Unknown prevout values are not part of other inputs' FORKID preimages.
        let mut supported_context = false;
        if !supported.is_empty() && tx_inputs <= 64 && tx_outputs <= 128 {
            if let Ok(tx) = Transaction::from_binary(&mut &raw[..]) {
                if tx.to_bytes().is_ok_and(|encoded| encoded == raw) {
                    supported_context = tx.version == 1
                        && tx.lock_time == 0
                        && tx.inputs.iter().all(|input| input.sequence == u32::MAX);
                    for (i, coin) in supported.into_iter().take(*budget) {
                        *budget -= 1;
                        match check_script(&tx, i, coin) {
                            Ok(true) => a.checks.script_checks_passed += 1,
                            Ok(false) => {
                                a.checks.script_checks_failed += 1;
                                a.violation
                                    .get_or_insert("witnessed P2PKH signature rejected");
                            }
                            Err(_) => {
                                // For the supported two-push/P2PKH form OP_EQUALVERIFY
                                // can fail on a key hash mismatch. Other errors may be
                                // interpreter limitations, so classify them as deferred.
                                let key =
                                    tx.inputs[i].unlocking_script.as_ref().unwrap().to_binary();
                                if bsv::primitives::hash::hash160(&key[key.len() - 33..])
                                    != coin.script[3..23]
                                {
                                    a.checks.script_checks_failed += 1;
                                    a.violation
                                        .get_or_insert("witnessed P2PKH key hash mismatch");
                                }
                            }
                        }
                    }
                }
            }
        }
        a.checks.script_checks_deferred =
            a.checks.known_inputs - a.checks.script_checks_passed - a.checks.script_checks_failed;
        if a.violation.is_none() {
            if supported_context && a.fee.is_some() && a.checks.script_checks_deferred == 0 {
                a.checks.transactions_determinate = 1;
            } else {
                a.checks.transactions_deferred = 1;
            }
        }
        Ok(a)
    }

    pub fn check_transaction(&self, raw: &[u8]) -> Result<Value> {
        ensure!(
            raw.len() <= 100_000,
            "transaction exceeds dry-run size limit"
        );
        let Some(height) = self.height.and_then(|tip| tip.checked_add(1)) else {
            return Ok(json!({"outcome":"deferred","reason":"no observed tip"}));
        };
        if self.blocked || self.awaiting_height.is_some() {
            return Ok(json!({"outcome":"deferred","reason":"awaiting complete peer body"}));
        }
        let mut budget = SCRIPT_BUDGET;
        let a = self.assess(raw, height, &mut HashSet::new(), &mut budget)?;
        let mut value = serde_json::to_value(a.checks)?;
        value["outcome"] = json!(if a.violation.is_some() {
            "invalid"
        } else if a.checks.transactions_determinate > 0 {
            "determinate"
        } else {
            "deferred"
        });
        if let Some(reason) = a.violation {
            value["reason"] = json!(reason);
        }
        Ok(value)
    }

    pub fn outpoint(&self, txid: &str, index: u32) -> Result<Value> {
        let mut hash: Hash = hex::decode(txid)?
            .try_into()
            .map_err(|_| anyhow::anyhow!("expected 32-byte txid"))?;
        hash.reverse();
        let p = (hash, index);
        let Some(coin) = self.coins.get(&p) else {
            return Ok(json!({"known":false,"eligible":false,"consumed":self.spent.has(&p)}));
        };
        let eligible = self
            .height
            .is_some_and(|h| h > coin.height && (!coin.coinbase || h - coin.height >= 99))
            && !self.blocked
            && self.awaiting_height.is_none()
            && !self.pending.values().any(|e| e.inputs.contains(&p));
        Ok(
            json!({"known":true,"eligible":eligible,"consumed":false,"height":coin.height,
                  "value_satoshis":coin.value,"coinbase":coin.coinbase}),
        )
    }

    pub(super) fn apply_observed(
        &mut self,
        height: u32,
        hash: Hash,
        txs: Vec<(Hash, &[u8])>,
    ) -> Result<Applied> {
        // Only touched coins enter the undo journal. Tombstones, diagnostics and
        // job state are committed after every transaction and the reward pass.
        let mut undo = Vec::new();
        let mut spent_changes = Vec::new();
        let result = (|| -> Result<(Checks, bool, u64)> {
            let mut checks = Checks::default();
            let mut fees = Some(0u64);
            let mut reward = Some(0u64);
            let mut seen = HashSet::new();
            let mut budget = SCRIPT_BUDGET;
            let mut omitted = 0;
            for (pos, (id, raw)) in txs.into_iter().enumerate() {
                if pos != 0 {
                    let a = self.assess(raw, height, &mut seen, &mut budget)?;
                    if let Some(violation) = a.violation {
                        anyhow::bail!("{violation}");
                    }
                    fees = fees.zip(a.fee).and_then(|(sum, fee)| sum.checked_add(fee));
                    checks.merge(a.checks);
                }
                wire::transaction(&mut &raw[..], |e| match e {
                    Event::Input(id, i, _) => {
                        if let Some(coin) = self.coins.remove(&(id, i)) {
                            undo.push(((id, i), Some(coin)));
                        }
                        if pos != 0 {
                            spent_changes.push(((id, i), true));
                        }
                    }
                    Event::Output(i, value, script) => {
                        if pos == 0 {
                            reward = checked_sum(reward, value);
                        }
                        if self.spent.has(&(id, i)) {
                            spent_changes.push(((id, i), false));
                        }
                        if wire::p2pkh(script) {
                            if seen.contains(&(id, i)) {
                                // A body-order forward reference was already counted as unknown.
                                // Avoid resurrecting its output when the producer appears later.
                                spent_changes.push(((id, i), true));
                            } else if self.coins.len() < MAX_WATCHED {
                                let previous = self.coins.insert(
                                    (id, i),
                                    Coin {
                                        script: script.try_into().unwrap(),
                                        value,
                                        height,
                                        coinbase: pos == 0,
                                    },
                                );
                                undo.push(((id, i), previous));
                            } else {
                                omitted += 1;
                            }
                        }
                    }
                })?;
            }
            let reward = reward.context("coinbase value exceeds money range")?;
            if let Some(fees) = fees {
                ensure!(
                    reward <= subsidy(height, self.regtest).saturating_add(fees),
                    "coinbase exceeds subsidy and known fees"
                );
            }
            Ok((checks, fees.is_some(), omitted))
        })();
        let (checks, reward_checked, omitted) = match result {
            Ok(result) => result,
            Err(error) => {
                for (p, previous) in undo.into_iter().rev() {
                    restore_entry(&mut self.coins, p, previous);
                }
                return Err(error);
            }
        };
        let spends = spent_changes
            .into_iter()
            .map(|(p, consumed)| {
                if consumed {
                    self.spent.record(p)
                } else {
                    self.spent.forget(&p)
                }
            })
            .collect();
        self.omitted_outputs += omitted;
        let previous_row = if self.validation_window.len() == HEADER_WINDOW {
            self.validation_window.pop_front()
        } else {
            None
        };
        self.validation_window.push_back(Observation {
            height,
            hash: wire::display_hash(hash),
            checks,
            coinbase_reward_checked: reward_checked,
        });
        Ok(Applied {
            coins: undo,
            spends,
            previous_row,
            omitted,
        })
    }
}
