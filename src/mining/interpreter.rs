//! Shared SV Node interpreter. Previous outputs supplied to this module were
//! witnessed on the selected branch. Upstream constructors set process-wide
//! configuration, so no constructor may run concurrently with validation.
use super::{wire, Coin};
use anyhow::{ensure, Context, Result};
use rust_bdk::{TxError, TxValidator};
use std::cell::RefCell;
use std::ffi::c_void;
use std::ptr::NonNull;
use std::sync::{OnceLock, RwLock};

const BDK: &str = "8a4e1d3ddb16cbb2b3a98f82ea1ee6ff19a297e9";
const SV: &str = "6504a3aff65ba97c0f6c80962b033e35ecbfed4b";
const BUILD: &str = include_str!("../../.native/provenance.json");
static CONFIG: RwLock<()> = RwLock::new(());
static NETWORK: OnceLock<bool> = OnceLock::new();
thread_local! {static INSTANCE: RefCell<Option<Interpreter>> = const {RefCell::new(None)};}

unsafe extern "C" {
    fn scary_spend_create(regtest: bool) -> *mut c_void;
    fn scary_spend_destroy(handle: *mut c_void);
    fn scary_verify_spend(
        handle: *mut c_void,
        tx: *const u8,
        size: usize,
        index: u32,
        script: *const u8,
        script_size: usize,
        value: i64,
        created: i32,
        height: i32,
    ) -> i32;
}

struct Interpreter {
    full: TxValidator,
    spend: NonNull<c_void>,
}
impl Drop for Interpreter {
    fn drop(&mut self) {
        // Handle is exclusively owned for its entire lifetime on this thread.
        unsafe { scary_spend_destroy(self.spend.as_ptr()) };
    }
}

pub(super) fn build() -> serde_json::Value {
    let mut manifest: serde_json::Value = serde_json::from_str(BUILD).expect("build manifest");
    manifest["linked_bdk_version"] = rust_bdk::bdk_version_string().into();
    manifest["linked_sv_version"] = rust_bdk::bsv_version_string().into();
    manifest["linked_bdk_commit"] = rust_bdk::source_git_commit_hash().into();
    manifest["linked_sv_commit"] = rust_bdk::bsv_git_commit_hash().into();
    manifest
}

fn with<T>(regtest: bool, call: impl FnOnce(&Interpreter) -> Result<T>) -> Result<T> {
    ensure!(
        *NETWORK.get_or_init(|| regtest) == regtest,
        "BDK network already selected"
    );
    INSTANCE.with(|slot| {
        if slot.borrow().is_none() {
            let _writer = CONFIG
                .write()
                .map_err(|_| anyhow::anyhow!("BDK configuration unavailable"))?;
            for (actual, expected) in [
                (rust_bdk::source_git_commit_hash(), BDK),
                (rust_bdk::bsv_git_commit_hash(), SV),
            ] {
                ensure!(
                    actual.len() >= 7 && expected.starts_with(actual),
                    "linked BDK provenance differs from the release pin"
                );
            }
            let full = TxValidator::new(if regtest { "regtest" } else { "main" })
                .context("BDK initialization failed")?;
            if regtest {
                full.set_genesis_activation_height(1)
                    .map_err(anyhow::Error::msg)?;
                full.set_chronicle_activation_height(2)
                    .map_err(anyhow::Error::msg)?;
                full.set_require_standard(false);
                full.set_accept_non_standard_output(true);
            }
            // Constructor uses the same network and activation settings as full.
            let spend = NonNull::new(unsafe { scary_spend_create(regtest) })
                .context("BDK spend initialization failed")?;
            *slot.borrow_mut() = Some(Interpreter { full, spend });
        }
        let _reader = CONFIG
            .read()
            .map_err(|_| anyhow::anyhow!("BDK configuration unavailable"))?;
        call(slot.borrow().as_ref().expect("initialized"))
    })
}

pub(super) fn activation(regtest: bool) -> u32 {
    if regtest {
        2
    } else {
        943816
    }
}

/// Copy canonical transaction bytes, inserting the BIP239 marker and each
/// ordered previous output after its corresponding complete serialized input.
pub(super) fn extended(raw: &[u8], coins: &[&Coin]) -> Result<(Vec<u8>, Vec<i32>)> {
    let mut reader = raw;
    let mut out = wire::take(&mut reader, 4)?.to_vec();
    out.extend([0, 0, 0, 0, 0, 0xef]);
    let n = wire::varint(&mut reader)?;
    ensure!(n == coins.len(), "incomplete BDK prevouts");
    wire::put_varint(&mut out, n);
    let mut heights = Vec::with_capacity(n);
    for coin in coins {
        let begin = reader;
        wire::take(&mut reader, 36)?;
        let script_size = wire::varint(&mut reader)?;
        wire::take(&mut reader, script_size)?;
        wire::take(&mut reader, 4)?;
        out.extend_from_slice(&begin[..begin.len() - reader.len()]);
        out.extend(coin.value.to_le_bytes());
        wire::put_varint(&mut out, coin.script.len());
        out.extend_from_slice(&coin.script);
        heights.push(coin.height.try_into()?);
    }
    out.extend_from_slice(reader);
    Ok((out, heights))
}

pub(super) fn transaction(
    regtest: bool,
    ef: &[u8],
    heights: &[i32],
    height: u32,
    consensus: bool,
) -> Result<std::result::Result<(), TxError>> {
    with(regtest, |engine| {
        let r = engine
            .full
            .validate_transaction(ef, heights, height.try_into()?, consensus);
        ensure!(
            !matches!(r, Err(TxError::Exception | TxError::Cancelled)),
            "BDK did not complete validation"
        );
        Ok(r)
    })
}
pub(super) fn spend(
    regtest: bool,
    raw: &[u8],
    index: usize,
    coin: &Coin,
    height: u32,
) -> Result<bool> {
    with(regtest, |v| {
        // Both slices and the exclusively thread-owned handle stay live for the
        // synchronous call. The shim reports local failures separately.
        let domain = unsafe {
            scary_verify_spend(
                v.spend.as_ptr(),
                raw.as_ptr(),
                raw.len(),
                index.try_into()?,
                coin.script.as_ptr(),
                coin.script.len(),
                coin.value.try_into()?,
                coin.height.try_into()?,
                height.try_into()?,
            )
        };
        match domain {
            0 => Ok(true),
            1 | 2 => Ok(false),
            _ => anyhow::bail!("BDK single-input validation unavailable"),
        }
    })
}
