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

#[derive(Serialize)]
struct Transition {
    at_ms: u64,
    state: &'static str,
    height: Option<u32>,
}

#[derive(Serialize)]
struct Solution {
    hash: String,
    parent: String,
    height: u32,
    job: String,
    queued_at_ms: u64,
    published_at_ms: Option<u64>,
    publication_error: Option<String>,
    peer_tip_reporters: Vec<String>,
    first_inclusion_at_ms: Option<u64>,
    first_settlement_at_ms: Option<u64>,
    chain_state: &'static str,
    transitions: VecDeque<Transition>,
}

impl Solution {
    fn update(&mut self, state: &'static str, height: Option<u32>) {
        if self.chain_state == state {
            return;
        }
        self.chain_state = state;
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
            state,
            height,
        });
    }
}

#[derive(Default)]
pub(super) struct Lifecycle {
    records: VecDeque<Solution>,
}

impl Lifecycle {
    pub fn queue(&mut self, hash: Hash, parent: Hash, height: u32, job: String) {
        if self.records.len() == HISTORY {
            self.records.pop_front();
        }
        self.records.push_back(Solution {
            hash: wire::display_hash(hash),
            parent: wire::display_hash(parent),
            height,
            job,
            queued_at_ms: unix_ms(),
            published_at_ms: None,
            publication_error: None,
            peer_tip_reporters: Vec::new(),
            first_inclusion_at_ms: None,
            first_settlement_at_ms: None,
            chain_state: "awaiting_observation",
            transitions: VecDeque::new(),
        });
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

    pub fn peer_tip(&mut self, peer: &str, hash: &str, height: u32) {
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
            }
        }
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
            if matches!(record.chain_state, "observed_active" | "competing_branch") {
                record.update("coverage_lost", None);
            }
        }
    }

    pub fn status(&self) -> Value {
        json!(self.records)
    }
}

impl Mining {
    pub fn publication(&mut self, hash: &str, error: Option<String>) {
        self.lifecycle.publication(hash, error);
    }

    pub fn reported_tip(&mut self, peer: &str, hash: &str, height: u32) {
        self.lifecycle.peer_tip(peer, hash, height);
    }
}
