//! Crash-consistent snapshots of the observed branch, its undo journal, and
//! local solutions. Atomic rename makes the previous complete state usable if
//! the process exits before commit. The data-directory lock fences writers.
use super::*;
use serde::{Deserialize, Deserializer, Serialize, Serializer};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, File},
    io::{BufReader, BufWriter, Read, Seek, SeekFrom, Write},
    path::Path,
};

pub(super) mod arc_bytes {
    use super::*;
    pub fn serialize<S: Serializer>(b: &Arc<[u8]>, s: S) -> Result<S::Ok, S::Error> {
        s.serialize_bytes(b)
    }
    pub fn deserialize<'a, D: Deserializer<'a>>(d: D) -> Result<Arc<[u8]>, D::Error> {
        Ok(Arc::from(serde_bytes::ByteBuf::deserialize(d)?.into_vec()))
    }
}
#[derive(Serialize, Deserialize)]
struct Header(#[serde(with = "serde_bytes")] [u8; 80]);
pub(super) mod headers {
    use super::*;
    pub fn serialize<S: Serializer>(h: &VecDeque<[u8; 80]>, s: S) -> Result<S::Ok, S::Error> {
        s.collect_seq(h.iter().copied().map(Header))
    }
    pub fn deserialize<'a, D: Deserializer<'a>>(d: D) -> Result<VecDeque<[u8; 80]>, D::Error> {
        Ok(Vec::<Header>::deserialize(d)?
            .into_iter()
            .map(|h| h.0)
            .collect())
    }
}
pub(super) mod optional_header {
    use super::*;
    pub fn serialize<S: Serializer>(h: &Option<[u8; 80]>, s: S) -> Result<S::Ok, S::Error> {
        h.map(Header).serialize(s)
    }
    pub fn deserialize<'a, D: Deserializer<'a>>(d: D) -> Result<Option<[u8; 80]>, D::Error> {
        Ok(Option::<Header>::deserialize(d)?.map(|h| h.0))
    }
}
const MAGIC: &[u8; 8] = b"SCARYWS1";
struct Hashed<W> {
    io: W,
    digest: Sha256,
}
impl<W: Write> Write for Hashed<W> {
    fn write(&mut self, b: &[u8]) -> std::io::Result<usize> {
        let n = self.io.write(b)?;
        self.digest.update(&b[..n]);
        Ok(n)
    }
    fn flush(&mut self) -> std::io::Result<()> {
        self.io.flush()
    }
}
impl Mining {
    pub fn open_store(&mut self, dir: &Path) -> Result<()> {
        let file = dir.join("witness.state");
        if file.exists() {
            let mut reader = BufReader::new(File::open(&file)?);
            let size = reader.get_ref().metadata()?.len();
            ensure!(size >= 40, "short witness checkpoint");
            let mut magic = [0; 8];
            reader.read_exact(&mut magic)?;
            ensure!(&magic == MAGIC, "unsupported checkpoint version");
            let mut hash = Sha256::new();
            let n = std::io::copy(&mut (&mut reader).take(size - 40), &mut hash)?;
            ensure!(n == size - 40, "truncated witness checkpoint");
            let mut expected = [0; 32];
            reader.read_exact(&mut expected)?;
            ensure!(
                hash.finalize().as_slice() == expected,
                "witness checkpoint digest mismatch"
            );
            reader.seek(SeekFrom::Start(8))?;
            let mut restored: Self = rmp_serde::from_read(reader.take(n))?;
            ensure!(
                restored.regtest == self.regtest,
                "checkpoint is for a different network"
            );
            if restored.applied_sequence != 0 {
                self.directives
                    .restore_through(&dir.join("alerts"), restored.applied_sequence)?;
                ensure!(
                    self.directives.applied() == restored.applied_sequence,
                    "checkpoint lacks contiguous signed directive history"
                );
            }
            ensure!(
                restored.headers.len() <= HEADER_WINDOW
                    && restored
                        .headers
                        .iter()
                        .zip(restored.headers.iter().skip(1))
                        .all(|(p, h)| hash256(p) == h[4..36]),
                "checkpoint header continuity failure"
            );
            if restored.active_since_entry.is_empty() {
                if let Some(height) = restored.height {
                    for (i, h) in restored.headers.iter().rev().enumerate() {
                        if let Some(n) = height.checked_sub(i as u32) {
                            restored.active_since_entry.insert(hash256(h), n);
                        }
                    }
                }
            }
            restored.verify_restored_boundary()?;
            if restored
                .height
                .is_some_and(|h| h < interpreter::activation(restored.regtest))
            {
                restored.suspend();
            }
            restored.payout = std::mem::take(&mut self.payout);
            restored.directives =
                std::mem::replace(&mut self.directives, crate::alerts::unconfigured());
            restored.min_observed_blocks = self.min_observed_blocks;
            restored.revision += 1;
            restored.parent_epoch += 1;
            if restored.applied_sequence != restored.directives.applied() {
                restored.pending.clear();
                restored.apply_invalidations();
            }
            restored.recheck_pending();
            *self = restored;
        }
        self.path = Some(dir.to_path_buf());
        Ok(())
    }
    pub(crate) fn commit_directive_sequence(&mut self, sequence: u32) -> Result<()> {
        let previous = self.applied_sequence;
        self.applied_sequence = sequence;
        if let Err(e) = self.save() {
            self.applied_sequence = previous;
            return Err(e);
        }
        Ok(())
    }
    /// Fence workers and retain payments admitted since the last complete snapshot.
    pub(crate) fn flush_on_shutdown(&mut self) -> Result<()> {
        self.shutting_down = true;
        ensure!(
            self.storage_failure.is_none(),
            "cannot finalize a failed durable boundary"
        );
        if self.pending_dirty {
            self.save()?;
        }
        Ok(())
    }

    pub(crate) fn save(&mut self) -> Result<()> {
        let Some(dir) = self.path.as_ref() else {
            return Ok(());
        };

        ensure!(
            self.storage_failure.is_none(),
            "a failed durable write requires restoring the committed boundary"
        );
        let result = (|| -> Result<()> {
            let tmp = dir.join("witness.next");
            let mut io = BufWriter::new(File::create(&tmp)?);
            io.write_all(MAGIC)?;
            let mut hashed = Hashed {
                io,
                digest: Sha256::new(),
            };
            rmp_serde::encode::write_named(&mut hashed, self)?;
            let Hashed { mut io, digest } = hashed;
            io.write_all(&digest.finalize())?;
            io.flush()?;
            io.get_ref().sync_all()?;
            fs::rename(tmp, dir.join("witness.state"))?;
            File::open(dir)?.sync_all()?;
            Ok(())
        })();
        if let Err(error) = &result {
            // The caller still holds the mining lock. Fence every publication
            // path before another task can inspect these uncommitted changes.
            self.storage_failure = Some(error.to_string());
            self.parent_epoch += 1;
            self.revision += 1;
        } else {
            self.pending_dirty = false;
        }
        result
    }
}

impl Mining {
    fn verify_restored_boundary(&mut self) -> Result<()> {
        ensure!(
            self.headers.is_empty() == self.height.is_none(),
            "checkpoint height and headers disagree"
        );
        ensure!(
            self.chain.applied.len() <= self.headers.len(),
            "undo journal extends beyond the observed headers"
        );
        // Older checkpoints acquired the active-height index by migration. From
        // either representation, every retained header must identify this branch.
        for (offset, header) in self.headers.iter().rev().enumerate() {
            let h = self
                .height
                .and_then(|tip| tip.checked_sub(offset as u32))
                .context("checkpoint height underflow")?;
            ensure!(
                self.active_since_entry.get(&hash256(header)) == Some(&h),
                "checkpoint active-height index mismatch"
            );
        }
        for (offset, (entry, header)) in self
            .chain
            .applied
            .iter()
            .rev()
            .zip(self.headers.iter().rev())
            .enumerate()
        {
            ensure!(
                self.height.and_then(|tip| tip.checked_sub(offset as u32)) == Some(entry.height),
                "checkpoint undo height mismatch"
            );
            ensure!(
                self.inspect_header(entry.height, &entry.raw)? == *header,
                "checkpoint body on another branch"
            );
            let (txs, err) = self.inspect_body(entry.height, header, &entry.raw)?;
            ensure!(!txs.is_empty() && err.is_none(), "invalid checkpoint body");
            self.lifecycle.restore_subtree(hash256(header), &txs)?;
        }
        if let Some(solution) = &self.solution_body {
            let header = self.inspect_header(solution.height, &solution.raw)?;
            ensure!(
                hash256(&header) == solution.hash,
                "checkpoint solution hash mismatch"
            );
            let (txs, err) = self.inspect_body(solution.height, &header, &solution.raw)?;
            ensure!(err.is_none(), "invalid checkpoint solution body");
            self.lifecycle.restore_subtree(solution.hash, &txs)?;
        }
        Ok(())
    }
}
