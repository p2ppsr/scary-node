use super::*;
use bsv::primitives::private_key::PrivateKey;
use bsv::script::{templates::p2pkh::P2PKH, LockingScript, ScriptTemplateLock};
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
    let mut damaged = make_block(hash256(&mined[..80]), 153, std::slice::from_ref(&bytes));
    *damaged.last_mut().unwrap() ^= 1;
    assert!(state.observe(153, &damaged).is_err());
    assert_eq!(state.status()["height"], 152);
    assert!(!state.observation_window_ready());
    assert!(state.outpoint(&child.id().unwrap(), 0).unwrap()["known"] == true);
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

#[tokio::test]
async fn mixed_and_same_block_payments_preserve_individual_check_results() {
    let signer = P2PKH::from_private_key(PrivateKey::from_hex("abcdef03").unwrap());
    let lock = signer.lock().unwrap();
    let funding = tx(&"14".repeat(32), 100_000, lock.clone());
    let mut mixed = tx(&funding.id().unwrap(), 130_000, lock.clone());
    mixed
        .add_input(TransactionInput {
            source_txid: Some("36".repeat(32)),
            source_output_index: 0,
            ..Default::default()
        })
        .unwrap();
    mixed.sign(0, &signer, 0x41, 100_000, &lock).await.unwrap();
    mixed.sign(1, &signer, 0x41, 31_000, &lock).await.unwrap();
    let mut next = tx(&mixed.id().unwrap(), 120_000, lock.clone());
    next.sign(0, &signer, 0x41, 130_000, &lock).await.unwrap();
    let first = make_block([0x12; 32], 155, &[funding.to_bytes().unwrap()]);
    let mut m = Mining::new(true, vec![]).unwrap();
    m.observe(155, &first).unwrap();
    let assessment = m.check_transaction(&mixed.to_bytes().unwrap()).unwrap();
    assert_eq!(assessment["outcome"], "deferred");
    assert_eq!(assessment["script_checks_passed"], 1);
    assert_eq!(assessment["amount_checks_deferred"], 1);
    let second = make_block(
        hash256(&first[..80]),
        156,
        &[mixed.to_bytes().unwrap(), next.to_bytes().unwrap()],
    );
    m.observe(156, &second).unwrap();
    let row = &m.status()["validation_window"][1];
    assert_eq!(row["unknown_inputs"], 1);
    assert_eq!(row["known_inputs"], 2);
    assert_eq!(row["script_checks_passed"], 2);
    assert_eq!(row["same_block_inputs"], 1);
    assert_eq!(row["transactions_determinate"], 1);
    assert_eq!(row["transactions_deferred"], 1);
    assert_eq!(row["coinbase_reward_checked"], false);
    assert_eq!(
        m.outpoint(&mixed.id().unwrap(), 0).unwrap()["consumed"],
        true
    );
    assert_eq!(
        m.check_transaction(&next.to_bytes().unwrap()).unwrap()["outcome"],
        "invalid"
    );
    m.suspend();
    assert_eq!(
        m.outpoint(&mixed.id().unwrap(), 0).unwrap()["consumed"],
        false
    );
}

#[tokio::test]
async fn rejected_known_signature_retires_jobs_and_cannot_be_reanchored() {
    let signer = P2PKH::from_private_key(PrivateKey::from_hex("abcdef04").unwrap());
    let lock = signer.lock().unwrap();
    let funding = tx(&"14".repeat(32), 100_000, lock.clone());
    let first = make_block([0x12; 32], 155, &[funding.to_bytes().unwrap()]);
    let mut m = Mining::new(true, vec![]).unwrap();
    m.observe(155, &first).unwrap();
    let mut payment = tx(&funding.id().unwrap(), 90_000, lock.clone());
    payment
        .sign(0, &signer, 0x41, 100_000, &lock)
        .await
        .unwrap();
    let valid = payment.to_bytes().unwrap();
    assert_eq!(
        m.check_transaction(&valid).unwrap()["outcome"],
        "determinate"
    );
    let mut previous_amount = tx(&funding.id().unwrap(), 90_000, lock.clone());
    previous_amount
        .sign(0, &signer, 0x41, 100_001, &lock)
        .await
        .unwrap();
    let different = previous_amount.to_bytes().unwrap();
    let assessment = m.check_transaction(&different).unwrap();
    assert_eq!(assessment["outcome"], "invalid");
    assert_eq!(assessment["script_checks_failed"], 1);
    assert_eq!(assessment["script_checks_deferred"], 0);
    assert!(m.height.is_some());
    let invalid = make_block(hash256(&first[..80]), 156, &[different]);
    assert!(m.observe(156, &invalid).is_err());
    assert_eq!(m.height, Some(155));
    assert!(!m.observation_window_ready());
    assert_eq!(m.validation_window.len(), 1);
    assert!(m.observe(156, &invalid).is_err());
    assert!(m.observe(400, &invalid).is_err());
    let descendant = make_block(hash256(&invalid[..80]), 157, &[]);
    assert!(m.observe(157, &descendant).is_err());
    let grandchild = make_block(hash256(&descendant[..80]), 158, &[]);
    assert!(m.observe(158, &grandchild).is_err());
    m.observe(155, &first).unwrap();
    let correct = make_block(hash256(&first[..80]), 156, std::slice::from_ref(&valid));
    m.observe(156, &correct).unwrap();
    let repeated = m.check_transaction(&valid).unwrap();
    assert_eq!(repeated["outcome"], "invalid");
    assert_eq!(repeated["consumed_inputs"], 1);
    assert_eq!(repeated["reason"], "witnessed outpoint already consumed");
}

#[test]
fn mining_observation_window_policy_resets_with_coverage() {
    let mut m = Mining::new(true, vec![]).unwrap();
    assert!(m.set_min_observed_blocks(0).is_err());
    assert!(m.set_min_observed_blocks(148).is_err());
    m.set_min_observed_blocks(3).unwrap();
    let mut previous = [0x34; 32];
    for (height, ready) in [(10, false), (11, false), (12, true)] {
        let body = make_block(previous, height, &[]);
        m.observe(height, &body).unwrap();
        previous = hash256(&body[..80]);
        assert_eq!(m.status()["observation_window_ready"], ready);
    }
    m.announced_height(13);
    assert!(!m.observation_window_ready());
    m.suspend();
    assert!(!m.observation_window_ready());
    assert_eq!(m.status()["min_observed_blocks"], 3);
    assert_eq!(m.status()["validation_window"], serde_json::json!([]));
}

#[tokio::test]
async fn incorrect_delivery_can_be_retried_without_losing_the_validation_prefix() {
    let signer = P2PKH::from_private_key(PrivateKey::from_hex("abcdef05").unwrap());
    let lock = signer.lock().unwrap();
    let funding = tx(&"14".repeat(32), 100_000, lock.clone());
    let mut payment = tx(&funding.id().unwrap(), 90_000, lock.clone());
    payment
        .sign(0, &signer, 0x41, 100_000, &lock)
        .await
        .unwrap();
    let first = make_block([0x12; 32], 155, &[funding.to_bytes().unwrap()]);
    let next = make_block(hash256(&first[..80]), 156, &[payment.to_bytes().unwrap()]);
    let mut m = Mining::new(true, vec![]).unwrap();
    let earlier = make_block([7; 32], 154, &[]);
    m.observe(154, &earlier).unwrap();
    let funding_bytes = funding.to_bytes().unwrap();
    assert_eq!(
        m.check_transaction(&funding_bytes).unwrap()["outcome"],
        "deferred"
    );
    m.suspend();
    m.observe(155, &first).unwrap();
    assert_eq!(
        m.check_transaction(&funding_bytes).unwrap()["outcome"],
        "invalid"
    );
    let mut alternate_body = next.clone();
    *alternate_body.last_mut().unwrap() ^= 1;
    assert!(m
        .observe(156, &alternate_body)
        .unwrap_err()
        .to_string()
        .contains("merkle root mismatch"));
    assert_eq!(m.status()["recent_invalid_blocks"], 0);
    assert_eq!(m.status()["retired_prefix_height"], 155);
    assert!(!m.observation_window_ready());
    m.announced_height(157);
    m.observe(156, &next).unwrap();
    assert_eq!(m.awaiting_height, Some(157));
    assert!(!m.observation_window_ready());
    let row = &m.status()["validation_window"][1];
    assert_eq!(row["known_inputs"], 1);
    assert_eq!(row["script_checks_passed"], 1);
    assert_eq!(row["transactions_determinate"], 1);
}

#[tokio::test]
async fn witnessed_amount_violation_rolls_back_prior_transactions_in_that_body() {
    let signer = P2PKH::from_private_key(PrivateKey::from_hex("abcdef06").unwrap());
    let lock = signer.lock().unwrap();
    let funding = tx(&"14".repeat(32), 100_000, lock.clone());
    let first = make_block([6; 32], 155, &[funding.to_bytes().unwrap()]);
    let mut paid = tx(&funding.id().unwrap(), 98_000, lock.clone());
    paid.sign(0, &signer, 0x41, 100_000, &lock).await.unwrap();
    let mut unbalanced = tx(&paid.id().unwrap(), 98_001, lock.clone());
    unbalanced
        .sign(0, &signer, 0x41, 98_000, &lock)
        .await
        .unwrap();
    let paid_bytes = paid.to_bytes().unwrap();
    let mut m = Mining::new(true, vec![]).unwrap();
    m.observe(155, &first).unwrap();
    let bad = make_block(
        hash256(&first[..80]),
        156,
        &[paid_bytes.clone(), unbalanced.to_bytes().unwrap()],
    );
    assert!(m
        .observe(156, &bad)
        .unwrap_err()
        .to_string()
        .contains("creates money"));
    let correct = make_block(
        hash256(&first[..80]),
        156,
        std::slice::from_ref(&paid_bytes),
    );
    m.observe(156, &correct).unwrap();
    let row = &m.status()["validation_window"][1];
    assert_eq!(row["known_inputs"], 1);
    assert_eq!(row["script_checks_passed"], 1);
    let disputed = m
        .check_transaction(&unbalanced.to_bytes().unwrap())
        .unwrap();
    assert_eq!(disputed["outcome"], "invalid");
    assert_eq!(disputed["script_checks_passed"], 1);
    assert_eq!(disputed["amount_checks_failed"], 1);
    assert_eq!(m.outpoint(&paid.id().unwrap(), 0).unwrap()["known"], true);
}

#[test]
fn bounded_consumption_evidence_expires_and_recreated_outpoint_survives_old_eviction() {
    let mut history = validation::Spent::default();
    let p = ([7; 32], 1);
    history.record(p);
    history.forget(&p);
    history.record(p);
    for i in 2..=MAX_WATCHED as u32 {
        history.record(([8; 32], i));
    }
    assert!(history.has(&p));
    history.record(([9; 32], 0));
    assert!(!history.has(&p));
}

#[tokio::test]
async fn matched_parent_read_only_amount_check_depends_on_witnessed_source() {
    let signer = P2PKH::from_private_key(PrivateKey::from_hex("abcdef07").unwrap());
    let lock = signer.lock().unwrap();
    let funding = tx(&"17".repeat(32), 100_000, lock.clone());
    let funding_id = funding.id().unwrap();
    let mut mature = Mining::new(true, vec![]).unwrap();
    let mut cold = Mining::new(true, vec![]).unwrap();

    let mut parent = make_block([0x18; 32], 155, &[funding.to_bytes().unwrap()]);
    mature.observe(155, &parent).unwrap();
    for height in 156..=167 {
        parent = make_block(hash256(&parent[..80]), height, &[]);
        mature.observe(height, &parent).unwrap();
    }
    cold.observe(167, &parent).unwrap();

    let mature_before = mature.status();
    let cold_before = cold.status();
    assert_eq!(mature_before["tip_hash"], cold_before["tip_hash"]);
    assert_eq!(mature_before["height"], 167);
    assert_eq!(cold_before["height"], 167);
    assert_eq!(mature_before["headers"], 13);
    assert_eq!(cold_before["headers"], 1);
    assert_eq!(
        mature.outpoint(&funding_id, 0).unwrap()["value_satoshis"],
        100_000
    );
    assert_eq!(cold.outpoint(&funding_id, 0).unwrap()["known"], false);

    // Both local checkers see the very same signature, which commits to the
    // actual input amount. Only an observer that saw the source can balance it.
    let mut payment = tx(&funding_id, 100_001, lock.clone());
    payment
        .sign(0, &signer, 0x41, 100_000, &lock)
        .await
        .unwrap();
    let raw = payment.to_bytes().unwrap();
    let early = cold.check_transaction(&raw).unwrap();
    assert_eq!(early["outcome"], "deferred");
    assert_eq!(early["unknown_inputs"], 1);
    assert_eq!(early["script_checks_passed"], 0);
    assert_eq!(early["amount_checks_deferred"], 1);
    let late = mature.check_transaction(&raw).unwrap();
    assert_eq!(late["outcome"], "invalid");
    assert_eq!(late["reason"], "transaction creates money");
    assert_eq!(late["known_inputs"], 1);
    assert_eq!(late["script_checks_passed"], 1);
    assert_eq!(late["amount_checks_failed"], 1);
    assert_eq!(mature.check_transaction(&raw).unwrap(), late);
    assert_eq!(cold.status(), cold_before);
    assert_eq!(mature.status(), mature_before);
    assert_eq!(mature.outpoint(&funding_id, 0).unwrap()["eligible"], true);
}

#[tokio::test]
async fn honest_reorg_restores_witnessed_inputs_and_replaces_settlement_ancestry() {
    let signer = P2PKH::from_private_key(PrivateKey::from_hex("abcdef08").unwrap());
    let lock = signer.lock().unwrap();
    let funded = tx(&"28".repeat(32), 100_000, lock.clone());
    let first = make_block([3; 32], 155, &[funded.to_bytes().unwrap()]);
    let anchor = hash256(&first[..80]);
    let mut spend = tx(&funded.id().unwrap(), 98_000, lock.clone());
    spend.sign(0, &signer, 0x41, 100_000, &lock).await.unwrap();
    let spend_bytes = spend.to_bytes().unwrap();
    let a = make_block(anchor, 156, std::slice::from_ref(&spend_bytes));
    let a_id = hash256(&a[..80]);
    let b = make_block(anchor, 156, &[]);
    let b2 = make_block(hash256(&b[..80]), 157, &[]);
    let b3 = make_block(hash256(&b2[..80]), 158, &[]);

    // Rotate the order in which an honest replacement or its child arrives.
    for child_first in [false, true] {
        let mut m = Mining::new(true, lock.to_binary()).unwrap();
        m.observe(155, &first).unwrap();
        m.lifecycle.queue(a_id, anchor, 156, "first-branch".into());
        m.observe(156, &a).unwrap();
        assert_eq!(
            m.outpoint(&funded.id().unwrap(), 0).unwrap()["consumed"],
            true
        );
        assert_eq!(m.outpoint(&spend.id().unwrap(), 0).unwrap()["known"], true);
        if child_first {
            m.observe(157, &b2).unwrap();
            assert_eq!(m.missing_recent(), vec![(hash256(&b[..80]), 156)]);
            assert_eq!(m.tip(), Some(a_id));
            assert!(!m.observation_window_ready());
            m.observe(156, &b).unwrap();
        } else {
            m.observe(156, &b).unwrap();
            assert_eq!(m.tip(), Some(a_id));
            m.observe(157, &b2).unwrap();
        }
        assert!(m.missing_recent().is_empty());
        assert_eq!(m.chain.reorgs, 1);
        assert_eq!(m.headers.len(), 3);
        assert!(m.observation_window_ready());
        assert_eq!(
            m.outpoint(&funded.id().unwrap(), 0).unwrap()["eligible"],
            true
        );
        assert_eq!(
            m.outpoint(&funded.id().unwrap(), 0).unwrap()["consumed"],
            false
        );
        assert_eq!(m.outpoint(&spend.id().unwrap(), 0).unwrap()["known"], false);
        assert_eq!(m.submit_tx(&spend_bytes).unwrap(), spend.id().unwrap());
        m.observe(158, &b3).unwrap();
        assert_eq!(m.status()["solutions"][0]["chain_state"], "stale_at_depth");
    }
}

#[test]
fn reorganizes_a_full_difficulty_window_without_losing_older_headers() {
    let mut state = Mining::new(true, vec![]).unwrap();
    let mut parent = [1; 32];
    for height in 1..=170 {
        let raw = make_block(parent, height, &[]);
        parent = hash256(&raw[..80]);
        state.observe(height, &raw).unwrap();
    }
    let base = parent;
    let mut left = make_block(base, 171, &[]);
    state.observe(171, &left).unwrap();
    // A second ordinary coinbase destination produces a different branch.
    let alt = make_block(
        base,
        171,
        &[tx(
            &"39".repeat(32),
            1000,
            P2PKH::from_private_key(PrivateKey::from_hex("abcdef09").unwrap())
                .lock()
                .unwrap(),
        )
        .to_bytes()
        .unwrap()],
    );
    for height in 172..=178 {
        left = make_block(hash256(&left[..80]), height, &[]);
        state.observe(height, &left).unwrap();
    }
    let mut right = alt;
    state.observe(171, &right).unwrap();
    for height in 172..=179 {
        right = make_block(hash256(&right[..80]), height, &[]);
        state.observe(height, &right).unwrap();
    }
    assert_eq!(state.tip(), Some(hash256(&right[..80])));
    assert_eq!(state.chain.reorgs, 1);
    assert_eq!(state.validation_window.len(), HEADER_WINDOW);
    assert_eq!(state.headers.len(), HEADER_WINDOW);
}

#[test]
fn branch_replay_across_depth_and_delivery_orders() {
    for trial in 0..256u32 {
        let start = 200 + trial * 16;
        let depth = 1 + trial % 8;
        let mut miner = Mining::new(true, vec![]).unwrap();
        let anchor = make_block([0x35; 32], start, &[]);
        miner.observe(start, &anchor).unwrap();
        let parent = hash256(&anchor[..80]);
        let mut left = parent;
        for height in start + 1..=start + depth {
            let body = make_block(left, height, &[]);
            left = hash256(&body[..80]);
            miner.observe(height, &body).unwrap();
        }
        let other_payment = tx(
            &"41".repeat(32),
            1000,
            P2PKH::from_private_key(PrivateKey::from_hex("abcdef0a").unwrap())
                .lock()
                .unwrap(),
        )
        .to_bytes()
        .unwrap();
        let body = make_block(parent, start + 1, &[other_payment]);
        let mut right = hash256(&body[..80]);
        let mut alternate = vec![(start + 1, body)];
        for height in start + 2..=start + depth + 1 {
            let body = make_block(right, height, &[]);
            right = hash256(&body[..80]);
            alternate.push((height, body));
        }
        if (trial / 8) % 2 == 0 {
            alternate.reverse();
        }
        for (height, body) in alternate {
            miner.observe(height, &body).unwrap();
        }
        assert_eq!(miner.tip(), Some(right));
        assert_eq!(miner.chain.reorgs, 1);
        assert!(miner.observation_window_ready());
        assert_eq!(miner.headers.len(), depth as usize + 2);
    }
}

#[test]
fn checkpoint_restores_branch_and_refuses_corrupt_state() {
    let dir = std::env::temp_dir().join(format!("scary-state-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let mut s = Mining::new(true, vec![]).unwrap();
    s.open_store(&dir).unwrap();
    let first = make_block([4; 32], 151, &[]);
    let second = make_block(hash256(&first[..80]), 152, &[]);
    s.observe(151, &first).unwrap();
    s.observe(152, &second).unwrap();
    s.save().unwrap();
    let mut recovered = Mining::new(true, vec![0x51]).unwrap();
    recovered.open_store(&dir).unwrap();
    assert_eq!(recovered.tip(), s.tip());
    assert_eq!(
        recovered.status()["validation_window"],
        s.status()["validation_window"]
    );
    assert_eq!(recovered.chain.applied.len(), 2);
    assert_eq!(recovered.payout, [0x51]);
    let f = dir.join("witness.state");
    let mut bytes = std::fs::read(&f).unwrap();
    bytes[12] ^= 1;
    std::fs::write(&f, bytes).unwrap();
    assert!(Mining::new(true, vec![]).unwrap().open_store(&dir).is_err());
    std::fs::remove_dir_all(&dir).unwrap();
}

#[tokio::test]
async fn packages_use_candidate_height_and_survive_restart() {
    let signer = P2PKH::from_private_key(PrivateKey::from_hex("abc101").unwrap());
    let lock = signer.lock().unwrap();
    let funding = tx(&"a1".repeat(32), 100000, lock.clone());
    let mut m = Mining::new(true, lock.to_binary()).unwrap();
    let block = make_block([3; 32], 250, &[funding.to_bytes().unwrap()]);
    m.observe(250, &block).unwrap();
    let next = make_block(m.tip().unwrap(), 251, &[]);
    m.observe(251, &next).unwrap();
    let mut p = tx(&funding.id().unwrap(), 90000, lock.clone());
    p.sign(0, &signer, 0x41, 100000, &lock).await.unwrap();
    let mut child = tx(&p.id().unwrap(), 80000, lock.clone());
    child.sign(0, &signer, 0x41, 90000, &lock).await.unwrap();
    let raws = [p.to_bytes().unwrap(), child.to_bytes().unwrap()];
    assert!(m.submit_tx(&raws[1]).is_err());
    m.submit_tx(&raws[0]).unwrap();
    m.submit_tx(&raws[1]).unwrap();
    let dir = std::env::temp_dir().join(format!("scary-package-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    m.open_store(&dir).unwrap();
    m.save().unwrap();
    let mut recovered = Mining::new(true, lock.to_binary()).unwrap();
    recovered.open_store(&dir).unwrap();
    assert_eq!(recovered.pending.len(), 2);
    let body = make_block(recovered.tip().unwrap(), 252, &raws);
    recovered.verify_own_block(252, &body, true).unwrap();
    let reversed = make_block(
        recovered.tip().unwrap(),
        252,
        &[raws[1].clone(), raws[0].clone()],
    );
    assert!(recovered.verify_own_block(252, &reversed, true).is_err());
    std::fs::remove_dir_all(dir).unwrap();
}

#[tokio::test]
#[ignore = "release throughput measurement"]
async fn measure_signed_body_commit() {
    let n = 4096;
    let signer = P2PKH::from_private_key(PrivateKey::from_hex("abcd29").unwrap());
    let lock = signer.lock().unwrap();
    let mut m = Mining::new(true, lock.to_binary()).unwrap();
    let dir = std::env::temp_dir().join(format!("scary-bench-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    m.open_store(&dir).unwrap();
    let mut blocks = vec![];
    let mut prev = [7; 32];
    let mut parents = (0..n)
        .map(|i| tx(&format!("{:064x}", i + 1), 100000, lock.clone()))
        .collect::<Vec<_>>();
    for round in 0..5u32 {
        let mut txs = vec![];
        for parent in &mut parents {
            if round > 0 {
                let mut child = tx(
                    &parent.id().unwrap(),
                    100000 - u64::from(round) * 1000,
                    lock.clone(),
                );
                child
                    .sign(0, &signer, 0x41, 101000 - u64::from(round) * 1000, &lock)
                    .await
                    .unwrap();
                *parent = child;
            }
            txs.push(parent.to_bytes().unwrap());
        }
        let body = make_block(prev, 250 + round, &txs);
        prev = hash256(&body[..80]);
        blocks.push(body);
    }
    for (round, b) in blocks.iter().enumerate() {
        let start = std::time::Instant::now();
        m.observe(250 + round as u32, b).unwrap();
        let observed = start.elapsed();
        m.save().unwrap();
        let total = start.elapsed();
        let checks = m.validation_window.back().unwrap().checks;
        if round > 0 {
            assert_eq!(checks.script_checks_passed, n as u64);
            assert_eq!(checks.transactions_determinate, n as u64);
        }
        println!(
            "{}",
            json!({"cohort":"signed_body_commit","round":round,"transactions":n,"wire_bytes":b.len(),
            "known_signatures":checks.script_checks_passed,"observe_us":observed.as_micros(),"commit_inclusive_us":total.as_micros(),
            "commit_inclusive_tps":n as f64/total.as_secs_f64(),
            "checkpoint_bytes":std::fs::metadata(dir.join("witness.state")).unwrap().len()})
        );
    }
    std::fs::remove_dir_all(dir).unwrap();
}

#[tokio::test]
async fn soft_freeze_releases_on_fourth_successor_and_explicit_invalidation_rolls_back() {
    use crate::alerts::tests::{keys, public, signed};
    let signer = P2PKH::from_private_key(PrivateKey::from_hex("abcd1004").unwrap());
    let lock = signer.lock().unwrap();
    let source = tx(&"45".repeat(32), 100000, lock.clone());
    let mut child = tx(&source.id().unwrap(), 90000, lock.clone());
    child.sign(0, &signer, 0x41, 100000, &lock).await.unwrap();
    let mut m = Mining::new(true, lock.to_binary()).unwrap();
    let signing = keys(12);
    m.directives = crate::alerts::Book::new(&public(&signing), false).unwrap();
    let parent = make_block([7; 32], 250, &[source.to_bytes().unwrap()]);
    m.observe(250, &parent).unwrap();
    let mut freeze = hex::decode(source.id().unwrap()).unwrap();
    freeze.extend(0u64.to_le_bytes());
    freeze.extend(251u64.to_le_bytes());
    freeze.extend(300u64.to_le_bytes());
    freeze.push(1);
    let envelope = signed(1, 2, &freeze, &signing);
    m.directives.receive(&envelope, None).unwrap();
    m.directives_changed(&envelope).unwrap();
    let first = make_block(m.tip().unwrap(), 251, &[child.to_bytes().unwrap()]);
    m.observe(251, &first).unwrap();
    let mut prev = hash256(&first[..80]);
    for height in 252..=255 {
        let block = make_block(prev, height, &[]);
        prev = hash256(&block[..80]);
        m.observe(height, &block).unwrap();
        assert_eq!(m.height, Some(if height == 255 { 255 } else { 250 }));
        assert!(m.awaiting_height.is_none());
    }
    let mut payload = hash256(&first[..80]).to_vec();
    payload.extend(b"\x03lab");
    let invalid = signed(2, 7, &payload, &signing);
    m.directives.receive(&invalid, None).unwrap();
    m.directives_changed(&invalid).unwrap();
    assert_eq!(m.tip(), Some(hash256(&parent[..80])));
    let extension = make_block(prev, 256, &[]);
    assert!(m.observe(256, &extension).is_err());
    assert!(m.outpoint(&source.id().unwrap(), 0).unwrap()["known"] == true);
}

#[test]
fn checkpoint_interruption_preserves_previous_boundary_and_burns_extranonce_lease() {
    let dir = std::env::temp_dir().join(format!("scary-interrupt-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let mut m = Mining::new(true, vec![]).unwrap();
    m.open_store(&dir).unwrap();
    let parent = make_block([71; 32], 240, &[]);
    m.observe(240, &parent).unwrap();
    m.save().unwrap();
    assert_eq!(m.extranonce_lease().unwrap(), (0, 1024));
    let child = make_block(m.tip().unwrap(), 241, &[]);
    m.observe(241, &child).unwrap();
    std::fs::create_dir(dir.join("witness.next")).unwrap();
    assert!(m.save().is_err());
    let mut restored = Mining::new(true, vec![]).unwrap();
    restored.open_store(&dir).unwrap();
    assert_eq!(restored.tip(), Some(hash256(&parent[..80])));
    assert_eq!(restored.extranonce_lease().unwrap(), (1024, 2048));
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn distant_gap_needs_two_linked_bodies_and_restarts_with_empty_old_evidence() {
    let mut m = Mining::new(true, vec![]).unwrap();
    let old = make_block([10; 32], 100, &[]);
    m.observe(100, &old).unwrap();
    let new = make_block([52; 32], 150, &[]);
    m.observe(150, &new).unwrap();
    assert!(!m.observation_window_ready());
    assert_eq!(m.height, Some(100));
    let child = make_block(hash256(&new[..80]), 151, &[]);
    m.observe(151, &child).unwrap();
    assert_eq!(m.tip(), Some(hash256(&child[..80])));
    assert_eq!(m.headers.len(), 2);
    assert_eq!(m.chain.applied.len(), 2);
}
