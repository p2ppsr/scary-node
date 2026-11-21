//! Local checks against the current contiguous witnessed output set.
use super::*;
use serde::Serialize;

#[derive(Debug)]
pub(super) struct InvalidBody(pub &'static str);
impl std::fmt::Display for InvalidBody {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.0)
    }
}
impl std::error::Error for InvalidBody {}

#[derive(Default, Copy, Clone, Serialize, serde::Deserialize)]
pub(super) struct Checks {
    pub noncoinbase_transactions: u64,
    pub known_inputs: u64,
    pub unknown_inputs: u64,
    pub consumed_inputs: u64,
    pub same_block_inputs: u64,
    pub script_checks_passed: u64,
    pub script_checks_deferred: u64,
    pub script_checks_failed: u64,
    #[serde(default)]
    pub authorized_script_exemptions: u64,
    pub amount_checks_passed: u64,
    pub amount_checks_deferred: u64,
    pub amount_checks_failed: u64,
    pub transactions_determinate: u64,
    pub transactions_deferred: u64,
}

#[derive(Serialize, serde::Deserialize)]
pub(super) struct Observation {
    pub height: u32,
    pub hash: String,
    pub coinbase_reward_checked: bool,
    #[serde(default)]
    pub coinbase_finality_checked: bool,
    #[serde(default)]
    pub applied_directive_sequence: Option<u32>,
    #[serde(default)]
    pub directive_state_ready: Option<bool>,
    #[serde(flatten)]
    pub checks: Checks,
}

#[derive(Default)]
pub(super) struct Assessment {
    confiscation: bool,
    pub checks: Checks,
    pub fee: Option<u64>,
    pub violation: Option<&'static str>,
}

#[derive(Clone, Copy)]
enum InputView<'a> {
    Observed(&'a HashSet<Hash>),
    Candidate(&'a HashMap<Outpoint, Coin>),
    DryRun,
}

#[derive(Clone, Copy)]
pub(super) struct BlockContext {
    height: u32,
    pub(super) time_cutoff: Option<u32>,
    directives_ready: bool,
}

// An entry survives for at most MAX_WATCHED later consumptions. Removing an
// entry before its FIFO turn (a recreated output) must not evict its new entry.
#[derive(Default, Serialize, serde::Deserialize)]
pub(super) struct Spent {
    ids: HashMap<Outpoint, u64>,
    order: VecDeque<(Outpoint, u64)>,
    serial: u64,
}

#[derive(Serialize, serde::Deserialize)]
pub(super) enum SpendUndo {
    Record {
        previous: Option<u64>,
        evicted: Option<(Outpoint, u64, bool)>,
    },
    Forget(Outpoint, Option<u64>),
}

#[derive(Serialize, serde::Deserialize)]
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
        self.authorized_script_exemptions += other.authorized_script_exemptions;
        self.amount_checks_passed += other.amount_checks_passed;
        self.amount_checks_deferred += other.amount_checks_deferred;
        self.amount_checks_failed += other.amount_checks_failed;
        self.transactions_determinate += other.transactions_determinate;
        self.transactions_deferred += other.transactions_deferred;
    }
}

fn checked_sum(sum: Option<u64>, value: u64) -> Option<u64> {
    sum?.checked_add(value).filter(|v| *v <= MAX_MONEY)
}

impl Mining {
    pub(super) fn block_context(&self, height: u32, block_time: u32) -> BlockContext {
        // Chronicle can be activated early on regtest. Its independent CSV
        // activation still selects the absolute locktime clock at height 576.
        let time_cutoff = if self.regtest && height < 576 {
            Some(block_time)
        } else {
            self.parent_median()
        };
        BlockContext {
            height,
            time_cutoff,
            directives_ready: self.directives.ready(),
        }
    }

    fn assess(
        &self,
        raw: &[u8],
        context: BlockContext,
        seen: &mut HashSet<Outpoint>,
        budget: &mut usize,
        view: InputView<'_>,
    ) -> Result<Assessment> {
        let height = context.height;
        let is_confiscation = crate::alerts::confiscation(raw)?.is_some();
        let is_authorized = is_confiscation && self.directives.authorized(&hash256(raw), height);
        let mut tx_inputs = 0;
        let mut total_in = Some(0);
        let mut total_out = Some(0);
        let mut supported = Vec::new();
        let mut a = Assessment {
            confiscation: is_confiscation,
            ..Assessment::default()
        };
        a.checks.noncoinbase_transactions = 1;
        let finality = wire::final_under(raw, height, context.time_cutoff)?;
        if finality == Some(false) {
            a.violation = Some("non-final transaction");
        }
        if is_confiscation && !is_authorized {
            if context.directives_ready {
                a.violation = Some("unauthorized confiscation");
            } else if matches!(view, InputView::Observed(_)) {
                anyhow::bail!("awaiting verified directives for confiscation");
            }
        }
        let mut bytes = raw;
        wire::transaction(&mut bytes, |event| match event {
            Event::Input(id, index, _) => {
                let p = (id, index);
                if p == ([0; 32], u32::MAX) {
                    a.violation.get_or_insert("unexpected coinbase prevout");
                }
                if !seen.insert(p) {
                    a.violation
                        .get_or_insert("duplicate input in transaction or block");
                }
                if !matches!(view, InputView::Observed(_))
                    && context.directives_ready
                    && !is_confiscation
                    && self.directives.frozen(&p, height, false)
                {
                    a.violation.get_or_insert("consensus frozen input");
                }
                let coin = self.coins.get(&p).or_else(|| match view {
                    InputView::Candidate(outputs) => outputs.get(&p),
                    _ => None,
                });
                if let Some(coin) = coin {
                    if coin.confiscation && height.saturating_sub(coin.height) < 1000 {
                        a.violation.get_or_insert("immature confiscation output");
                    }
                    a.checks.known_inputs += 1;
                    a.checks.same_block_inputs += u64::from(coin.height == height);
                    total_in = checked_sum(total_in, coin.value);
                    if coin.coinbase && height.saturating_sub(coin.height) < 100 {
                        a.violation.get_or_insert("immature witnessed coinbase");
                    }
                    if coin.height >= interpreter::activation(self.regtest) {
                        supported.push((tx_inputs, coin));
                    }
                } else if matches!(view, InputView::Observed(ids) if ids.contains(&id)) {
                    a.violation
                        .get_or_insert("same-block output unavailable at spend");
                } else if self.spent.has(&p) {
                    a.checks.consumed_inputs += 1;
                    a.violation
                        .get_or_insert("witnessed outpoint already consumed");
                } else {
                    a.checks.unknown_inputs += 1;
                }
                tx_inputs += 1;
            }
            Event::Output(_, value, script) => {
                if wire::p2sh(script) {
                    a.violation.get_or_insert("post-Genesis P2SH output");
                }
                total_out = checked_sum(total_out, value);
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
        let supported_context = supported.len() == tx_inputs && tx_inputs <= *budget;
        // Try the full reference validator once on complete transactions. On a
        // rejected or partial transaction, evaluate only evidenced inputs for
        // the diagnostic record without inventing values or creation heights.
        let mut full_passed = false;
        if supported_context && !is_confiscation {
            let coins: Vec<_> = supported.iter().map(|(_, coin)| *coin).collect();
            let (ef, heights) = interpreter::extended(raw, &coins)?;
            match interpreter::transaction(self.regtest, &ef, &heights, height, true)? {
                Ok(()) => {
                    full_passed = true;
                    a.checks.script_checks_passed += tx_inputs as u64;
                    *budget -= tx_inputs;
                }
                Err(_) => {
                    a.violation
                        .get_or_insert("BDK rejected witnessed transaction");
                }
            }
        }
        if !full_passed && !is_confiscation {
            for (index, coin) in supported.into_iter().take(*budget) {
                *budget -= 1;
                if interpreter::spend(self.regtest, raw, index, coin, height)? {
                    a.checks.script_checks_passed += 1;
                } else {
                    a.checks.script_checks_failed += 1;
                    a.violation.get_or_insert("witnessed script rejected");
                }
            }
        }
        if is_authorized {
            a.checks.authorized_script_exemptions = a.checks.known_inputs;
        }
        a.checks.script_checks_deferred = a.checks.known_inputs
            - a.checks.script_checks_passed
            - a.checks.script_checks_failed
            - a.checks.authorized_script_exemptions;
        if a.violation.is_none() {
            if supported_context
                && context.directives_ready
                && finality == Some(true)
                && a.fee.is_some()
                && (a.checks.script_checks_deferred == 0 || is_authorized)
            {
                a.checks.transactions_determinate = 1;
            } else {
                a.checks.transactions_deferred = 1;
            }
        }
        Ok(a)
    }

    pub(super) fn assess_candidate(
        &self,
        raw: &[u8],
        context: BlockContext,
        seen: &mut HashSet<Outpoint>,
        created: &HashMap<Outpoint, Coin>,
    ) -> Result<u64> {
        let mut budget = usize::MAX;
        let a = self.assess(
            raw,
            context,
            seen,
            &mut budget,
            InputView::Candidate(created),
        )?;
        if let Some(reason) = a.violation {
            anyhow::bail!("invalid candidate transaction: {reason}");
        }
        ensure!(
            a.checks.transactions_determinate == 1,
            "candidate checks deferred"
        );
        a.fee.context("candidate fee unresolved")
    }

    pub fn check_transaction(&self, raw: &[u8]) -> Result<Value> {
        ensure!(
            raw.len() <= transaction_limit(),
            "transaction exceeds dry-run size limit"
        );
        let Some(height) = self.height.and_then(|tip| tip.checked_add(1)) else {
            return Ok(json!({"outcome":"deferred","reason":"no observed tip"}));
        };
        if self.blocked || self.awaiting_height.is_some() {
            return Ok(json!({"outcome":"deferred","reason":"awaiting complete peer body"}));
        }
        let mut budget = usize::MAX;
        let context = self.block_context(height, now()?.max(self.median_time().saturating_add(1)));
        let a = self.assess(
            raw,
            context,
            &mut HashSet::new(),
            &mut budget,
            InputView::DryRun,
        )?;
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
        let eligible = self.directives.ready()
            && !self
                .directives
                .frozen(&p, self.height.unwrap_or(0).saturating_add(1), true)
            && self.height.is_some_and(|h| {
                h > coin.height
                    && (!coin.coinbase || h - coin.height >= 99)
                    && (!coin.confiscation || h - coin.height >= 999)
            })
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
        context: BlockContext,
        hash: Hash,
        txs: Vec<(Hash, &[u8])>,
    ) -> Result<Applied> {
        // Only touched coins enter the undo journal. Tombstones, diagnostics and
        // job state are committed after every transaction and the reward pass.
        let height = context.height;
        let coinbase_finality = wire::final_under(txs[0].1, height, context.time_cutoff)?;
        if coinbase_finality == Some(false) {
            return Err(InvalidBody("non-final coinbase").into());
        }
        let mut undo = Vec::new();
        let mut spent_changes = Vec::new();
        let result = (|| -> Result<(Checks, bool, u64)> {
            let mut checks = Checks::default();
            let mut fees = Some(0u64);
            let mut reward = Some(0u64);
            let mut seen = HashSet::new();
            let mut budget = usize::MAX;
            let mut omitted = 0;
            let body_ids: HashSet<Hash> = txs.iter().map(|(id, _)| *id).collect();
            for (pos, (id, raw)) in txs.into_iter().enumerate() {
                let is_confiscation = if pos == 0 {
                    false
                } else {
                    let a = self.assess(
                        raw,
                        context,
                        &mut seen,
                        &mut budget,
                        InputView::Observed(&body_ids),
                    )?;
                    if let Some(violation) = a.violation {
                        return Err(InvalidBody(violation).into());
                    }
                    fees = fees.zip(a.fee).and_then(|(sum, fee)| sum.checked_add(fee));
                    checks.merge(a.checks);
                    a.confiscation
                };
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
                        if height >= interpreter::activation(self.regtest)
                            && !script.starts_with(&[0, 0x6a])
                        {
                            if self.coins.len() < capacity().witnessed_outputs {
                                let previous = self.coins.insert(
                                    (id, i),
                                    Coin {
                                        script: Arc::from(script),
                                        value,
                                        height,
                                        coinbase: pos == 0,
                                        confiscation: is_confiscation,
                                    },
                                );
                                undo.push(((id, i), previous));
                            } else {
                                omitted += 1;
                            }
                        }
                    }
                })?;
                ensure!(omitted == 0, "witnessed-output capacity exhausted; increase SCARY_MAX_WITNESSED_OUTPUTS and retry the body");
            }
            let reward = reward.ok_or(InvalidBody("coinbase value exceeds money range"))?;
            if let Some(fees) = fees {
                if reward > subsidy(height, self.regtest).saturating_add(fees) {
                    return Err(InvalidBody("coinbase exceeds subsidy and known fees").into());
                }
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
            coinbase_finality_checked: coinbase_finality.is_some(),
            applied_directive_sequence: Some(self.directives.applied()),
            directive_state_ready: Some(context.directives_ready),
        });
        Ok(Applied {
            coins: undo,
            spends,
            previous_row,
            omitted,
        })
    }
}
