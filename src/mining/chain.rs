//! Bounded body buffering and reversible changes to the witnessed branch.
use super::*;
use num_bigint::BigUint;

const REORG_DEPTH: usize = 8;
const BUFFERED_BLOCKS: usize = 24;
const HISTORY_BYTES: usize = 512 << 20;
const UNDO_ITEMS: usize = 2_000_000;

#[derive(serde::Serialize, serde::Deserialize)]
pub(super) struct AppliedBlock {
    pub height: u32,
    #[serde(with = "store::arc_bytes")]
    pub raw: Arc<[u8]>,
    pub applied: validation::Applied,
    #[serde(with = "store::optional_header")]
    pub previous_header: Option<[u8; 80]>,
}

#[derive(Clone, serde::Serialize, serde::Deserialize)]
struct Body {
    height: u32,
    hash: Hash,
    #[serde(with = "serde_bytes")]
    header: [u8; 80],
    #[serde(with = "store::arc_bytes")]
    raw: Arc<[u8]>,
}

impl Body {
    fn previous(&self) -> Hash {
        self.header[4..36].try_into().unwrap()
    }
}

#[derive(Default, serde::Serialize, serde::Deserialize)]
pub(super) struct RecentChain {
    pub applied: VecDeque<AppliedBlock>,
    buffered: VecDeque<Body>,
    pub reorgs: u64,
}

impl RecentChain {
    pub fn buffered_len(&self) -> usize {
        self.buffered.len()
    }

    pub fn record(&mut self, block: AppliedBlock) {
        self.applied.push_back(block);
        // Delay eviction until a branch switch commits. A failed trial must be
        // able to undo each tentative block before replaying the original side.
    }

    fn prune(&mut self) {
        while self.applied.len() > REORG_DEPTH
            || self.applied.iter().map(|b| b.raw.len()).sum::<usize>() > HISTORY_BYTES
            || self
                .applied
                .iter()
                .map(|b| b.applied.coins.len() + b.applied.spends.len())
                .sum::<usize>()
                > UNDO_ITEMS
        {
            self.applied.pop_front();
        }
    }

    fn buffer(&mut self, body: Body) {
        self.buffered.retain(|b| b.hash != body.hash);
        self.buffered.push_back(body);
        while self.buffered.len() > BUFFERED_BLOCKS
            || self.buffered.iter().map(|b| b.raw.len()).sum::<usize>() > HISTORY_BYTES
        {
            self.buffered.pop_front();
        }
    }
}

pub(super) fn work(header: &[u8; 80]) -> Result<BigUint> {
    let target = BigUint::from_bytes_le(&wire::as_le_target(bits(header))?);
    Ok((BigUint::from(1u8) << 256) / (target + 1u8))
}

impl Mining {
    pub fn tip(&self) -> Option<Hash> {
        self.headers.back().map(|h| hash256(h))
    }

    pub(crate) fn missing_recent(&self) -> Vec<(Hash, u32)> {
        let Some(tip) = self.height else {
            return vec![];
        };
        let mut missing = HashMap::new();
        for b in &self.chain.buffered {
            let Some(height) = b.height.checked_sub(1) else {
                continue;
            };
            if height > tip.saturating_add(BUFFERED_BLOCKS as u32)
                || height < tip.saturating_sub(self.chain.applied.len() as u32)
            {
                continue;
            }
            let id = b.previous();
            if !self.active_since_entry.contains_key(&id)
                && !self.chain.buffered.iter().any(|body| body.hash == id)
                && !self.invalid_descendants.contains(&id)
                && !self.directives.invalid.contains(&id)
            {
                missing.insert(id, height);
            }
        }
        missing.into_iter().collect()
    }

    pub fn wait_for_body(&mut self, reason: String) {
        if !self.blocked {
            self.revision += 1;
        }
        self.blocked = true;
        self.suspended_reason = Some(reason);
    }

    // SV Node's default quarantine lasts through the third successor.
    // Other contextual checks are still performed before adopting the branch.
    fn soft_frozen(&self, raw: &[u8], height: u32) -> Result<bool> {
        if !self.directives.has_freezes() {
            return Ok(false);
        }
        let mut body = raw.get(80..).context("short block")?;
        let n = wire::varint(&mut body)?;
        for index in 0..n {
            let mut frozen = false;
            let tx = wire::transaction(&mut body, |event| {
                if let Event::Input(id, n, _) = event {
                    frozen |= self.directives.frozen(&(id, n), height, false);
                }
            })?;
            if index != 0 && frozen && crate::alerts::confiscation(tx)?.is_none() {
                return Ok(true);
            }
        }
        Ok(false)
    }

    pub fn observe(&mut self, height: u32, raw: &[u8]) -> Result<()> {
        if height < interpreter::activation(self.regtest) {
            return Ok(());
        }
        let header = self.inspect_header(raw)?;
        let hash = hash256(&header);
        let previous: Hash = header[4..36].try_into().unwrap();
        if self.directives.invalid.contains(&hash)
            || self.directives.invalid.contains(&previous)
            || self.invalid_descendants.contains(&hash)
            || self.invalid_descendants.contains(&previous)
        {
            self.invalid_descendants.insert(hash);
            anyhow::bail!("explicitly invalidated ancestry");
        }
        ensure!(
            !self.invalidated_boundary,
            "explicit invalidation exceeds reversible ancestry"
        );
        if let Some((id, _, reason)) = self
            .recent_invalid
            .iter()
            .find(|(id, _, _)| *id == hash || *id == previous)
        {
            let (id, reason) = (*id, reason.clone());
            if id == previous {
                self.remember_invalid(hash, height, reason.clone());
            }
            anyhow::bail!("known rejected block or ancestor: {reason}");
        }
        if self.active_height(hash) == Some(height) {
            return Ok(());
        }
        let old_tip = self.tip();
        if let Some(tip) = self.height {
            // An older body outside the window proves nothing about the
            // current branch. Ignore it without resetting usable work.
            if height < tip.saturating_sub(REORG_DEPTH as u32) {
                return Ok(());
            }
        }
        let direct = old_tip.is_none() || old_tip == Some(previous);
        let quarantined = self.soft_frozen(raw, height)?;
        if direct && !quarantined {
            if let Err(error) = self.connect(height, Arc::from(raw), header) {
                self.wait_for_body(error.to_string());
                return Err(error);
            }
        } else {
            let (_, coinbase_error) = self.inspect_body(height, &header, raw)?;
            ensure!(
                coinbase_error.is_none(),
                "invalid competing coinbase framing"
            );
            let deep_gap = self
                .height
                .is_some_and(|tip| height > tip.saturating_add(BUFFERED_BLOCKS as u32));
            let candidate = Body {
                height,
                header,
                hash,
                raw: Arc::from(raw),
            };
            if deep_gap && !quarantined {
                let anchor = self
                    .chain
                    .buffered
                    .iter()
                    .find(|p| p.hash == previous && p.height.checked_add(1) == Some(height))
                    .cloned();
                if let Some(p) = anchor {
                    ensure!(
                        !self.soft_frozen(&p.raw, p.height)?
                            && !self.directives.invalid.contains(&p.hash)
                            && !self.invalid_descendants.contains(&p.hash),
                        "ineligible re-entry boundary"
                    );
                    // Validate the replacement boundary in isolation. Failure leaves
                    // the previous snapshot and its readiness gate intact.
                    let mut trial = Mining::new(self.regtest, self.payout.clone())?;
                    trial.directives = self.directives.clone();
                    trial.connect(p.height, p.raw, p.header)?;
                    trial.connect(height, candidate.raw, header)?;
                    self.suspend();
                    self.height = trial.height;
                    self.headers = trial.headers;
                    self.active_since_entry = trial.active_since_entry;
                    self.chain = trial.chain;
                    self.coins = trial.coins;
                    self.spent = trial.spent;
                    self.omitted_outputs += trial.omitted_outputs;
                    self.validation_window = trial.validation_window;
                    self.suspended_reason = None;
                    self.revision += 1;
                    self.lifecycle.observe(&self.headers, height);
                    return Ok(());
                }
            }
            let mut held = quarantined;
            let mut predecessor = previous;
            for _ in 0..3 {
                let Some(body) = self.chain.buffered.iter().find(|b| b.hash == predecessor) else {
                    break;
                };
                held |= self.soft_frozen(&body.raw, body.height)?;
                predecessor = body.previous();
            }
            self.chain.buffer(candidate);
            if held && self.awaiting_height.is_some_and(|h| h <= height) {
                self.awaiting_height = None;
                self.revision += 1;
            }
            if !held && self.height.is_some_and(|tip| height > tip) {
                self.announced_height(height);
            }
        }

        // A late parent can make a previously buffered child attachable.
        // A competing branch beats the incumbent only with greater work.
        let mut result = Ok(());
        while let Some((ancestor, branch)) = self.best_buffered_branch()? {
            if let Err(error) = self.switch_branch(ancestor, branch) {
                result = Err(error);
                break;
            }
        }
        if self.tip() != old_tip {
            let height = self.height.expect("changed tip has a height");
            self.awaiting_height = self.awaiting_height.filter(|waiting| *waiting > height);
            if result.is_ok() {
                self.blocked = false;
                self.suspended_reason = None;
            }
            self.solved_on_tip = false;
            self.parent_epoch += 1;
            self.revision += 1;
            self.recheck_pending();
            self.lifecycle.observe(&self.headers, height);
        }
        self.chain.prune();
        self.chain
            .buffered
            .retain(|b| !self.headers.iter().any(|h| h == &b.header));
        result
    }

    fn active_height(&self, hash: Hash) -> Option<u32> {
        let tip = self.height?;
        let offset = self.headers.iter().rev().position(|h| hash256(h) == hash)?;
        tip.checked_sub(offset as u32)
    }

    fn best_buffered_branch(&self) -> Result<Option<(u32, Vec<Body>)>> {
        let Some(height) = self.height else {
            return Ok(None);
        };
        if self.chain.buffered.is_empty() {
            return Ok(None);
        }
        let active: HashMap<_, _> = self
            .headers
            .iter()
            .rev()
            .enumerate()
            .filter_map(|(offset, h)| height.checked_sub(offset as u32).map(|n| (hash256(h), n)))
            .collect();
        let bodies: HashMap<_, _> = self.chain.buffered.iter().map(|b| (b.hash, b)).collect();
        let mut best = None;
        let mut best_gain = BigUint::default();
        for tip in &self.chain.buffered {
            let mut path = vec![tip];
            let mut parent = tip.previous();
            while let Some(b) = bodies.get(&parent) {
                if b.height.checked_add(1) != Some(path.last().unwrap().height) {
                    break;
                }
                path.push(b);
                parent = b.previous();
            }
            let Some(&ancestor) = active.get(&parent) else {
                continue;
            };
            let depth = (height - ancestor) as usize;
            if depth > self.chain.applied.len()
                || ancestor.checked_add(1) != Some(path.last().unwrap().height)
            {
                continue;
            }
            if path
                .iter()
                .take(4)
                .any(|b| self.soft_frozen(&b.raw, b.height).unwrap_or(true))
                || path
                    .iter()
                    .any(|b| self.directives.invalid.contains(&b.hash))
            {
                continue;
            }
            let candidate = path.iter().try_fold(BigUint::default(), |sum, b| {
                Ok::<_, anyhow::Error>(sum + work(&b.header)?)
            })?;
            let incumbent = self
                .headers
                .iter()
                .rev()
                .take(depth)
                .try_fold(BigUint::default(), |sum, h| {
                    Ok::<_, anyhow::Error>(sum + work(h)?)
                })?;
            if candidate > incumbent && &candidate - &incumbent > best_gain {
                best_gain = candidate - incumbent;
                best = Some((ancestor, path.into_iter().rev().cloned().collect()));
            }
        }
        Ok(best)
    }

    fn detach(&mut self) -> AppliedBlock {
        let block = self
            .chain
            .applied
            .pop_back()
            .expect("rollback bounded by the journal");
        for (p, undo) in block.applied.coins.iter().rev() {
            validation::restore_entry(&mut self.coins, *p, undo.clone());
        }
        self.validation_window.pop_back();
        if let Some(h) = self.headers.pop_back() {
            self.active_since_entry.remove(&hash256(&h));
        }
        self.omitted_outputs -= block.applied.omitted;
        self.height = block.height.checked_sub(1);
        block
    }

    fn rollback(&mut self, mut block: AppliedBlock) -> (u32, Arc<[u8]>) {
        for undo in block.applied.spends.into_iter().rev() {
            self.spent.undo(undo);
        }
        if let Some(header) = block.previous_header.take() {
            self.headers.push_front(header);
        }
        if let Some(row) = block.applied.previous_row.take() {
            self.validation_window.push_front(row);
        }
        (block.height, block.raw)
    }

    fn switch_branch(&mut self, ancestor: u32, branch: Vec<Body>) -> Result<()> {
        let mut old = Vec::new();
        while self.height.is_some_and(|h| h > ancestor) {
            let block = self.detach();
            old.push(self.rollback(block));
        }
        for body in &branch {
            self.chain.buffered.retain(|b| b.hash != body.hash);
            if let Err(error) = self.connect(body.height, body.raw.clone(), body.header) {
                while self.height.is_some_and(|h| h > ancestor) {
                    let block = self.detach();
                    self.rollback(block);
                }
                for (height, raw) in old.into_iter().rev() {
                    let header = self.inspect_header(&raw)?;
                    if let Err(error) = self.connect(height, raw, header) {
                        self.suspend();
                        return Err(error.context("could not restore verified branch"));
                    }
                }
                // A good earlier body removed from the queue is still a valid
                // candidate when an honest alternate suffix arrives.
                for good in branch.iter().take_while(|b| b.hash != body.hash) {
                    self.chain.buffer(good.clone());
                }
                self.wait_for_body(error.to_string());
                return Err(error);
            }
        }
        if !old.is_empty() {
            self.chain.reorgs += 1;
            for (height, raw) in old.into_iter().rev() {
                let header = self.inspect_header(&raw)?;
                self.chain.buffer(Body {
                    height,
                    hash: hash256(&header),
                    header,
                    raw,
                });
            }
        }
        Ok(())
    }
}

impl Mining {
    pub(super) fn apply_invalidations(&mut self) {
        let invalid = self
            .directives
            .invalid
            .iter()
            .filter_map(|id| self.active_since_entry.get(id))
            .copied()
            .min();
        let Some(height) = invalid else {
            return;
        };
        let depth = self
            .height
            .map(|tip| tip.saturating_sub(height) as usize + 1)
            .unwrap_or(0);
        if depth >= self.headers.len() || depth > self.chain.applied.len() {
            self.invalidated_boundary = true;
            self.wait_for_body("directive invalidated ancestry beyond undo coverage".into());
        } else {
            for _ in 0..depth {
                if let Some(h) = self.tip() {
                    self.invalid_descendants.insert(h);
                }
                let block = self.detach();
                self.rollback(block);
            }
            self.awaiting_height = None;
            self.blocked = false;
            self.solved_on_tip = false;
            self.suspended_reason = None;
            self.recheck_pending();
            self.lifecycle.observe(
                &self.headers,
                self.height.expect("remaining eligible ancestor"),
            );
        }
        self.parent_epoch += 1;
        self.revision += 1;
    }
    fn reevaluate_soft_freezes(&mut self) -> Result<()> {
        let mut first = None;
        for b in self.chain.applied.iter().rev().take(4) {
            if self.soft_frozen(&b.raw, b.height)? {
                first = Some(b.height);
            }
        }
        if let Some(frozen_height) = first {
            if self
                .height
                .is_none_or(|h| h - frozen_height + 1 >= self.headers.len() as u32)
            {
                self.wait_for_body("soft freeze at observation boundary".into());
                return Ok(());
            }
            while self.height.is_some_and(|tip| tip >= frozen_height) {
                let removed = self.detach();
                let (height, raw) = self.rollback(removed);
                let header: [u8; 80] = raw[..80].try_into()?;
                self.chain.buffer(Body {
                    height,
                    hash: hash256(&header),
                    header,
                    raw,
                });
            }
            self.awaiting_height = None;
            self.solved_on_tip = false;
            self.parent_epoch += 1;
        }
        let previous = self.tip();
        while let Some((ancestor, branch)) = self.best_buffered_branch()? {
            self.switch_branch(ancestor, branch)?;
        }
        if previous != self.tip() {
            self.parent_epoch += 1;
            self.solved_on_tip = false;
        }
        if let Some(h) = self.height {
            self.lifecycle.observe(&self.headers, h);
        }
        Ok(())
    }

    pub(crate) fn directives_changed(&mut self, raw: &[u8]) -> Result<()> {
        let kind = u32::from_le_bytes(raw[16..20].try_into()?);
        if matches!(kind, 2..=4) {
            self.reevaluate_soft_freezes()?;
            self.recheck_pending();
            self.revision += 1;
        }
        if kind == 7 {
            self.apply_invalidations();
        }
        if self.solved_on_tip {
            if let Some(b) = &self.solution_body {
                let invalid = self.directives.invalid.contains(&b.hash)
                    || self.tip().is_none_or(|h| b.raw[4..36] != h)
                    || self
                        .inspect_body(b.height, &b.raw[..80].try_into()?, &b.raw)?
                        .0
                        .iter()
                        .skip(1)
                        .any(|(id, _)| !self.pending.contains_key(id));
                if invalid {
                    self.lifecycle
                        .retire(&wire::display_hash(b.hash), self.height);
                    self.solved_on_tip = false;
                    self.revision += 1;
                }
            }
        }
        Ok(())
    }
}
