//! Contemporary BSV next-work calculation over a contiguous, oldest-first header window.
use anyhow::{anyhow, ensure, Result};
use num_bigint::BigUint;

const MAIN_LIMIT: u32 = 0x1d00ffff;
const REGTEST_LIMIT: u32 = 0x207fffff;
const WINDOW: usize = 144;
const SPACING: i64 = 600;

fn timestamp(h: &[u8; 80]) -> i64 {
    u32::from_le_bytes(h[68..72].try_into().unwrap()) as i64
}

fn bits(h: &[u8; 80]) -> u32 {
    u32::from_le_bytes(h[72..76].try_into().unwrap())
}

fn target(compact: u32) -> Result<BigUint> {
    let exponent = (compact >> 24) as usize;
    let coefficient = compact & 0x007fffff;
    ensure!(
        coefficient != 0 && compact & 0x00800000 == 0,
        "nonpositive target"
    );
    ensure!(exponent <= 34, "compact target overflow");
    let n = BigUint::from(coefficient);
    let value = if exponent <= 3 {
        n >> (8 * (3 - exponent))
    } else {
        n << (8 * (exponent - 3))
    };
    ensure!(
        value.bits() > 0 && value.bits() <= 256,
        "out-of-range target"
    );
    Ok(value)
}

fn encode(n: &BigUint) -> u32 {
    let bytes = n.to_bytes_be();
    let mut leading = [0u8; 4];
    let used = bytes.len().min(3);
    leading[1..1 + used].copy_from_slice(&bytes[..used]);
    let mut coefficient = u32::from_be_bytes(leading);
    let mut exponent = bytes.len() as u32;
    if coefficient & 0x00800000 != 0 {
        coefficient >>= 8;
        exponent += 1;
    }
    (exponent << 24) | coefficient
}

// The strict comparisons and order preserve the reference node's tie behavior.
fn suitable(headers: &[[u8; 80]], end: usize) -> usize {
    let mut idx = [end - 2, end - 1, end];
    for (a, b) in [(0, 2), (0, 1), (1, 2)] {
        if timestamp(&headers[idx[a]]) > timestamp(&headers[idx[b]]) {
            idx.swap(a, b);
        }
    }
    idx[1]
}

pub fn next_bits(headers: &[[u8; 80]], regtest: bool) -> Result<u32> {
    let parent = headers.last().ok_or_else(|| anyhow!("no parent header"))?;
    if regtest {
        ensure!(
            bits(parent) == REGTEST_LIMIT,
            "unexpected regtest difficulty"
        );
        return Ok(REGTEST_LIMIT);
    }
    ensure!(
        headers.len() >= WINDOW + 3,
        "waiting for 147 contiguous mainnet headers"
    );
    let end = headers.len() - 1;
    let first = suitable(headers, end - WINDOW);
    let last = suitable(headers, end);
    let scale = BigUint::from(1u8) << 256;
    let limit = target(MAIN_LIMIT)?;
    let mut work = BigUint::default();
    for header in &headers[first + 1..=last] {
        let n = target(bits(header))?;
        ensure!(n <= limit, "header target exceeds mainnet PoW limit");
        work += &scale / (n + 1u8);
    }
    let elapsed = (timestamp(&headers[last]) - timestamp(&headers[first]))
        .clamp(72 * SPACING, 288 * SPACING) as u64;
    let projected = work * (SPACING as u64) / elapsed;
    ensure!(
        projected != BigUint::default() && projected < scale,
        "invalid projected work"
    );
    let next = (&scale - &projected) / projected;
    Ok(encode(&next.min(limit)))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn headers(interval: u32) -> Vec<[u8; 80]> {
        (0..147u32)
            .map(|i| {
                let mut header = [0; 80];
                header[68..72].copy_from_slice(&(i * interval).to_le_bytes());
                header[72..76].copy_from_slice(&MAIN_LIMIT.to_le_bytes());
                header
            })
            .collect()
    }

    #[test]
    fn constant_work_at_target_spacing_and_both_time_clamps() {
        assert_eq!(next_bits(&headers(600), false).unwrap(), MAIN_LIMIT);
        assert_eq!(next_bits(&headers(2000), false).unwrap(), MAIN_LIMIT);
        assert_eq!(next_bits(&headers(1), false).unwrap(), 0x1c7fff80);
        assert!(next_bits(&headers(600)[1..], false).is_err());
    }

    #[test]
    fn invalid_targets_and_regtest() {
        assert!(target(0x01000001).is_err());
        assert!(target(0x1d80ffff).is_err());
        assert!(target(0x23010000).is_err());
        let mut parent = [0; 80];
        parent[72..76].copy_from_slice(&REGTEST_LIMIT.to_le_bytes());
        assert_eq!(next_bits(&[parent], true).unwrap(), REGTEST_LIMIT);
        assert!(next_bits(&[parent], false).is_err());
    }
}
