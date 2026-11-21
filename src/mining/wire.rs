//! Bounded framing of peer data before passing a selected transaction to the SDK.
use anyhow::{bail, ensure, Context, Result};
use bsv::primitives::hash::hash256;

pub type Hash = [u8; 32];

pub fn display_hash(hash: Hash) -> String {
    hex::encode(hash.into_iter().rev().collect::<Vec<_>>())
}

pub fn take<'a>(reader: &mut &'a [u8], size: usize) -> Result<&'a [u8]> {
    ensure!(size <= reader.len(), "truncated wire object");
    let (item, rest) = reader.split_at(size);
    *reader = rest;
    Ok(item)
}

pub fn varint(reader: &mut &[u8]) -> Result<usize> {
    let value = match take(reader, 1)?[0] {
        253 => {
            let n = u16::from_le_bytes(take(reader, 2)?.try_into()?) as u64;
            ensure!(n >= 253, "non-minimal compact size");
            n
        }
        254 => {
            let n = u32::from_le_bytes(take(reader, 4)?.try_into()?) as u64;
            ensure!(n > u16::MAX as u64, "non-minimal compact size");
            n
        }
        255 => {
            let n = u64::from_le_bytes(take(reader, 8)?.try_into()?);
            ensure!(n > u32::MAX as u64, "non-minimal compact size");
            n
        }
        n => n as u64,
    };
    Ok(value.try_into()?)
}

pub fn put_varint(out: &mut Vec<u8>, n: usize) {
    if n < 253 {
        out.push(n as u8);
    } else if n <= u16::MAX as usize {
        out.push(253);
        out.extend((n as u16).to_le_bytes());
    } else if n <= u32::MAX as usize {
        out.push(254);
        out.extend((n as u32).to_le_bytes());
    } else {
        out.push(255);
        out.extend((n as u64).to_le_bytes());
    }
}

pub enum Event<'a> {
    Input(Hash, u32, &'a [u8]),
    Output(u32, u64, &'a [u8]),
}

pub fn transaction<'a>(
    reader: &mut &'a [u8],
    mut event: impl FnMut(Event<'a>),
) -> Result<&'a [u8]> {
    let start = *reader;
    take(reader, 4)?;
    let inputs = varint(reader)?;
    ensure!(
        inputs > 0 && inputs <= reader.len() / 41,
        "invalid input count"
    );
    for _ in 0..inputs {
        let txid = take(reader, 32)?.try_into()?;
        let vout = u32::from_le_bytes(take(reader, 4)?.try_into()?);
        let size = varint(reader)?;
        let script = take(reader, size)?;
        take(reader, 4)?;
        event(Event::Input(txid, vout, script));
    }
    let outputs = varint(reader)?;
    ensure!(
        outputs > 0 && outputs <= reader.len() / 9,
        "invalid output count"
    );
    for i in 0..outputs {
        let value = u64::from_le_bytes(take(reader, 8)?.try_into()?);
        let size = varint(reader)?;
        event(Event::Output(i.try_into()?, value, take(reader, size)?));
    }
    take(reader, 4)?;
    Ok(&start[..start.len() - reader.len()])
}

/// Contextual locktime can be read without copying every script into SDK objects.
pub fn final_under(raw: &[u8], height: u32, median: Option<u32>) -> Result<Option<bool>> {
    let lock = u32::from_le_bytes(
        raw.get(raw.len().checked_sub(4).context("missing locktime")?..)
            .context("missing locktime")?
            .try_into()?,
    );
    if lock == 0 {
        return Ok(Some(true));
    }
    let mut r = raw;
    take(&mut r, 4)?;
    let n = varint(&mut r)?;
    let mut final_inputs = true;
    for _ in 0..n {
        take(&mut r, 36)?;
        let len = varint(&mut r)?;
        take(&mut r, len)?;
        final_inputs &= take(&mut r, 4)? == [255; 4];
    }
    if final_inputs {
        return Ok(Some(true));
    }
    Ok(if lock < 500_000_000 {
        Some(lock < height)
    } else {
        median.map(|t| lock < t)
    })
}
pub fn p2sh(script: &[u8]) -> bool {
    script.len() == 23 && script[0] == 0xa9 && script[1] == 20 && script[22] == 0x87
}

pub fn joined(left: Hash, right: Hash) -> Hash {
    let mut both = [0; 64];
    both[..32].copy_from_slice(&left);
    both[32..].copy_from_slice(&right);
    hash256(&both)
}

/// Return the root and the branches for the first (coinbase) leaf.
pub fn merkle(mut hashes: Vec<Hash>) -> Result<(Hash, Vec<Hash>)> {
    ensure!(!hashes.is_empty(), "empty merkle tree");
    let mut branches = Vec::new();
    while hashes.len() > 1 {
        if hashes.as_chunks::<2>().0.iter().any(|p| p[0] == p[1]) {
            bail!("ambiguous duplicate merkle pair");
        }
        branches.push(hashes[1]);
        if hashes.len() % 2 == 1 {
            hashes.push(*hashes.last().unwrap());
        }
        hashes = hashes
            .as_chunks::<2>()
            .0
            .iter()
            .map(|p| joined(p[0], p[1]))
            .collect();
    }
    Ok((hashes[0], branches))
}

pub fn as_le_target(bits: u32) -> Result<Hash> {
    let size = (bits >> 24) as usize;
    let word = bits & 0x007fffff;
    ensure!(
        bits & 0x00800000 == 0 && word != 0,
        "invalid compact target"
    );
    let mut target = [0; 32];
    if size <= 3 {
        target[..4].copy_from_slice(&(word >> (8 * (3 - size))).to_le_bytes());
    } else {
        for (i, byte) in word.to_le_bytes()[..3].iter().copied().enumerate() {
            if byte != 0 {
                *target
                    .get_mut(size - 3 + i)
                    .context("compact target overflow")? = byte;
            }
        }
    }
    ensure!(target != [0; 32], "zero compact target");
    Ok(target)
}

pub fn meets_target(digest: Hash, bits: u32) -> Result<bool> {
    Ok(digest
        .iter()
        .rev()
        .cmp(as_le_target(bits)?.iter().rev())
        .is_le())
}

pub fn height_push(height: u32) -> Vec<u8> {
    if height == 0 {
        return vec![0];
    }
    if height <= 16 {
        return vec![0x50 + height as u8];
    }
    let mut n = height;
    let mut data = Vec::new();
    while n != 0 {
        data.push(n as u8);
        n >>= 8;
    }
    if data.last().unwrap() & 0x80 != 0 {
        data.push(0);
    }
    let mut script = vec![data.len() as u8];
    script.extend(data);
    script
}
