//! An unconfirmed Teranode subtree is transaction inventory. Its publisher
//! can identify candidate bytes but cannot supply evidence for an input.
use super::fetch::{download, Request};
use crate::mining::{body_limit, wire, Mining, TxSource};
use anyhow::{ensure, Context, Result};
use bsv::primitives::hash::hash256;
use std::{
    borrow::Cow,
    time::{Duration, Instant},
};
use tokio::sync::Mutex;

const EF_MARKER: &[u8] = &[0, 0, 0, 0, 0, 0xef];

/// Teranode can return mixed conventional and extended (BRC-30) transactions.
/// Discard EF prevout metadata before computing the conventional txid or
/// passing the transaction to the entrant's own evidenced-input validator.
fn next_transaction<'a>(data: &mut &'a [u8]) -> Result<Cow<'a, [u8]>> {
    if data.get(4..10) != Some(EF_MARKER) {
        return Ok(Cow::Borrowed(wire::transaction(data, |_| {})?));
    }
    let mut standard = wire::take(data, 4)?.to_vec();
    wire::take(data, EF_MARKER.len())?;
    let count = wire::varint(data)?;
    ensure!(
        count > 0 && count <= data.len() / 50,
        "invalid extended input count"
    );
    wire::put_varint(&mut standard, count);
    for _ in 0..count {
        let start = *data;
        wire::take(data, 36)?;
        let script_len = wire::varint(data)?;
        wire::take(data, script_len)?;
        wire::take(data, 4)?;
        standard.extend_from_slice(&start[..start.len() - data.len()]);
        wire::take(data, 8)?;
        let source_script_len = wire::varint(data)?;
        wire::take(data, source_script_len)?;
    }
    let outputs = *data;
    let count = wire::varint(data)?;
    ensure!(
        count > 0 && count <= data.len() / 9,
        "invalid extended output count"
    );
    for _ in 0..count {
        wire::take(data, 8)?;
        let size = wire::varint(data)?;
        wire::take(data, size)?;
    }
    wire::take(data, 4)?;
    standard.extend_from_slice(&outputs[..outputs.len() - data.len()]);
    Ok(Cow::Owned(standard))
}

/// Occupied levels hold the unpaired left root for that height. The number
/// of hashes retained grows logarithmically with the size of the subtree.
#[derive(Default)]
struct Frontier {
    levels: Vec<Option<wire::Hash>>,
    mutated: bool,
}

impl Frontier {
    fn push(&mut self, mut right: wire::Hash) {
        for slot in &mut self.levels {
            if let Some(left) = slot.take() {
                self.mutated |= left == right;
                right = wire::joined(left, right);
            } else {
                *slot = Some(right);
                return;
            }
        }
        self.levels.push(Some(right));
    }

    fn root(self) -> Option<wire::Hash> {
        if self.mutated {
            return None;
        }
        let mut pending = None;
        for (level, left) in self.levels.into_iter().enumerate() {
            let Some(left) = left else { continue };
            if let Some((mut right, mut at)) = pending {
                while at < level {
                    right = wire::joined(right, right);
                    at += 1;
                }
                if left == right {
                    return None;
                }
                pending = Some((wire::joined(left, right), level + 1));
            } else {
                pending = Some((left, level));
            }
        }
        pending.map(|(h, _)| h)
    }
}

fn verify_data(mut cursor: &[u8], root: &str) -> Result<()> {
    let mut ordinary = Frontier::default();
    // The first subtree in a candidate may commit to the 0xff coinbase
    // placeholder. The reference omits that placeholder from the data stream.
    let mut with_coinbase = Frontier::default();
    with_coinbase.push([0xff; 32]);
    while !cursor.is_empty() {
        let id = hash256(&next_transaction(&mut cursor)?);
        ordinary.push(id);
        with_coinbase.push(id);
    }
    ensure!(
        [ordinary, with_coinbase]
            .into_iter()
            .filter_map(Frontier::root)
            .any(|h| wire::display_hash(h) == root),
        "subtree transaction root mismatch"
    );
    Ok(())
}

pub(super) async fn fetch_verified(
    network: &str,
    request: &Request,
) -> Result<crate::body::BlockBody> {
    let path = format!("subtree_data/{}", request.hash);
    let data = tokio::time::timeout(
        Duration::from_secs(55 + (body_limit() as u64 >> 20)),
        download(network, &request.base, &path, None),
    )
    .await
    .context("subtree download timed out")??;
    let root = request.hash.clone();
    tokio::task::spawn_blocking(move || -> Result<_> {
        verify_data(&data, &root)?;
        Ok(data)
    })
    .await?
}

pub(super) async fn admit(data: &[u8], state: &Mutex<Mining>) -> Result<usize> {
    let mut cursor = data;
    let mut processed = 0;
    // Bound time under the shared mining lock. Block processing, directives
    // and Stratum can advance between consecutive groups of ordinary offers.
    while !cursor.is_empty() {
        {
            let mut mining = state.lock().await;
            let began = Instant::now();
            for _ in 0..32 {
                if cursor.is_empty() {
                    break;
                }
                mining.peer_tx(TxSource::Subtree, &next_transaction(&mut cursor)?);
                processed += 1;
                if began.elapsed() >= Duration::from_millis(4) {
                    break;
                }
            }
        }
        tokio::task::yield_now().await;
    }
    Ok(processed)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ordinary(index: u8) -> Vec<u8> {
        let mut raw = vec![1, 0, 0, 0, 1];
        raw.extend([index; 32]);
        raw.extend([0, 0, 0, 0, 1, 0x51, 0xff, 0xff, 0xff, 0xff]);
        raw.extend([1, 2, 0, 0, 0, 0, 0, 0, 0, 1, 0x51, 0, 0, 0, 0]);
        raw
    }

    #[test]
    fn streaming_roots_match_the_candidate_merkle_implementation() {
        for width in 1..=65 {
            let mut frontier = Frontier::default();
            let ids = (0..width).map(|n| hash256(&[n])).collect::<Vec<_>>();
            for id in &ids {
                frontier.push(*id);
            }
            assert_eq!(frontier.root(), Some(wire::merkle(ids).unwrap().0));
        }
        let mut duplicate = Frontier::default();
        duplicate.push([1; 32]);
        duplicate.push([1; 32]);
        assert!(duplicate.root().is_none());
    }

    #[test]
    fn verifies_mixed_ordinary_and_extended_streams_with_or_without_placeholder() {
        let first = ordinary(1);
        let second = ordinary(2);
        let mut data = first.clone();
        data.extend_from_slice(&second[..4]);
        data.extend(EF_MARKER);
        data.extend_from_slice(&second[4..47]);
        data.extend([0xff; 8]); // Claims an arbitrary, untrusted source amount.
        data.extend([1, 0x6a]); // Claims an arbitrary source script.
        data.extend_from_slice(&second[47..]);
        let mut cursor = &data[..];
        assert_eq!(&*next_transaction(&mut cursor).unwrap(), first);
        assert_eq!(&*next_transaction(&mut cursor).unwrap(), second);
        assert!(cursor.is_empty());
        let mut ids = vec![hash256(&first), hash256(&second)];
        let root = wire::display_hash(wire::merkle(ids.clone()).unwrap().0);
        verify_data(&data, &root).unwrap();
        ids.insert(0, [0xff; 32]);
        let root = wire::display_hash(wire::merkle(ids).unwrap().0);
        verify_data(&data, &root).unwrap();
        assert!(verify_data(&data, &"00".repeat(32)).is_err());
        assert!(verify_data(&data[..data.len() - 1], &root).is_err());
    }
}
