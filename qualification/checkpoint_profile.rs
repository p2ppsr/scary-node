// This test module is appended only to the archived node source used by the benchmark.
use super::{Hashed, MAGIC};
use crate::mining::Mining;
use anyhow::{ensure, Result};
use serde_json::json;
use sha2::{Digest, Sha256};
use std::{
    fs::{self, File},
    io::{BufWriter, Read, Write},
    path::{Path, PathBuf},
    time::Instant,
};

const CAPACITY: usize = 8192;

enum Payload<'a> {
    Typed(&'a Mining),
    Original(&'a [u8]),
}

impl Payload<'_> {
    fn serialize<W: Write>(&self, out: &mut W) -> Result<()> {
        match self {
            Self::Typed(state) => rmp_serde::encode::write_named(out, state)?,
            Self::Original(bytes) => {
                for chunk in bytes.chunks(CAPACITY) {
                    out.write_all(chunk)?;
                }
            }
        }
        Ok(())
    }
}

#[derive(Clone, Copy)]
enum Order {
    Existing,
    BufferedHash,
}

impl Order {
    fn label(self) -> &'static str {
        match self {
            Self::Existing => "existing",
            Self::BufferedHash => "buffered_hash",
        }
    }
}

// Use the original Hashed<File> implementation for both paths. Count callbacks in a
// separate untimed pass so the high-frequency baseline does not pay extra instrumentation.
#[derive(Default)]
struct Counts {
    calls: u64,
    bytes: u64,
}

impl Write for Counts {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        self.calls += 1;
        self.bytes += bytes.len() as u64;
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

fn save(path: &Path, order: Order, payload: &Payload<'_>) -> Result<serde_json::Value> {
    let temp = path.join("witness.next");
    let dest = path.join("witness.state");
    let start = Instant::now();
    let mut file = File::create(&temp)?;
    let opened = start.elapsed().as_nanos() as u64;
    let encoded = match order {
        Order::Existing => {
            let mut buffer = BufWriter::with_capacity(CAPACITY, &mut file);
            buffer.write_all(MAGIC)?;
            let mut writer = Hashed {
                io: buffer,
                digest: Sha256::new(),
            };
            payload.serialize(&mut writer)?;
            let encoded = start.elapsed().as_nanos() as u64;
            let Hashed { mut io, digest } = writer;
            io.write_all(&digest.finalize())?;
            io.flush()?;
            encoded
        }
        Order::BufferedHash => {
            file.write_all(MAGIC)?;
            let mut writer = Hashed {
                io: &mut file,
                digest: Sha256::new(),
            };
            let mut buffer = BufWriter::with_capacity(CAPACITY, &mut writer);
            payload.serialize(&mut buffer)?;
            let encoded = start.elapsed().as_nanos() as u64;
            buffer.flush()?;
            drop(buffer);
            let Hashed { io, digest } = writer;
            io.write_all(&digest.finalize())?;
            io.flush()?;
            encoded
        }
    };
    let flushed = start.elapsed().as_nanos() as u64;
    file.sync_all()?;
    let synced = start.elapsed().as_nanos() as u64;
    fs::rename(temp, dest)?;
    let renamed = start.elapsed().as_nanos() as u64;
    File::open(path)?.sync_all()?;
    let total = start.elapsed().as_nanos() as u64;
    Ok(
        json!({"writer":order.label(),"open_ns":opened,"encode_hash_write_ns":encoded-opened,
        "checksum_flush_ns":flushed-encoded,"file_sync_ns":synced-flushed,"rename_ns":renamed-synced,
        "directory_sync_ns":total-renamed,"total_ns":total}),
    )
}

fn file_matches(path: &Path, expected: &[u8]) -> Result<()> {
    let mut file = File::open(path)?;
    ensure!(
        file.metadata()?.len() == expected.len() as u64,
        "different wire length"
    );
    let mut buffer = vec![0; 1 << 20];
    let mut digest = Sha256::new();
    let end = expected.len() - 32;
    for (index, part) in expected.chunks(buffer.len()).enumerate() {
        let slice = &mut buffer[..part.len()];
        file.read_exact(slice)?;
        ensure!(slice == part, "different bytes at block {}", index);
        let offset = index * (1 << 20);
        let low = 8_usize.saturating_sub(offset);
        let high = end.saturating_sub(offset).min(slice.len());
        if low < high {
            digest.update(&slice[low..high]);
        }
    }
    ensure!(
        expected.starts_with(MAGIC) && digest.finalize()[..] == expected[end..],
        "checksum mismatch"
    );
    Ok(())
}

#[test]
#[ignore]
fn profile() -> Result<()> {
    let source = PathBuf::from(std::env::var("SCARY_PROFILE_SOURCE")?);
    let scratch = PathBuf::from(std::env::var("SCARY_PROFILE_SCRATCH")?);
    let target = scratch.join("witness.state");
    let original = fs::read(&source)?;
    file_matches(&source, &original)?;
    let end = original.len() - 32;
    let original_payload = &original[8..end];
    let state: Mining = rmp_serde::from_slice(original_payload)?;
    let typed = Payload::Typed(&state);
    let earlier = Order::Existing;
    let later = Order::BufferedHash;
    for order in [earlier, later] {
        save(&scratch, order, &Payload::Original(original_payload))?;
        file_matches(&target, &original)?;
    }
    let start = Instant::now();
    let mut direct = Counts::default();
    typed.serialize(&mut direct)?;
    let sink_ns = start.elapsed().as_nanos() as u64;
    let mut buffered = BufWriter::with_capacity(CAPACITY, Counts::default());
    typed.serialize(&mut buffered)?;
    buffered.flush()?;
    let buffered = buffered.get_ref();
    ensure!(direct.bytes == buffered.bytes, "payload lengths differ");
    let start = Instant::now();
    let digest = Sha256::digest(original_payload);
    let hash_ns = start.elapsed().as_nanos() as u64;
    ensure!(digest[..] == original[end..], "original checksum mismatch");
    drop(original);
    let warm_a = save(&scratch, earlier, &typed)?;
    let reference = fs::read(&target)?;
    file_matches(&target, &reference)?;
    fs::hard_link(&target, scratch.join("typed.reference"))?;
    let warm_b = save(&scratch, later, &typed)?;
    file_matches(&target, &reference)?;
    println!(
        "CHECKPOINT_PROFILE {}",
        json!({"event":"warmup","bytes":reference.len(),
        "typed_file_sha256":hex::encode(Sha256::digest(&reference)), "original_payload_sha256":hex::encode(digest),
        "existing_hash_calls":direct.calls,"buffered_hash_calls":buffered.calls,"payload_bytes":direct.bytes,
        "counting_sink_encode_ns":sink_ns,"contiguous_digest_ns":hash_ns,"saves":[warm_a,warm_b]})
    );
    for pair in 0..24 {
        let order = if pair % 2 == 0 {
            [earlier, later]
        } else {
            [later, earlier]
        };
        for (position, writer) in order.iter().enumerate() {
            let result = save(&scratch, *writer, &typed)?;
            file_matches(&target, &reference)?;
            println!(
                "CHECKPOINT_PROFILE {}",
                json!({"event":"sample","pair":pair,"position":position,"save":result})
            );
        }
    }
    let _decoded: Mining = rmp_serde::from_slice(&reference[8..reference.len() - 32])?;
    Ok(())
}
