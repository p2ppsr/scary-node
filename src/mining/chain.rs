//! Bounded body buffering and reversible changes to the witnessed branch.
use super::*;
use num_bigint::BigUint;

const REORG_DEPTH: usize = 8;
const REPAIR_BATCH: usize = 24;

#[derive(serde::Serialize, serde::Deserialize)]
pub(super) struct AppliedBlock {
    pub height: u32,
    pub raw: crate::body::BlockBody,
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
    raw: crate::body::BlockBody,
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
    pub fn mapped_bodies(&self) -> (usize, usize) {
        let retained = self
            .applied
            .iter()
            .map(|b| &b.raw)
            .chain(self.buffered.iter().map(|b| &b.raw));
        retained
            .filter(|b| matches!(b, crate::body::BlockBody::File(_)))
            .fold((0, 0), |(count, bytes), b| (count + 1, bytes + b.len()))
    }
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
            || self.applied.iter().map(|b| b.raw.len()).sum::<usize>() > capacity().journal_bytes
            || self
                .applied
                .iter()
                .map(|b| b.applied.coins.len() + b.applied.spends.len())
                .sum::<usize>()
                > capacity().undo_items
        {
            self.applied.pop_front();
        }
    }

    fn buffer(&mut self, body: Body) {
        self.buffered.retain(|b| b.hash != body.hash);
        self.buffered.push_back(body);
        while self.buffered.len() > HEADER_WINDOW
            || self.buffered.iter().map(|b| b.raw.len()).sum::<usize>() > capacity().journal_bytes
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
    fn retained_copy(&self, height: u32, raw: &[u8]) -> bool {
        self.chain
            .applied
            .iter()
            .rev()
            .find(|b| b.height == height)
            .is_some_and(|b| &b.raw[..] == raw)
    }

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
            if height > tip.saturating_add(REPAIR_BATCH as u32)
                || height < tip.saturating_sub(HEADER_WINDOW as u32)
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

    pub(crate) fn repair_locator(&self) -> Vec<Hash> {
        if self.peer_ready()
            && !self.blocked
            && self.awaiting_height.is_none()
            && !self.invalidated_boundary
        {
            return Vec::new();
        }
        // Dense near the tip; spaced further back, retaining the oldest
        // observed header. Stock locators permit at most 101 entries.
        let mut ids: Vec<_> = self
            .headers
            .iter()
            .rev()
            .enumerate()
            .filter(|(i, _)| *i < 32 || *i % 4 == 0 || i + 1 == self.headers.len())
            .map(|(_, h)| hash256(h))
            .filter(|id| {
                !self.directives.invalid.contains(id) && !self.invalid_descendants.contains(id)
            })
            .collect();
        if let Some(eligible) = self.first_invalid_height().and_then(|h| h.checked_sub(1)) {
            if let Some((id, _)) = self
                .active_since_entry
                .iter()
                .find(|(_, h)| **h == eligible)
            {
                if !ids.contains(id) {
                    ids.insert(0, *id);
                }
            }
        }
        ids
    }

    /// Interpret only a contiguous header response rooted in locally
    /// observed ancestry. A remote peer's genesis fallback never starts IBD.
    /// Only complete validated bodies advance the spend-coverage boundary.
    pub(crate) fn repair_headers(&mut self, headers: &[[u8; 80]]) -> Result<Vec<Hash>> {
        let Some(first) = headers.first() else {
            return Ok(vec![]);
        };
        let mut previous: Hash = first[4..36].try_into()?;
        let Some(&start) = self.active_since_entry.get(&previous) else {
            return Ok(vec![]);
        };
        if self.invalid_descendants.contains(&previous)
            || self.directives.invalid.contains(&previous)
        {
            return Ok(vec![]);
        }
        let mut height = start;
        let mut requests = Vec::new();
        for header in headers {
            height = height
                .checked_add(1)
                .context("peer header height overflow")?;
            ensure!(
                header[4..36] == previous,
                "discontinuous peer repair headers"
            );
            self.inspect_header(height, header)?;
            let id = hash256(header);
            if self.directives.invalid.contains(&id) || self.invalid_descendants.contains(&id) {
                return Ok(vec![]);
            }
            if requests.len() < REPAIR_BATCH
                && !self.active_since_entry.contains_key(&id)
                && !self.chain.buffered.iter().any(|b| b.hash == id)
            {
                requests.push(id);
            }
            previous = id;
        }
        let waiting = self.awaiting_height;
        self.announced_height(height);
        if self.awaiting_height != waiting {
            self.save()?;
        }
        Ok(requests)
    }

    /// An alternate transport can fail after the same body has been committed.
    /// Only a hash on the presently witnessed branch proves that failure is redundant.
    pub(crate) fn record_body_gap(&mut self, requested: Option<&str>, reason: String) -> bool {
        if let Some(display) = requested {
            let mut id = [0; 32];
            if hex::decode_to_slice(display, &mut id).is_ok() {
                id.reverse();
                if self.active_since_entry.contains_key(&id) {
                    return false;
                }
            }
        }
        self.wait_for_body(reason);
        true
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
                ensure!(
                    self.directives.ready(),
                    "awaiting current directives for a frozen input"
                );
                return Ok(true);
            }
        }
        Ok(false)
    }

    // An explicit soft freeze is an attribute of the block index. Changing
    // the blacklist does not clear it. The default holds three successors.
    fn held_by_soft_freeze(&self, hash: Hash, mut parent: Hash) -> bool {
        if self.soft_freezes.is_empty() {
            return false;
        }
        if self.soft_freezes.contains(&hash) {
            return true;
        }
        for _ in 0..3 {
            if self.soft_freezes.contains(&parent) {
                return true;
            }
            if let Some(b) = self.chain.buffered.iter().find(|b| b.hash == parent) {
                parent = b.previous();
            } else if let Some(h) = self
                .headers
                .iter()
                .rev()
                .find(|h| hash256(&h[..]) == parent)
            {
                parent = h[4..36].try_into().expect("header prev hash");
            } else {
                break;
            }
        }
        false
    }

    #[cfg(test)]
    pub fn observe(&mut self, height: u32, raw: &[u8]) -> Result<()> {
        self.observe_body(
            height,
            crate::body::BlockBody::Memory(Arc::new(raw.to_vec())),
        )
        .map(|_| ())
    }
    /// Returns false when the input left the durable observation state unchanged.
    pub fn observe_body(&mut self, height: u32, raw: crate::body::BlockBody) -> Result<bool> {
        if height < interpreter::activation(self.regtest) {
            return Ok(false);
        }
        let header = self.inspect_header(height, &raw)?;
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
            !self.headers.is_empty()
                || (!self.invalidated_boundary && self.invalid_descendants.is_empty()),
            "no eligible ancestry retained after signed invalidation"
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
            if self.retained_copy(height, &raw) {
                return Ok(false);
            }
            let (_, error) = self.inspect_body(height, &header, &raw)?;
            ensure!(
                error.is_none(),
                "active header received with different coinbase framing"
            );
            return Ok(false);
        }
        let old_tip = self.tip();
        if let Some(tip) = self.height {
            // An older body outside the window proves nothing about the
            // current branch. Ignore it without resetting usable work.
            if height < tip.saturating_sub(HEADER_WINDOW as u32)
                && !(self.invalidated_boundary
                    && self.first_invalid_height().is_some_and(|invalid| {
                        height >= invalid.saturating_sub(HEADER_WINDOW as u32)
                    }))
            {
                return Ok(false);
            }
        }
        let direct = !self.invalidated_boundary && (old_tip.is_none() || old_tip == Some(previous));
        let existing_hold = self.held_by_soft_freeze(hash, previous);
        let newly_frozen = direct
            && !existing_hold
            && self
                .soft_frozen(&raw, height)
                .inspect_err(|error| self.wait_for_body(error.to_string()))?;
        let quarantined = newly_frozen || existing_hold;
        if direct && !quarantined {
            if let Err(error) = self.connect(height, raw, header) {
                self.wait_for_body(error.to_string());
                return Err(error);
            }
        } else {
            let (_, coinbase_error) = self.inspect_body(height, &header, &raw)?;
            ensure!(
                coinbase_error.is_none(),
                "invalid competing coinbase framing"
            );
            if newly_frozen {
                self.soft_freezes.insert(hash);
            }
            let candidate = Body {
                height,
                header,
                hash,
                raw,
            };
            self.chain.buffer(candidate);
            if quarantined && self.awaiting_height.is_some_and(|h| h <= height) {
                self.awaiting_height = None;
                self.revision += 1;
            }
            if !quarantined && self.height.is_some_and(|tip| height > tip) {
                self.announced_height(height);
            }
        }

        // A late parent can make a previously buffered child attachable.
        // A competing branch beats the incumbent only with greater work.
        let result = self.activate_buffered_branches();
        if self.held_by_soft_freeze(hash, previous)
            && self.awaiting_height.is_some_and(|h| h <= height)
        {
            self.awaiting_height = None;
            self.revision += 1;
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
            self.reconsider_waiting();
            self.lifecycle.observe(&self.headers, height);
        }
        self.chain.prune();
        self.chain
            .buffered
            .retain(|b| !self.headers.iter().any(|h| h == &b.header));
        result.map(|_| true)
    }

    pub(crate) fn linked_height(&self, hash: Hash, parent: Hash) -> Option<u32> {
        self.active_since_entry
            .get(&hash)
            .copied()
            .or_else(|| self.active_since_entry.get(&parent)?.checked_add(1))
            .or_else(|| {
                self.chain.buffered.iter().find_map(|b| {
                    if b.hash == hash {
                        Some(b.height)
                    } else if b.hash == parent {
                        b.height.checked_add(1)
                    } else if b.previous() == hash {
                        b.height.checked_sub(1)
                    } else {
                        None
                    }
                })
            })
    }

    fn active_height(&self, hash: Hash) -> Option<u32> {
        let tip = self.height?;
        let offset = self.headers.iter().rev().position(|h| hash256(h) == hash)?;
        tip.checked_sub(offset as u32)
    }

    fn adopt_new_boundary(&mut self, ancestor: u32, path: Vec<Body>) -> Result<()> {
        let mut trial = Mining::new(self.regtest, self.payout.clone())?;
        trial.directives = self.directives.clone();
        let height = path.last().context("empty replacement branch")?.height;
        let length = path.len();
        if let Some(tip) = self.height {
            let anchor = ancestor;
            let depth = tip
                .checked_sub(anchor)
                .context("replacement ancestor above tip")? as usize;
            if depth < self.headers.len() {
                trial
                    .headers
                    .extend(self.headers.iter().take(self.headers.len() - depth));
                trial.height = Some(anchor);
            }
        }
        for b in path {
            if let Err(error) = trial.connect(b.height, b.raw, b.header) {
                self.chain.buffered.retain(|other| other.hash != b.hash);
                if error.downcast_ref::<validation::InvalidBody>().is_some() {
                    self.remember_invalid(b.hash, b.height, error.to_string());
                }
                return Err(error);
            }
            // Only the recent journal is needed after a successful atomic
            // switch. Older trial undo is unnecessary even for a long fork.
            trial.chain.prune();
        }
        // The shared ancestor supplies contextual header checks during replay.
        // Spend coverage and undo start at the first newly verified body.
        while trial.headers.len() > length {
            trial.headers.pop_front();
        }
        for entry in &mut trial.chain.applied {
            entry.previous_header = None;
        }
        trial.chain.prune();
        // Also migrate a boundary quarantined by versions that retained only
        // the signed root and the active-height index in their checkpoint.
        if self.invalidated_boundary {
            if let Some(invalid) = self.first_invalid_height() {
                self.mark_invalid_descendants(invalid);
            }
        }
        let known_prefix = std::mem::take(&mut self.active_since_entry);
        let mut buffered = std::mem::take(&mut self.chain.buffered);
        buffered.retain(|b| !trial.active_since_entry.contains_key(&b.hash));
        let reorgs = self.chain.reorgs + 1;
        // An ordinary partial repair cannot clear a farther announced height.
        // A signed invalidation disqualifies the old suffix and its reports.
        let awaiting = self.awaiting_height.filter(|_| !self.invalidated_boundary);
        self.suspend();
        self.awaiting_height = awaiting.filter(|h| *h > height);
        self.height = trial.height;
        self.headers = trial.headers;
        self.active_since_entry = trial.active_since_entry;
        self.active_since_entry
            .extend(known_prefix.into_iter().filter(|(_, n)| *n <= ancestor));
        self.chain = trial.chain;
        self.chain.buffered = buffered;
        self.chain.reorgs = reorgs;
        self.coins = trial.coins;
        self.spent = trial.spent;
        self.omitted_outputs += trial.omitted_outputs;
        self.validation_window = trial.validation_window;
        self.invalidated_boundary = false;
        self.suspended_reason = None;
        self.lifecycle.observe(&self.headers, height);
        Ok(())
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
        let invalid_height = self
            .invalidated_boundary
            .then(|| self.first_invalid_height())
            .flatten();
        if self.invalidated_boundary && invalid_height.is_none() {
            return Ok(None);
        }
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
            let Some(&ancestor) = active
                .get(&parent)
                .or_else(|| invalid_height.and_then(|_| self.active_since_entry.get(&parent)))
            else {
                continue;
            };
            let depth = (height - ancestor) as usize;
            if ancestor.checked_add(1) != Some(path.last().unwrap().height)
                || invalid_height.is_some_and(|invalid| ancestor >= invalid)
                || ((self.invalidated_boundary || depth > self.chain.applied.len())
                    && path.len() < 2)
            {
                continue;
            }
            if self.held_by_soft_freeze(tip.hash, tip.previous())
                || path.iter().any(|b| {
                    self.directives.invalid.contains(&b.hash)
                        || self.invalid_descendants.contains(&b.hash)
                })
            {
                continue;
            }
            let candidate = path.iter().try_fold(BigUint::default(), |sum, b| {
                Ok::<_, anyhow::Error>(sum + work(&b.header)?)
            })?;
            // Compare each fork against exactly the same eligible incumbent.
            // When signed invalidation disqualifies the tail, its parent still
            // has accumulated work. A fork from *earlier* must overtake it.
            let eligible = invalid_height.map_or(height, |bad| bad.saturating_sub(1));
            let skip = (height - eligible) as usize;
            let compare = (eligible - ancestor) as usize;
            if compare > 0 && skip + compare > self.headers.len() {
                continue; // The common-prefix work difference cannot be reconstructed.
            }
            let incumbent = self
                .headers
                .iter()
                .rev()
                .skip(skip)
                .take(compare)
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

    fn rollback(&mut self, mut block: AppliedBlock) -> (u32, crate::body::BlockBody) {
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
                    let header = self.inspect_header(height, &raw)?;
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
                let header = self.inspect_header(height, &raw)?;
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
    fn first_invalid_height(&self) -> Option<u32> {
        self.directives
            .invalid
            .iter()
            .filter_map(|id| self.active_since_entry.get(id))
            .copied()
            .min()
    }

    fn mark_invalid_descendants(&mut self, height: u32) {
        self.invalid_descendants.extend(
            self.active_since_entry
                .iter()
                .filter_map(|(hash, h)| (*h >= height).then_some(*hash)),
        );
    }

    pub(super) fn apply_invalidations(&mut self) {
        let Some(height) = self.first_invalid_height() else {
            return;
        };
        self.mark_invalid_descendants(height);
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
    fn activate_buffered_branches(&mut self) -> Result<()> {
        while let Some((ancestor, path)) = self.best_buffered_branch()? {
            // SV records a new freeze while connecting a competitive branch.
            // A losing side branch may receive a different directive before
            // it ever earns enough work for the node to try connecting it.
            let mut held = false;
            for b in &path {
                if !self.soft_freezes.contains(&b.hash) && self.soft_frozen(&b.raw, b.height)? {
                    self.soft_freezes.insert(b.hash);
                    held = true;
                    break;
                }
            }
            if !held {
                let depth = self.height.unwrap() - ancestor;
                if self.invalidated_boundary || depth as usize > self.chain.applied.len() {
                    self.adopt_new_boundary(ancestor, path)?;
                } else {
                    self.switch_branch(ancestor, path)?;
                }
            }
        }
        Ok(())
    }

    pub(crate) fn directives_changed(&mut self, raw: &[u8]) -> Result<()> {
        let kind = u32::from_le_bytes(raw[16..20].try_into()?);
        let previous = self.tip();
        if kind == 7 {
            self.apply_invalidations();
        }
        if matches!(kind, 2..=4 | 7) {
            // The directive also governs future branches. If a buffered peer
            // body cannot connect, fence its coverage independently so that it
            // cannot prevent the signed sequence from advancing.
            if let Err(error) = self.activate_buffered_branches() {
                self.wait_for_body(format!("branch selection after directive: {error}"));
            }
            if self.tip() != previous {
                self.parent_epoch += 1;
                self.solved_on_tip = false;
                if let Some(h) = self.height {
                    self.lifecycle.observe(&self.headers, h);
                }
            }
            self.recheck_pending();
            self.revision += 1;
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
