//! A single active Stratum allocator per network and public mining identity.
use anyhow::{ensure, Context, Result};
use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
};

pub struct MiningLease {
    _lock: File,
    directory: PathBuf,
    state: PathBuf,
    domain: [u8; 32],
    watermark: u32,
}

fn durable_replace(directory: &Path, name: &str, data: &[u8]) -> Result<()> {
    let mut pending = tempfile::NamedTempFile::new_in(directory)?;
    pending.write_all(data)?;
    pending.as_file().sync_all()?;
    pending.persist(directory.join(name))?;
    File::open(directory)?.sync_all()?;
    Ok(())
}

fn read_fixed<const N: usize>(path: &Path) -> Result<Option<[u8; N]>> {
    match File::open(path) {
        Ok(mut file) => {
            ensure!(
                file.metadata()?.len() == N as u64,
                "invalid mining lease record"
            );
            let mut data = [0; N];
            file.read_exact(&mut data)?;
            Ok(Some(data))
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(e.into()),
    }
}

fn default_root() -> Result<PathBuf> {
    let base = std::env::var_os("XDG_STATE_HOME")
        .map(PathBuf::from)
        .filter(|p| p.is_absolute())
        .or_else(|| std::env::var_os("HOME").map(|p| PathBuf::from(p).join(".local/state")))
        .context("configure --mining-lease-dir or a home directory")?;
    let root = base.join("scary-node/mining-leases");
    fs::create_dir_all(&root)?;
    Ok(root)
}

impl MiningLease {
    pub fn acquire(
        root: Option<&Path>,
        network: &str,
        identity: &[u8; 32],
        state: &Path,
    ) -> Result<Self> {
        let root = match root {
            Some(path) => path.to_owned(),
            None => default_root()?,
        };
        // An explicitly configured mount must already exist at startup.
        let root_fd = File::open(&root).context("open mining lease root")?;
        let directory = root.join(format!("{network}-{}", hex::encode(identity)));
        match fs::create_dir(&directory) {
            Ok(()) => root_fd.sync_all()?,
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => (),
            Err(e) => return Err(e.into()),
        }
        let lock = OpenOptions::new()
            .create(true)
            .truncate(false)
            .write(true)
            .open(directory.join("active.lock"))?;
        lock.try_lock()
            .context("mining identity already active or lease unavailable")?;

        // Bind restored checkpoints to the same authority. A newly mounted empty
        // filesystem must never quietly grant work to a previously bound miner.
        let binding = format!("mining-domain-{}", hex::encode(identity));
        let local = read_fixed::<32>(&state.join(&binding))?;
        let record = read_fixed::<36>(&directory.join("allocation"))?;
        let (domain, shared) = match record {
            Some(bytes) => (
                <[u8; 32]>::try_from(&bytes[..32])?,
                u32::from_le_bytes(bytes[32..].try_into()?),
            ),
            None => {
                ensure!(local.is_none(), "mining lease domain changed or missing");
                let key = libp2p::identity::Keypair::generate_ed25519();
                (
                    bsv::primitives::hash::hash256(&key.public().encode_protobuf()),
                    0,
                )
            }
        };
        ensure!(
            local.is_none_or(|expected| expected == domain),
            "mining lease domain changed or missing"
        );
        let legacy = read_fixed::<4>(&state.join("extranonce.watermark"))?;
        let watermark = shared.max(u32::from_le_bytes(legacy.unwrap_or_default()));
        let lease = Self {
            _lock: lock,
            directory,
            state: state.to_owned(),
            domain,
            watermark,
        };
        lease.write_allocation(watermark)?;
        if local.is_none() {
            durable_replace(state, &binding, &domain)?;
        }
        Ok(lease)
    }

    fn write_allocation(&self, end: u32) -> Result<()> {
        let mut record = [0; 36];
        record[..32].copy_from_slice(&self.domain);
        record[32..].copy_from_slice(&end.to_le_bytes());
        durable_replace(&self.directory, "allocation", &record)
    }

    pub fn reserve(&mut self) -> Result<(u32, u32)> {
        let start = self.watermark;
        ensure!(start < u32::MAX, "extranonce space exhausted");
        let end = start.saturating_add(1024);
        self.write_allocation(end)?;
        // Burn the shared reservation even if the compatibility mirror fails.
        self.watermark = end;
        durable_replace(&self.state, "extranonce.watermark", &end.to_le_bytes())?;
        Ok((start, end))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reservations_survive_stale_restore_and_reject_switched_domain() {
        let primary = tempfile::tempdir().unwrap();
        let standby = tempfile::tempdir().unwrap();
        let root = tempfile::tempdir().unwrap();
        let different = tempfile::tempdir().unwrap();
        let id = [21; 32];
        let acquire =
            |base: &Path, state: &Path| MiningLease::acquire(Some(base), "regtest", &id, state);
        fs::write(
            primary.path().join("extranonce.watermark"),
            73u32.to_le_bytes(),
        )
        .unwrap();
        let mut active = acquire(root.path(), primary.path()).unwrap();
        let binding = format!("mining-domain-{}", hex::encode(id));
        fs::copy(primary.path().join(&binding), standby.path().join(binding)).unwrap();
        assert!(acquire(root.path(), standby.path()).is_err());
        assert_eq!(active.reserve().unwrap(), (73, 1097));
        drop(active);
        assert!(acquire(different.path(), standby.path()).is_err());
        let mut restored = acquire(root.path(), standby.path()).unwrap();
        assert_eq!(restored.reserve().unwrap(), (1097, 2121));
        // Exhausted space or a corrupt authoritative record must withhold a lease.
        fs::write(
            standby.path().join("extranonce.watermark"),
            u32::MAX.to_le_bytes(),
        )
        .unwrap();
        drop(restored);
        let mut last = acquire(root.path(), standby.path()).unwrap();
        assert!(last.reserve().is_err());
        fs::write(last.directory.join("allocation"), [0; 2]).unwrap();
        drop(last);
        assert!(acquire(root.path(), primary.path()).is_err());
    }
}
