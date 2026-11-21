use super::*;
use bsv::primitives::private_key::PrivateKey;
use bsv::script::{templates::p2pkh::P2PKH, ScriptTemplateLock};
use bsv::transaction::{TransactionInput, TransactionOutput};

fn make_block(previous: Hash, height: u32, more: &[Vec<u8>]) -> Vec<u8> {
    // Ordinary coinbase with an untracked output. The txid varies per height.
    let mut cb = hex::decode(
        "01000000010000000000000000000000000000000000000000000000000000000000000000ffffffff",
    )
    .unwrap();
    let h = wire::height_push(height);
    cb.push(h.len() as u8 + 1);
    cb.extend(h);
    cb.push(0);
    cb.extend(hex::decode("ffffffff010000000000000000015100000000").unwrap());
    let ids = std::iter::once(hash256(&cb))
        .chain(more.iter().map(|t| hash256(t)))
        .collect();
    let root = wire::merkle(ids).unwrap().0;
    let mut hdr = [0; 80];
    hdr[..4].copy_from_slice(&0x20000000u32.to_le_bytes());
    hdr[4..36].copy_from_slice(&previous);
    hdr[36..68].copy_from_slice(&root);
    hdr[68..72].copy_from_slice(&(1_600_000_000 + height).to_le_bytes());
    hdr[72..76].copy_from_slice(&0x207fffffu32.to_le_bytes());
    for n in 0u32.. {
        hdr[76..].copy_from_slice(&n.to_le_bytes());
        if wire::meets_target(hash256(&hdr), 0x207fffff).unwrap() {
            break;
        }
    }
    let mut block = hdr.to_vec();
    wire::put_varint(&mut block, 1 + more.len());
    block.extend(cb);
    for t in more {
        block.extend(t);
    }
    block
}

fn tx(from: &str, amount: u64, script: LockingScript) -> Transaction {
    let mut tx = Transaction::new();
    tx.add_input(TransactionInput {
        source_txid: Some(from.to_owned()),
        source_output_index: 0,
        ..Default::default()
    })
    .unwrap();
    tx.add_output(TransactionOutput {
        satoshis: Some(amount),
        locking_script: script,
        change: false,
    })
    .unwrap();
    tx
}

#[tokio::test]
async fn witness_requires_successor_signature_and_unspent_coverage() {
    let signer = P2PKH::from_private_key(PrivateKey::from_hex("abcdef01").unwrap());
    let lock = signer.lock().unwrap();
    let parent = tx(&"12".repeat(32), 100_000, lock.clone());
    let funding = make_block([8; 32], 150, &[parent.to_bytes().unwrap()]);
    let extension = make_block(hash256(&funding[..80]), 151, &[]);
    let mut state = Mining::new(true, lock.to_binary()).unwrap();
    state.observe(150, &funding).unwrap();
    let mut child = tx(&parent.id().unwrap(), 90_000, lock.clone());
    child.sign(0, &signer, 0x41, 100_000, &lock).await.unwrap();
    let bytes = child.to_bytes().unwrap();
    assert!(state
        .submit_tx(&bytes)
        .unwrap_err()
        .to_string()
        .contains("no observed successor"));
    state.observe(151, &extension).unwrap();
    let mut wrong = tx(&parent.id().unwrap(), 88_000, lock.clone());
    wrong.sign(0, &signer, 0x41, 99_999, &lock).await.unwrap();
    assert!(state.submit_tx(&wrong.to_bytes().unwrap()).is_err());
    assert_eq!(state.submit_tx(&bytes).unwrap(), child.id().unwrap());
    assert_eq!(state.submit_tx(&bytes).unwrap(), child.id().unwrap());
    let mined = make_block(hash256(&extension[..80]), 152, std::slice::from_ref(&bytes));
    state.observe(152, &mined).unwrap();
    assert_eq!(state.status()["pending_transactions"], 0);
    assert!(state.submit_tx(&bytes).is_err());
    let mut damaged = make_block(hash256(&mined[..80]), 153, &[]);
    damaged[36] ^= 1;
    assert!(state.observe(153, &damaged).is_err());
    assert!(state.status()["height"].is_null());
    assert_eq!(state.status()["witnessed_outputs"], 0);
}

#[test]
fn higher_tip_retires_work_until_its_complete_body_arrives() {
    let mut state = Mining::new(true, Vec::new()).unwrap();
    let first = make_block([8; 32], 150, &[]);
    state.observe(150, &first).unwrap();
    let revision = state.revision;
    state.announced_height(150);
    assert_eq!(state.revision, revision);
    state.announced_height(152);
    state.announced_height(151);
    assert_eq!(state.revision, revision + 1);
    let next = make_block(hash256(&first[..80]), 151, &[]);
    state.observe(151, &next).unwrap();
    assert_eq!(state.awaiting_height, Some(152));
    let last = make_block(hash256(&next[..80]), 152, &[]);
    state.observe(152, &last).unwrap();
    assert_eq!(state.awaiting_height, None);
}

#[test]
fn framing_rejects_unbounded_and_nonminimal_lengths() {
    for data in [
        "01000000ffffffffffffffffff",
        "01000000fd0100",
        "0100000000",
        "0100000001",
    ] {
        let compact = hex::decode(data).unwrap();
        assert!(wire::transaction(&mut &compact[..], |_| {}).is_err());
    }
    assert!(!wire::meets_target([255; 32], 0x207fffff).unwrap());
    assert!(wire::meets_target([0; 32], 0x207fffff).unwrap());
    assert!(wire::as_le_target(0x23010000).is_err());
    assert!(wire::as_le_target(0x00000011).is_err());
}
