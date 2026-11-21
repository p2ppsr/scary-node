//! Reconsider a native payment when its witnessed funding block gains a successor.
use super::*;

#[derive(Debug)]
pub(super) struct AwaitingSuccessor(pub Hash);

impl std::fmt::Display for AwaitingSuccessor {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("creation block has no observed successor")
    }
}
impl std::error::Error for AwaitingSuccessor {}

#[derive(Default)]
pub(super) struct WaitingOffers {
    ids: HashSet<Hash>,
    arrivals: Vec<(TxSource, Arc<[u8]>)>,
    bytes: usize,
}

impl WaitingOffers {
    pub(super) fn len(&self) -> usize {
        self.arrivals.len()
    }
    pub(super) fn bytes(&self) -> usize {
        self.bytes
    }

    fn retain(&mut self, id: Hash, source: TxSource, raw: &[u8]) -> bool {
        if self.len() >= capacity().pending_transactions
            || raw.len() > capacity().pending_bytes.saturating_sub(self.bytes)
            || !self.ids.insert(id)
        {
            return false;
        }
        self.arrivals.push((source, Arc::from(raw)));
        self.bytes += raw.len();
        true
    }
}

impl Mining {
    pub(crate) fn knows_offer(&self, id: &Hash) -> bool {
        self.pending.contains_key(id) || self.waiting.ids.contains(id)
    }

    fn intake(&mut self, source: TxSource) -> &mut IntakeCounts {
        match source {
            TxSource::Sv => &mut self.sv_intake,
            TxSource::Subtree => &mut self.subtree_intake,
        }
    }

    pub(crate) fn peer_tx(&mut self, source: TxSource, raw: &[u8]) {
        if raw.len() > transaction_limit() {
            self.intake(source).ineligible += 1;
            return;
        }
        if !self.waiting.ids.is_empty() && self.waiting.ids.contains(&hash256(raw)) {
            self.intake(source).duplicate += 1;
            return;
        }
        let count = self.pending.len();
        match self.submit_tx(raw) {
            Ok(_) if self.pending.len() > count => self.intake(source).admitted += 1,
            Ok(_) => self.intake(source).duplicate += 1,
            Err(error) => {
                if let Some(AwaitingSuccessor(id)) = error.downcast_ref() {
                    if self.waiting.retain(*id, source, raw) {
                        self.intake(source).awaiting_successor += 1;
                        return;
                    }
                }
                self.intake(source).ineligible += 1;
            }
        }
    }

    pub(crate) fn reconsider_waiting(&mut self) {
        if self.waiting.len() == 0
            || self.storage_failure.is_some()
            || self.blocked
            || self.awaiting_height.is_some()
            || !self.directives.ready()
        {
            return;
        }
        // First eligible arrival wins conflicting spends. Every offer goes
        // through admission again on the new branch and current directive state.
        for (source, raw) in std::mem::take(&mut self.waiting).arrivals {
            let count = self.pending.len();
            match self.submit_tx(&raw) {
                Ok(_) if self.pending.len() > count => {
                    let stats = self.intake(source);
                    stats.admitted += 1;
                    stats.promoted_after_successor += 1;
                }
                Err(error) if error.is::<AwaitingSuccessor>() => {
                    if !self.waiting.retain(hash256(&raw), source, &raw) {
                        self.intake(source).discarded_after_recheck += 1;
                    }
                }
                _ => self.intake(source).discarded_after_recheck += 1,
            }
        }
    }
}
