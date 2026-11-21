//! Differential runner for the exact upstream Rust binding.
use anyhow::{Context, Result};
use rust_bdk::TxValidator;
use serde_json::json;

fn main() -> Result<()> {
    let mut args = std::env::args().skip(1);
    let raw = hex::decode(args.next().context("missing extended transaction")?)?;
    let source: Vec<i32> = args
        .next()
        .context("missing source height")?
        .split(',')
        .map(str::parse)
        .collect::<std::result::Result<_, _>>()?;
    let tip = args.next().context("missing tip height")?.parse()?;
    let block = args.next().context("missing block height")?.parse()?;
    let v = TxValidator::new("regtest").context("initialize BDK")?;
    v.set_genesis_activation_height(1)
        .map_err(anyhow::Error::msg)?;
    v.set_chronicle_activation_height(2)
        .map_err(anyhow::Error::msg)?;
    v.set_require_standard(false);
    v.set_accept_non_standard_output(true);
    let policy = v.validate_transaction(&raw, &source, tip, false);
    let consensus = v.validate_transaction(&raw, &source, block, true);
    println!(
        "{}",
        json!({"policy":policy.is_ok(),"consensus":consensus.is_ok(),
              "policy_error":policy.err().map(|e|e.to_string()),"consensus_error":consensus.err().map(|e|e.to_string()),
              "sv_source":rust_bdk::bsv_git_commit_hash(),"bdk_version":rust_bdk::bdk_version_string()})
    );
    Ok(())
}
