// Compiled only inside a temporary archive of the implementation under study.
use super::{Queue, Request};
use crate::mining::Mining;
use anyhow::{ensure, Context, Result};
use axum::{
    body::Bytes,
    extract::{Path, State},
    routing::get,
    Router,
};
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    fs,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

struct Asset {
    bodies: HashMap<String, (usize, Bytes)>,
    start: Instant,
    log: Mutex<Vec<(usize, u64, u64)>>,
}

async fn read_root(Path(root): Path<String>, State(asset): State<Arc<Asset>>) -> Bytes {
    let (index, body) = &asset.bodies[&root];
    let requested = asset.start.elapsed().as_micros() as u64;
    tokio::time::sleep(Duration::from_millis(if *index == 0 { 400 } else { 100 })).await;
    asset
        .log
        .lock()
        .unwrap()
        .push((*index, requested, asset.start.elapsed().as_micros() as u64));
    body.clone()
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
#[ignore = "registered sealed subtree scheduling comparison"]
async fn compare() -> Result<()> {
    let fixture: Value =
        serde_json::from_slice(&fs::read(std::env::var("SCARY_SCHEDULER_FIXTURE")?)?)?;
    let mut mining = Mining::new(true, vec![0x51])?;
    for (height, field) in [(107, "funding_hex"), (108, "successor_hex")] {
        mining.observe(
            height,
            &hex::decode(fixture[field].as_str().context(field)?)?,
        )?;
    }
    let parent = mining.shadow_decision()?["hash"].clone();
    ensure!(parent == fixture["parent"], "fresh successor differs");
    let state = Arc::new(tokio::sync::Mutex::new(mining));
    let parallel: usize = std::env::var("SCARY_SCHEDULER_PARALLEL")?.parse()?;
    let capacity: usize = std::env::var("SCARY_SCHEDULER_CAPACITY")?.parse()?;
    let queue = start_probe_queue(state.clone(), parallel, capacity);
    let roots = fixture["roots"].as_array().context("roots array")?;
    let mut bodies = HashMap::new();
    for (i, r) in roots.iter().enumerate() {
        bodies.insert(
            r["root"].as_str().context("root")?.to_owned(),
            (
                i,
                Bytes::from(hex::decode(r["wire"].as_str().context("data")?)?),
            ),
        );
    }
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
    let base = format!("http://{}", listener.local_addr()?);
    let start = Instant::now();
    let asset = Arc::new(Asset {
        bodies,
        start,
        log: Mutex::new(Vec::new()),
    });
    let web = Router::new()
        .route("/subtree_data/{root}", get(read_root))
        .with_state(asset.clone());
    let server = tokio::spawn(async move { axum::serve(listener, web).await });
    let mut offers = Vec::new();
    for (i, r) in roots.iter().enumerate() {
        tokio::time::sleep_until(tokio::time::Instant::from_std(
            start + Duration::from_millis(20 * i as u64),
        ))
        .await;
        let at = start.elapsed().as_micros() as u64;
        let accepted = queue
            .offer(Request {
                base: base.clone(),
                height: 0,
                hash: r["root"].as_str().unwrap().into(),
                header: None,
            })
            .is_ok();
        offers.push(json!({"index":i,"at_us":at,"accepted":accepted}));
    }
    tokio::time::timeout(Duration::from_secs(90), async {
        while !queue.schedule.lock().unwrap().pending.is_empty() {
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .context("queue failed to drain")?;
    let drained = start.elapsed().as_micros() as u64;
    let after = state.lock().await.shadow_decision()?;
    let transactions = match after["unsigned_candidate"].get("transactions") {
        Some(ids) => ids
            .as_array()
            .context("malformed unsigned template")?
            .clone(),
        None => Vec::new(),
    };
    let result = json!({"parallel":parallel,"capacity":capacity,"parent_before":parent,
        "parent_after":after["hash"],"offers":offers,"requests":*asset.log.lock().unwrap(),
        "drained_us":drained,"transaction_ids":transactions});
    fs::write(
        std::env::var("SCARY_SCHEDULER_RESULT")?,
        serde_json::to_vec(&result)?,
    )?;
    server.abort();
    Ok(())
}
