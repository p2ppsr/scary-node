//! Bounded body buffering and reversible changes to the witnessed branch.
use super::*;
use num_bigint::BigUint;

const REORG_DEPTH: usize = 8;
const BUFFERED_BLOCKS: usize = 24;
const HISTORY_BYTES: usize = 64 << 20;
const UNDO_ITEMS: usize = 250_000;

pub(super) struct AppliedBlock {
    pub height: u32,
    pub raw: Arc<[u8]>,
    pub applied: validation::Applied,
    pub previous_header: Option<[u8; 80]>,
}

#[derive(Clone)]
struct Body {
    height: u32,
    hash: Hash,
    header: [u8; 80],
    raw: Arc<[u8]>,
}

impl Body {
    fn previous(&self) -> Hash {
        self.header[4..36].try_into().unwrap()
    }
}

#[derive(Default)]
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

fn work(header: &[u8; 80]) -> Result<BigUint> {
    let target = BigUint::from_bytes_le(&wire::as_le_target(bits(header))?);
    Ok((BigUint::from(1u8) << 256) / (target + 1u8))
}

impl Mining {
    pub fn tip(&self) -> Option<Hash> {
        self.headers.back().map(|h| hash256(h))
    }

    pub fn wait_for_body(&mut self, reason: String) {
        if !self.blocked {
            self.revision += 1;
        }
        self.blocked = true;
        self.suspended_reason = Some(reason);
    }

    pub fn observe(&mut self, height: u32, raw: &[u8]) -> Result<()> {
        let header = self.inspect_header(raw)?;
        let hash = hash256(&header);
        let previous: Hash = header[4..36].try_into().unwrap();
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
        if direct {
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
            let near = self
                .height
                .is_some_and(|h| height <= h.saturating_add(BUFFERED_BLOCKS as u32));
            if near {
                self.chain.buffer(Body {
                    height,
                    header,
                    hash,
                    raw: Arc::from(raw),
                });
            }
            if self.height.is_some_and(|tip| height > tip) {
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
            self.pending.retain(|_, entry| {
                entry.inputs.iter().all(|p| {
                    self.coins.get(p).is_some_and(|coin| {
                        height > coin.height && (!coin.coinbase || height - coin.height >= 99)
                    })
                })
            });
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
        self.headers.pop_back();
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
