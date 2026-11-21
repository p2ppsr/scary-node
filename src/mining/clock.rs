//! Compare the process wall clock with UTC reported in ordinary SV version
//! handshakes. Peers can withhold work on disagreement, never relax a time limit.
use super::{clock_step, now, MAX_CLOCK_STEP_SECONDS};
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    net::{IpAddr, SocketAddr},
};

#[derive(Default)]
pub(super) struct PeerClocks {
    samples: BTreeMap<SocketAddr, i64>,
    groups: usize,
    disagreement: bool,
    // Offsets use the process monotonic anchor. Keep the last corroborated
    // value through disconnects so losing a source cannot lift a clock hold.
    reference: Option<i64>,
}

pub(crate) fn version_clock(timestamp: i64) -> Option<i64> {
    if !(1..=i64::from(u32::MAX)).contains(&timestamp) {
        return None;
    }
    Some(timestamp - i64::from(now().ok()?) + clock_step()?)
}

impl PeerClocks {
    pub fn update(&mut self, addr: SocketAddr, sample: Option<i64>, regtest: bool) {
        if let Some(offset) = sample {
            self.samples.insert(addr, offset);
        } else {
            self.samples.remove(&addr);
        }
        let mut groups = BTreeMap::<Vec<u8>, Vec<i64>>::new();
        for (addr, sample) in &self.samples {
            let group = match addr.ip().to_canonical() {
                IpAddr::V4(ip) => ip.octets()[..2].to_vec(),
                IpAddr::V6(ip) => ip.octets()[..4].to_vec(),
            };
            groups.entry(group).or_default().push(*sample);
        }
        self.groups = groups.len();
        if self.groups >= if regtest { 1 } else { 3 } {
            let mut representatives: Vec<_> = groups.values_mut().map(|g| median(g)).collect();
            let center = median(&mut representatives);
            let agreeing = representatives
                .iter()
                .filter(|s| (**s - center).abs() <= MAX_CLOCK_STEP_SECONDS)
                .count();
            self.disagreement = agreeing <= self.groups / 2;
            if !self.disagreement {
                self.reference = Some(center);
            }
        }
    }

    fn offset(&self) -> Option<i64> {
        Some(self.reference? - clock_step()?)
    }

    pub fn ready(&self) -> bool {
        !self.disagreement
            && self
                .offset()
                .is_none_or(|offset| offset.abs() <= MAX_CLOCK_STEP_SECONDS)
    }

    pub fn candidate_time_limit(&self, wall: u32) -> u32 {
        let earlier = i64::from(wall) + self.offset().unwrap_or(0).min(0);
        earlier.clamp(0, i64::from(u32::MAX)) as u32
    }

    pub fn status(&self, regtest: bool) -> Value {
        json!({"connected_endpoints":self.samples.len(), "connected_network_groups":self.groups,
               "minimum_groups":if regtest {1} else {3}, "disagreeing_samples":self.disagreement, "offset_from_wall_seconds":self.offset(),
               "hold_threshold_seconds":MAX_CLOCK_STEP_SECONDS})
    }
}

fn median(samples: &mut [i64]) -> i64 {
    samples.sort_unstable();
    let m = samples.len() / 2;
    // Version timestamps are uint32. Adding these signed offsets fits i64.
    if samples.len().is_multiple_of(2) {
        (samples[m - 1] + samples[m]) / 2
    } else {
        samples[m]
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn same_group_has_one_vote_and_disconnect_cannot_clear_a_known_offset() {
        let mut c = PeerClocks::default();
        let a = "203.0.113.1:8333".parse().unwrap();
        let b = "198.51.100.7:8333".parse().unwrap();
        let d = "[2001:db8::7]:8333".parse().unwrap();
        for ip in [a, "203.0.113.2:8333".parse().unwrap(), b] {
            c.update(ip, Some(600), false);
        }
        assert_eq!(c.groups, 2);
        assert_eq!(c.reference, None);
        c.update(d, Some(0), false);
        assert_eq!(c.reference, Some(600));
        assert!(!c.ready());
        c.update(a, None, false);
        c.update(b, None, false);
        assert!(!c.ready());
        c.update(b, Some(0), false);
        assert!(c.ready());
        assert_eq!(c.candidate_time_limit(1_000), 1_000);
        c.update(b, Some(-600), false);
        assert!(c.disagreement && !c.ready());
        assert!(version_clock(-1).is_none());
    }
}
