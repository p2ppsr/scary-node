//! Reconsider a native payment when its witnessed funding block gains a successor.
use super::*;
use serde::{de, Deserialize, Deserializer, Serialize};
use std::fmt;

#[derive(Debug)]
pub(super) struct AwaitingSuccessor(pub Hash);

impl std::fmt::Display for AwaitingSuccessor {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("creation block has no observed successor")
    }
}
impl std::error::Error for AwaitingSuccessor {}

// The durable representation is just the arrival sequence. Its index and
// byte counter are reconstructed under the limits of the restoring process.
#[derive(Serialize, Deserialize)]
struct Offer(TxSource, #[serde(with = "store::arc_bytes")] Arc<[u8]>);

#[derive(Default, Serialize)]
#[serde(transparent)]
pub(super) struct WaitingOffers {
    arrivals: Vec<Offer>,
    #[serde(skip)]
    ids: HashSet<Hash>,
    #[serde(skip)]
    bytes: usize,
}

struct WaitingVisitor<'a>(&'a Capacity);

impl<'de> de::Visitor<'de> for WaitingVisitor<'_> {
    type Value = WaitingOffers;

    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("an ordered sequence of waiting transaction offers")
    }

    fn visit_seq<A: de::SeqAccess<'de>>(self, mut seq: A) -> Result<Self::Value, A::Error> {
        let limits = self.0;
        if seq
            .size_hint()
            .is_some_and(|n| n > limits.pending_transactions)
        {
            return Err(de::Error::custom(
                "waiting checkpoint exceeds transaction count capacity",
            ));
        }
        let mut waiting = WaitingOffers::default();
        while let Some(Offer(source, raw)) = seq.next_element()? {
            if waiting.len() >= limits.pending_transactions {
                return Err(de::Error::custom(
                    "waiting checkpoint exceeds transaction count capacity",
                ));
            }
            if raw.len() > limits.transaction_bytes {
                return Err(de::Error::custom(
                    "waiting checkpoint exceeds per-transaction capacity",
                ));
            }
            if raw.len() > limits.pending_bytes.saturating_sub(waiting.bytes) {
                return Err(de::Error::custom(
                    "waiting checkpoint exceeds queued byte capacity",
                ));
            }
            let mut framing = raw.as_ref();
            wire::transaction(&mut framing, |_| {}).map_err(|error| {
                de::Error::custom(format!("malformed waiting checkpoint transaction: {error}"))
            })?;
            if !framing.is_empty() {
                return Err(de::Error::custom(
                    "trailing bytes in waiting checkpoint transaction",
                ));
            }
            let id = hash256(&raw);
            if !waiting.ids.insert(id) {
                return Err(de::Error::custom(
                    "duplicate transaction in waiting checkpoint",
                ));
            }
            waiting.push(source, raw);
        }
        Ok(waiting)
    }
}

impl<'de> Deserialize<'de> for WaitingOffers {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        d.deserialize_seq(WaitingVisitor(capacity()))
    }
}

impl WaitingOffers {
    pub(super) fn is_empty(&self) -> bool {
        self.arrivals.is_empty()
    }
    pub(super) fn len(&self) -> usize {
        self.arrivals.len()
    }
    pub(super) fn bytes(&self) -> usize {
        self.bytes
    }
    pub(super) fn contains(&self, id: &Hash) -> bool {
        self.ids.contains(id)
    }

    fn push(&mut self, source: TxSource, raw: Arc<[u8]>) {
        self.bytes += raw.len();
        self.arrivals.push(Offer(source, raw));
    }

    fn retain(&mut self, id: Hash, source: TxSource, raw: &[u8]) -> bool {
        if self.len() >= capacity().pending_transactions
            || raw.len() > capacity().pending_bytes.saturating_sub(self.bytes)
            || !self.ids.insert(id)
        {
            return false;
        }
        self.push(source, Arc::from(raw));
        true
    }
}

impl Mining {
    pub(crate) fn knows_offer(&self, id: &Hash) -> bool {
        self.pending.contains_key(id) || self.waiting.contains(id)
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
        if !self.waiting.is_empty() && self.waiting.contains(&hash256(raw)) {
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
                        self.offers_dirty = true;
                        self.intake(source).awaiting_successor += 1;
                        return;
                    }
                }
                self.intake(source).ineligible += 1;
            }
        }
    }

    pub(crate) fn reconsider_waiting(&mut self) {
        if self.waiting.is_empty()
            || self.shutting_down
            || self.storage_failure.is_some()
            || self.blocked
            || self.awaiting_height.is_some()
            || !self.directives.ready()
        {
            return;
        }
        // First eligible arrival wins conflicting spends. Every offer goes
        // through admission again on the new branch and current directive state.
        let previous_count = self.waiting.len();
        for Offer(source, raw) in std::mem::take(&mut self.waiting).arrivals {
            let count = self.pending.len();
            match self.submit_tx(&raw) {
                Ok(_) if self.pending.len() > count => {
                    let stats = self.intake(source);
                    stats.admitted += 1;
                    stats.promoted_after_successor += 1;
                }
                Err(error) => {
                    if let Some(AwaitingSuccessor(id)) = error.downcast_ref() {
                        // These are a subset of the original distinct, bounded
                        // offers. Admission already hashed these exact bytes.
                        self.waiting.ids.insert(*id);
                        self.waiting.push(source, raw);
                    } else {
                        self.intake(source).discarded_after_recheck += 1;
                    }
                }
                Ok(_) => self.intake(source).discarded_after_recheck += 1,
            }
        }
        self.offers_dirty |= self.waiting.len() != previous_count;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn restore_refuses_exceeded_limits_and_malformed_or_duplicate_rows() {
        let mut raw = vec![1, 0, 0, 0, 1];
        raw.extend([7; 36]);
        raw.extend([0, 255, 255, 255, 255, 1]);
        raw.extend(10u64.to_le_bytes());
        raw.extend([1, 81, 0, 0, 0, 0]);
        let mut other = raw.clone();
        other[5] += 1;
        let bytes = raw.len();
        let read = |rows: &[(&str, &[u8])], count, total_bytes, tx_bytes| {
            let fields: Vec<_> = rows
                .iter()
                .map(|(source, raw)| (*source, serde_bytes::Bytes::new(raw)))
                .collect();
            let encoded = rmp_serde::to_vec(&fields).unwrap();
            let mut decoder = rmp_serde::Deserializer::from_read_ref(&encoded);
            let limits = Capacity {
                pending_transactions: count,
                pending_bytes: total_bytes,
                transaction_bytes: tx_bytes,
                ..Capacity::load().unwrap()
            };
            (&mut decoder).deserialize_seq(WaitingVisitor(&limits))
        };
        let rows = [("sv", raw.as_slice()), ("subtree", other.as_slice())];
        let valid = read(&rows, 2, 2 * bytes, bytes).unwrap();
        assert_eq!(valid.len(), 2);
        assert_eq!(valid.bytes(), 2 * bytes);
        let bad = |rows: &[(&str, &[u8])], count, total, single, msg| {
            let err = read(rows, count, total, single).err().unwrap();
            assert!(err.to_string().contains(msg), "{err}");
        };
        bad(&rows, 1, bytes * 2, bytes, "transaction count capacity");
        bad(&rows, 2, bytes * 2 - 1, bytes, "queued byte capacity");
        bad(&rows, 2, bytes * 2, bytes - 1, "per-transaction capacity");
        bad(
            &[("sv", &raw), ("subtree", &raw)],
            2,
            bytes * 2,
            bytes,
            "duplicate transaction",
        );
        bad(&[("sv", &[])], 1, bytes, bytes, "malformed waiting");
        let mut trailing = raw.clone();
        trailing.push(0);
        bad(
            &[("sv", &trailing)],
            1,
            bytes + 1,
            bytes + 1,
            "trailing bytes",
        );
        bad(&[("unknown", &raw)], 1, bytes, bytes, "unknown variant");
    }
}
