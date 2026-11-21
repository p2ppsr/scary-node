//! Complete, immutable wire bodies. Large payloads use anonymous files
//! on the node volume, shared by validation and the short reorg journal.
use anyhow::{ensure, Context, Result};
use memmap2::{Mmap, MmapOptions};
use serde::{
    de::{Error, SeqAccess, Visitor},
    ser::SerializeSeq,
    Deserialize, Deserializer, Serialize, Serializer,
};
use std::{
    fmt,
    fs::File,
    io::{BufWriter, Write},
    ops::Deref,
    path::{Path, PathBuf},
    sync::{Arc, OnceLock},
};

const RAM_BYTES: usize = 1 << 20;
static DIRECTORY: OnceLock<PathBuf> = OnceLock::new();
pub fn configure(directory: &Path) {
    let _ = DIRECTORY.set(directory.to_path_buf());
}

#[derive(Clone, Debug)]
pub enum BlockBody {
    Memory(Arc<Vec<u8>>),
    File(Arc<Mmap>),
}
impl Deref for BlockBody {
    type Target = [u8];
    fn deref(&self) -> &[u8] {
        match self {
            Self::Memory(v) => v,
            Self::File(v) => v,
        }
    }
}

pub struct Spool {
    ram: Vec<u8>,
    file: Option<BufWriter<File>>,
    len: usize,
    limit: usize,
}
impl Spool {
    pub fn new(limit: usize) -> Self {
        Self {
            ram: Vec::new(),
            file: None,
            len: 0,
            limit,
        }
    }
    pub fn append(&mut self, bytes: &[u8]) -> Result<()> {
        ensure!(
            bytes.len() <= self.limit - self.len,
            "body exceeds configured capacity"
        );
        let new_len = self.len + bytes.len();
        if new_len > RAM_BYTES && self.file.is_none() {
            let file = match DIRECTORY.get() {
                Some(dir) => tempfile::tempfile_in(dir),
                None => tempfile::tempfile(),
            }
            .context("opening body spool on node volume")?;
            let mut buffered = BufWriter::with_capacity(256 << 10, file);
            buffered.write_all(&self.ram)?;
            self.ram = Vec::new();
            self.file = Some(buffered);
        }
        if let Some(file) = &mut self.file {
            file.write_all(bytes)?;
        } else {
            self.ram.extend_from_slice(bytes);
        }
        self.len = new_len;
        Ok(())
    }
    pub fn finish(self) -> Result<BlockBody> {
        if let Some(mut writer) = self.file {
            writer.flush()?;
            let file = writer.into_inner().map_err(|e| e.into_error())?;
            ensure!(
                file.metadata()?.len() == self.len as u64,
                "incomplete body spool"
            );
            // Only this process has the anonymous file. The sole writable handle
            // is dropped here. No caller can mutate or truncate the mapped bytes.
            let map = unsafe { MmapOptions::new().map(&file) }?;
            Ok(BlockBody::File(Arc::new(map)))
        } else {
            Ok(BlockBody::Memory(Arc::new(self.ram)))
        }
    }
}
impl Serialize for BlockBody {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        if self.len() <= u32::MAX as usize {
            return s.serialize_bytes(self);
        }
        let chunks = self.chunks(RAM_BYTES);
        let mut seq = s.serialize_seq(Some(chunks.len()))?;
        for c in chunks {
            seq.serialize_element(serde_bytes::Bytes::new(c))?;
        }
        seq.end()
    }
}
struct ReadBody;
impl<'de> Visitor<'de> for ReadBody {
    type Value = BlockBody;
    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("complete block bytes or a sequence of block chunks")
    }
    fn visit_bytes<E: Error>(self, b: &[u8]) -> Result<BlockBody, E> {
        let mut spool = Spool::new(crate::mining::body_limit());
        spool.append(b).map_err(E::custom)?;
        spool.finish().map_err(E::custom)
    }
    fn visit_byte_buf<E: Error>(self, b: Vec<u8>) -> Result<BlockBody, E> {
        if b.len() <= RAM_BYTES && b.len() <= crate::mining::body_limit() {
            return Ok(BlockBody::Memory(Arc::new(b)));
        }
        self.visit_bytes(&b)
    }
    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<BlockBody, A::Error> {
        let mut spool = Spool::new(crate::mining::body_limit());
        while let Some(part) = seq.next_element::<serde_bytes::ByteBuf>()? {
            spool.append(&part).map_err(A::Error::custom)?;
        }
        spool.finish().map_err(A::Error::custom)
    }
}
impl<'de> Deserialize<'de> for BlockBody {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        d.deserialize_any(ReadBody)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn spill_preserves_exact_bytes_and_restores_old_checkpoint_encoding() {
        let part = vec![0x51; 64 << 10];
        let mut s = Spool::new(RAM_BYTES + part.len());
        for _ in 0..=RAM_BYTES / part.len() {
            s.append(&part).unwrap();
        }
        assert!(s.append(&[1]).is_err());
        let b = s.finish().unwrap();
        assert!(matches!(b, BlockBody::File(_)));
        let encoded = rmp_serde::to_vec(serde_bytes::Bytes::new(&b)).unwrap();
        let restored: BlockBody = rmp_serde::from_slice(&encoded).unwrap();
        assert_eq!(&*b, &*restored);
        assert!(matches!(restored, BlockBody::File(_)));
        assert_eq!(rmp_serde::to_vec(&b).unwrap(), encoded);
    }
}
