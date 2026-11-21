//! Read-only native Teranode assets for blocks this process has solved.
use super::body_limit;
use crate::mining::wire::{display_hash, merkle, put_varint, take, transaction, varint, Hash};
use anyhow::{ensure, Context, Result};
use axum::{
    body::{Body, Bytes},
    extract::{DefaultBodyLimit, Path, State},
    http::{header, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Router,
};
use bsv::primitives::hash::hash256;
use std::{
    collections::{HashMap, HashSet, VecDeque},
    net::SocketAddr,
    ops::Range,
    sync::{
        atomic::{AtomicU8, Ordering},
        Arc,
    },
};
use tokio::{
    net::TcpListener,
    sync::{Mutex, RwLock},
};

struct Store {
    mining: Arc<Mutex<crate::mining::Mining>>,
    cached: RwLock<VecDeque<Arc<Assets>>>,
    rebuild: Mutex<()>,
}

pub(super) struct Assets {
    pub hash: String,
    pub subtree: Option<String>,
    pub header: String,
    raw: Bytes,
    blkdat_header: Option<[u8; 8]>,
    block_model: Bytes,
    leaves: Bytes,
    tail: Range<usize>,
    transactions: HashMap<Hash, Range<usize>>,
    requested: AtomicU8,
}

#[derive(Clone, Copy, serde::Serialize)]
#[serde(rename_all = "snake_case")]
enum NativeAsset {
    Block,
    Subtree,
    SubtreeData,
    SubtreeTransactions,
}

// The optional blkdat representation in v0.16.1 adds the header and count
// width to SizeInBytes. Only that legacy representation uses a 32-bit length.
fn blkdat_header(raw_len: usize, header_and_count: usize) -> Option<[u8; 8]> {
    let size = u32::try_from(raw_len.checked_add(header_and_count)?).ok()?;
    let mut header = [0xf9, 0xbe, 0xb4, 0xd9, 0, 0, 0, 0];
    header[4..].copy_from_slice(&size.to_le_bytes());
    Some(header)
}

impl Assets {
    fn blkdat(&self) -> Response {
        let Some(header) = self.blkdat_header else {
            return (
                StatusCode::NOT_ACCEPTABLE,
                "Block size exceeds blkdat framing. Request ?wire=1.",
            )
                .into_response();
        };
        framed_binary(
            self.raw.len() + header.len(),
            [Bytes::copy_from_slice(&header), self.raw.clone()],
        )
    }

    // Report at most one successful request per representation per retained
    // block. The receiving node's tip provides the separate acceptance result.
    fn requested(&self, kind: NativeAsset) {
        let mask = 1 << kind as u8;
        if self.requested.fetch_or(mask, Ordering::Relaxed) & mask == 0 {
            println!(
                "{}",
                serde_json::json!({"event":"asset_request", "block":self.hash, "asset":kind})
            );
        }
    }

    pub fn new(height: u32, raw: impl Into<Bytes>) -> Result<Self> {
        let raw = raw.into();
        ensure!(
            raw.len() <= body_limit(),
            "block exceeds the alpha Asset size limit"
        );
        let mut remaining = &raw[..];
        let header = take(&mut remaining, 80)?;
        let count = varint(&mut remaining)?;
        // The reference adds the header and CompactSize width to the native
        // SizeInBytes field when framing the no-query response.
        let header_and_count = raw.len() - remaining.len();
        let blkdat_header = blkdat_header(raw.len(), header_and_count);
        ensure!(
            count > 0 && count <= remaining.len() / 60,
            "invalid block transaction count"
        );
        let mut txs = HashMap::new();
        let mut ids = Vec::with_capacity(count);
        let mut coinbase = 0..0;
        for index in 0..count {
            let start = raw.len() - remaining.len();
            let original = transaction(&mut remaining, |_| {})?;
            let range = start..(start + original.len());
            let txid = hash256(original);
            ensure!(
                txs.insert(txid, range.clone()).is_none(),
                "duplicate txid in solved block"
            );
            ids.push(txid);
            if index == 0 {
                coinbase = range;
            }
        }
        ensure!(remaining.is_empty(), "trailing bytes in block");
        let (root, _) = merkle(ids.clone())?;
        ensure!(header[36..68] == root, "block merkle root mismatch");
        let mut model = header.to_vec();
        put_varint(&mut model, count);
        put_varint(&mut model, raw.len());
        let mut tree_hash = None;
        let mut leaves = Vec::new();
        // v0.16.1 model.NewBlockFromMsgBlock omits subtrees for coinbase-only blocks.
        // Other blocks here use one subtree, with go-subtree's 0xff coinbase placeholder.
        if count == 1 {
            put_varint(&mut model, 0);
        } else {
            ids[0] = [0xff; 32];
            let (hash, _) = merkle(ids.clone())?;
            put_varint(&mut model, 1);
            model.extend(hash);
            tree_hash = Some(display_hash(hash));
            leaves.reserve(count * 32);
            for id in ids {
                leaves.extend(id);
            }
        }
        model.extend(&raw[coinbase.clone()]);
        put_varint(&mut model, height as usize);
        put_varint(&mut model, 0); // optional coinbase BUMP
        Ok(Self {
            hash: display_hash(hash256(header)),
            subtree: tree_hash,
            header: hex::encode(header),
            block_model: model.into(),
            leaves: leaves.into(),
            tail: coinbase.end..raw.len(),
            transactions: txs,
            raw,
            blkdat_header,
            requested: AtomicU8::new(0),
        })
    }
}

#[derive(Clone)]
pub(super) struct AssetService {
    store: Arc<Store>,
}

impl AssetService {
    pub fn new(mining: Arc<Mutex<crate::mining::Mining>>) -> Self {
        Self {
            store: Arc::new(Store {
                mining,
                cached: RwLock::default(),
                rebuild: Mutex::new(()),
            }),
        }
    }

    pub async fn serve(&self, bind: SocketAddr, base: &str) -> Result<()> {
        let base = reqwest::Url::parse(base)?;
        ensure!(super::valid_base(&base), "invalid advertised Asset URL");
        let routes = Router::new()
            .route("/block/{hash}", get(block))
            .route("/block_legacy/{hash}", get(legacy))
            .route("/header/{hash}", get(block_header))
            .route("/subtree/{hash}", get(subtree))
            .route("/subtree_data/{hash}", get(subtree_data))
            .route("/subtree/{hash}/txs", post(batch))
            .layer(DefaultBodyLimit::max(1024 * 1024))
            .with_state(self.clone());
        let path = base.path().trim_end_matches('/');
        let app = if path.is_empty() {
            routes
        } else {
            Router::new().nest(path, routes)
        };
        let socket = TcpListener::bind(bind)
            .await
            .context("binding the read-only Asset server")?;
        tokio::spawn(async move {
            if let Err(err) = axum::serve(socket, app).await {
                eprintln!("asset server stopped: {err}");
            }
        });
        Ok(())
    }

    pub async fn insert(&self, asset: Arc<Assets>) {
        let mut list = self.store.cached.write().await;
        list.retain(|old| old.hash != asset.hash);
        list.push_back(asset);
        while list.len() > 16 || list.iter().map(|a| a.raw.len()).sum::<usize>() > body_limit() {
            list.pop_front();
        }
    }
}

fn binary(bytes: Bytes) -> Response {
    ([(header::CONTENT_TYPE, "application/octet-stream")], bytes).into_response()
}

// All parts are immutable views of retained bytes, with their exact total
// length known before headers are sent. No response-sized copy is needed.
fn framed_binary(
    length: usize,
    chunks: impl IntoIterator<Item = Bytes, IntoIter: Send + 'static>,
) -> Response {
    let length = length.to_string();
    let body = Body::from_stream(futures::stream::iter(
        chunks.into_iter().map(Ok::<_, std::convert::Infallible>),
    ));
    (
        [
            (header::CONTENT_TYPE, "application/octet-stream"),
            (header::CONTENT_LENGTH, &length),
        ],
        body,
    )
        .into_response()
}

async fn lookup(service: &AssetService, key: &str, tree: bool) -> Option<Arc<Assets>> {
    let resolve = async || {
        service
            .store
            .mining
            .lock()
            .await
            .requested_solution(key, tree)
    };
    let (id, _, _) = resolve().await?;
    let hash = display_hash(id);
    let cached = async || {
        service
            .store
            .cached
            .read()
            .await
            .iter()
            .find(|a| a.hash == hash)
            .cloned()
    };
    if let Some(asset) = cached().await {
        return Some(asset);
    }

    // Coalesce cold reads. Building a subtree index can hash a large block.
    // Only a locally retained, eligible solution can start this work.
    let _building = service.store.rebuild.lock().await;
    let (current, height, raw) = resolve().await?;
    if current != id {
        return None;
    }
    if let Some(asset) = cached().await {
        return Some(asset);
    }
    let asset = Arc::new(
        tokio::task::spawn_blocking(move || Assets::new(height, raw))
            .await
            .ok()?
            .ok()?,
    );
    if resolve().await?.0 != id {
        return None;
    }
    service.insert(asset.clone()).await;
    Some(asset)
}
async fn block(State(store): State<AssetService>, Path(hash): Path<String>) -> Response {
    match lookup(&store, &hash, false).await {
        Some(a) => {
            a.requested(NativeAsset::Block);
            binary(a.block_model.clone())
        }
        None => StatusCode::NOT_FOUND.into_response(),
    }
}
async fn block_header(State(store): State<AssetService>, Path(hash): Path<String>) -> Response {
    match lookup(&store, &hash, false).await {
        Some(a) => binary(a.raw.slice(..80)),
        None => StatusCode::NOT_FOUND.into_response(),
    }
}
async fn legacy(
    State(store): State<AssetService>,
    Path(hash): Path<String>,
    axum::extract::Query(query): axum::extract::Query<HashMap<String, String>>,
) -> Response {
    let Some(a) = lookup(&store, &hash, false).await else {
        return StatusCode::NOT_FOUND.into_response();
    };
    if query.get("wire").is_some_and(|value| !value.is_empty()) {
        return binary(a.raw.clone());
    }
    a.blkdat()
}
async fn subtree(State(store): State<AssetService>, Path(hash): Path<String>) -> Response {
    match lookup(&store, &hash, true).await {
        Some(a) => {
            a.requested(NativeAsset::Subtree);
            binary(a.leaves.clone())
        }
        None => StatusCode::NOT_FOUND.into_response(),
    }
}
async fn subtree_data(State(store): State<AssetService>, Path(hash): Path<String>) -> Response {
    match lookup(&store, &hash, true).await {
        Some(a) => {
            a.requested(NativeAsset::SubtreeData);
            binary(a.raw.slice(a.tail.clone()))
        }
        None => StatusCode::NOT_FOUND.into_response(),
    }
}
async fn batch(
    State(store): State<AssetService>,
    Path(hash): Path<String>,
    body: Bytes,
) -> Response {
    let Some(a) = lookup(&store, &hash, true).await else {
        return StatusCode::NOT_FOUND.into_response();
    };
    let (ids, remainder) = body.as_chunks::<32>();
    if ids.is_empty() || !remainder.is_empty() {
        return StatusCode::BAD_REQUEST.into_response();
    }
    let mut out = Vec::with_capacity(ids.len());
    let mut length = 0;
    let mut seen = HashSet::new();
    for id in ids {
        if !seen.insert(id) {
            return StatusCode::BAD_REQUEST.into_response();
        }
        let Some(range) = a.transactions.get(id) else {
            return StatusCode::NOT_FOUND.into_response();
        };
        length += range.len();
        out.push(a.raw.slice(range.clone()));
    }
    a.requested(NativeAsset::SubtreeTransactions);
    framed_binary(length, out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::mining::wire::joined;

    fn example_transaction(byte: u8) -> Vec<u8> {
        let mut tx = vec![1, 0, 0, 0, 1];
        tx.extend([byte; 32]);
        tx.extend([0xff; 4]);
        tx.extend([2, 1, byte]);
        tx.extend([0xff; 4]);
        tx.extend([1, 1, 0, 0, 0, 0, 0, 0, 0, 1, 0x51, 0, 0, 0, 0]);
        tx
    }

    #[test]
    fn legacy_size_only_limits_the_optional_blkdat_representation() {
        let largest = u32::MAX as usize - 83;
        assert_eq!(blkdat_header(largest, 83).unwrap()[4..], [0xff; 4]);
        assert!(blkdat_header(largest + 1, 83).is_none());
        assert!(blkdat_header(usize::MAX, 83).is_none());
    }

    #[tokio::test]
    async fn serves_wire_and_native_commitments_from_the_same_solved_bytes() {
        let coinbase = example_transaction(0);
        let child = example_transaction(1);
        let child_id = hash256(&child);
        let root = joined(hash256(&coinbase), child_id);
        let mut raw = vec![0; 80];
        raw[36..68].copy_from_slice(&root);
        raw.push(2);
        raw.extend(&coinbase);
        raw.extend(&child);
        let mut a = Assets::new(10, raw.clone()).unwrap();
        let reply = a.blkdat();
        assert_eq!(
            reply.headers()[header::CONTENT_LENGTH],
            (raw.len() + 8).to_string()
        );
        let framed = axum::body::to_bytes(reply.into_body(), raw.len() + 8)
            .await
            .unwrap();
        a.blkdat_header = None;
        assert_eq!(a.blkdat().status(), StatusCode::NOT_ACCEPTABLE);
        assert_eq!(&framed[..4], &[0xf9, 0xbe, 0xb4, 0xd9]);
        assert_eq!(&framed[4..8], &((raw.len() + 81) as u32).to_le_bytes());
        assert_eq!(&framed[8..], &raw);
        let tree_hash = joined([0xff; 32], child_id);
        assert_eq!(a.subtree, Some(display_hash(tree_hash)));
        assert_eq!(&a.leaves[..32], &[0xff; 32]);
        assert_eq!(&a.leaves[32..], &child_id);
        assert_eq!(&a.raw[a.tail.clone()], &child);
        let mut model = &a.block_model[..];
        assert_eq!(take(&mut model, 80).unwrap(), &raw[..80]);
        assert_eq!(varint(&mut model).unwrap(), 2);
        assert_eq!(varint(&mut model).unwrap(), raw.len());
        assert_eq!(varint(&mut model).unwrap(), 1);
        assert_eq!(take(&mut model, 32).unwrap(), tree_hash);
        assert_eq!(transaction(&mut model, |_| {}).unwrap(), coinbase);
        assert_eq!(varint(&mut model).unwrap(), 10);
        assert_eq!(varint(&mut model).unwrap(), 0);
        assert!(model.is_empty());
        raw[36] ^= 1;
        assert!(Assets::new(10, raw).is_err());
    }
}
