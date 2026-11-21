//! Local solution records. Native peer claims remain separately attributed.
use super::*;
use serde::Serialize;

const HISTORY: usize = 256;
const SETTLEMENT_DEPTH: u32 = 2;

fn unix_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

#[derive(Serialize, serde::Deserialize)]
struct Transition {
    at_ms: u64,
    state: String,
    height: Option<u32>,
}

#[derive(Serialize, serde::Deserialize)]
struct Solution {
    #[serde(default)]
    subtree: Option<String>,
    hash: String,
    parent: String,
    height: u32,
    job: String,
    queued_at_ms: u64,
    published_at_ms: Option<u64>,
    publication_error: Option<String>,
    peer_tip_reporters: Vec<String>,
    #[serde(default)]
    deliveries: BTreeMap<String, BTreeMap<String, u64>>,
    first_inclusion_at_ms: Option<u64>,
    first_settlement_at_ms: Option<u64>,
    chain_state: String,
    transitions: VecDeque<Transition>,
}

impl Solution {
    fn update(&mut self, state: &'static str, height: Option<u32>) {
        if self.chain_state == state {
            return;
        }
        self.chain_state = state.into();
        let at_ms = unix_ms();
        match state {
            "observed_active" => {
                self.first_inclusion_at_ms.get_or_insert(at_ms);
            }
            "settled" => {
                self.first_inclusion_at_ms.get_or_insert(at_ms);
                self.first_settlement_at_ms.get_or_insert(at_ms);
            }
            _ => {}
        }
        if self.transitions.len() == 16 {
            self.transitions.pop_front();
        }
        self.transitions.push_back(Transition {
            at_ms,
            state: state.into(),
            height,
        });
    }
}

#[derive(Default, Serialize, serde::Deserialize)]
pub(super) struct Lifecycle {
    records: VecDeque<Solution>,
}

impl Lifecycle {
    pub fn queue(
        &mut self,
        hash: Hash,
        parent: Hash,
        height: u32,
        job: String,
        subtree: Option<Hash>,
    ) {
        if self.records.len() == HISTORY {
            self.records.pop_front();
        }
        self.records.push_back(Solution {
            subtree: subtree.map(wire::display_hash),
            hash: wire::display_hash(hash),
            parent: wire::display_hash(parent),
            height,
            job,
            queued_at_ms: unix_ms(),
            published_at_ms: None,
            publication_error: None,
            peer_tip_reporters: Vec::new(),
            deliveries: BTreeMap::new(),
            first_inclusion_at_ms: None,
            first_settlement_at_ms: None,
            chain_state: "awaiting_observation".into(),
            transitions: VecDeque::new(),
        });
    }

    pub(super) fn restore_subtree(&mut self, hash: Hash, txs: &[(Hash, &[u8])]) -> Result<()> {
        let hash = wire::display_hash(hash);
        if let Some(record) = self.records.iter_mut().find(|r| r.hash == hash) {
            let subtree = if txs.len() > 1 {
                let mut leaves: Vec<_> = txs.iter().map(|(id, _)| *id).collect();
                leaves[0] = [0xff; 32];
                Some(wire::display_hash(wire::merkle(leaves)?.0))
            } else {
                None
            };
            ensure!(
                record.subtree.is_none() || record.subtree == subtree,
                "checkpoint solution subtree mismatch"
            );
            record.subtree = subtree;
        }
        Ok(())
    }

    pub fn retire(&mut self, hash: &str, height: Option<u32>) {
        if let Some(record) = self
            .records
            .iter_mut()
            .rev()
            .find(|record| record.hash == hash)
        {
            record.update("directive_retired", height);
        }
    }
    pub fn publication(&mut self, hash: &str, error: Option<String>) {
        if let Some(record) = self
            .records
            .iter_mut()
            .rev()
            .find(|record| record.hash == hash)
        {
            if error.is_none() {
                record.published_at_ms.get_or_insert_with(unix_ms);
            }
            record.publication_error = error;
        }
    }

    pub fn peer_tip(&mut self, peer: &str, hash: &str, height: u32) -> bool {
        if let Some(record) = self
            .records
            .iter_mut()
            .rev()
            .find(|r| r.hash == hash && r.height == height)
        {
            if record.peer_tip_reporters.len() < 16
                && !record.peer_tip_reporters.iter().any(|p| p == peer)
            {
                record.peer_tip_reporters.push(peer.into());
                return true;
            }
        }
        false
    }

    pub fn observe(&mut self, headers: &VecDeque<[u8; 80]>, height: u32) {
        if self.records.is_empty() {
            return;
        }
        let branch: Vec<_> = headers
            .iter()
            .rev()
            .map(|h| wire::display_hash(hash256(h)))
            .collect();
        for record in &mut self.records {
            if let Some(depth) = height.checked_sub(record.height) {
                if let Some(active) = branch.get(depth as usize) {
                    let state = match (active == &record.hash, depth >= SETTLEMENT_DEPTH) {
                        (true, true) => "settled",
                        (true, false) => "observed_active",
                        (false, true) => "stale_at_depth",
                        (false, false) => "competing_branch",
                    };
                    record.update(state, Some(height));
                }
            } else if record.first_inclusion_at_ms.is_some()
                || record.chain_state != "awaiting_observation"
            {
                record.update("awaiting_observation", Some(height));
            }
        }
    }

    pub fn lost_view(&mut self) {
        for record in &mut self.records {
            if matches!(
                record.chain_state.as_str(),
                "observed_active" | "competing_branch"
            ) {
                record.update("coverage_lost", None);
            }
        }
    }

    pub fn status(&self) -> Value {
        json!(self.records)
    }
}

impl Mining {
    /// A peer can retrieve our selected active block while its complete body
    /// survives in the undo journal, or a current solution eligible to publish.
    pub(crate) fn requested_solution(
        &self,
        name: &str,
        subtree: bool,
    ) -> Option<(Hash, u32, axum::body::Bytes)> {
        if name.len() != 64 || !self.observation_window_ready() {
            return None;
        }
        self.lifecycle
            .records
            .iter()
            .rev()
            .filter(|r| {
                if subtree {
                    r.subtree.as_deref() == Some(name)
                } else {
                    r.hash == name
                }
            })
            .find_map(|r| {
                let mut id: Hash = [0; 32];
                hex::decode_to_slice(&r.hash, &mut id).ok()?;
                id.reverse();
                if self.directives.invalid.contains(&id) || self.invalid_descendants.contains(&id) {
                    return None;
                }
                if self.active_since_entry.get(&id) == Some(&r.height) {
                    if let Some(block) = self
                        .chain
                        .applied
                        .iter()
                        .find(|b| b.height == r.height && hash256(&b.raw[..80]) == id)
                    {
                        return Some((
                            id,
                            r.height,
                            axum::body::Bytes::from_owner(block.raw.clone()),
                        ));
                    }
                }
                let latest = self.solution_body.as_ref().filter(|s| s.hash == id)?;
                (self.relayable_solution() == Some(id)).then(|| {
                    (
                        id,
                        r.height,
                        axum::body::Bytes::from_owner(latest.raw.clone()),
                    )
                })
            })
    }

    pub fn publication(&mut self, hash: &str, error: Option<String>) {
        self.lifecycle.publication(hash, error);
    }

    pub fn record_delivery(&mut self, hash: &str, peer: &str, direction: &str) -> bool {
        let Some(record) = self
            .lifecycle
            .records
            .iter_mut()
            .rev()
            .find(|s| s.hash == hash)
        else {
            return false;
        };
        if !record.deliveries.contains_key(peer) && record.deliveries.len() >= 64 {
            return false;
        }
        let entry = record.deliveries.entry(peer.into()).or_default();
        if entry.contains_key(direction) {
            return false;
        }
        entry.insert(direction.into(), unix_ms());
        true
    }

    pub fn reported_tip(&mut self, peer: &str, hash: &str, height: u32) -> bool {
        self.lifecycle.peer_tip(peer, hash, height)
    }
}
