//! Bounded block retrieval on a separate task so Asset I/O cannot stop GossipSub.
use super::{body_limit, Event};
use crate::mining::wire::display_hash;
use anyhow::{ensure, Context, Result};
use bsv::primitives::hash::hash256;
use futures::StreamExt;
use reqwest::{redirect::Policy, Url};
use std::{
    collections::{HashMap, VecDeque},
    net::{IpAddr, SocketAddr},
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::sync::mpsc;

#[derive(Clone)]
pub(super) struct Request {
    pub base: String,
    pub height: u32,
    pub hash: String,
    pub header: Option<Vec<u8>>,
}

const MAX_QUEUED_HASHES: usize = 16;
const MAX_SOURCES_PER_HASH: usize = 3;

#[derive(Default)]
struct Pending {
    sources: Vec<Request>,
    next: usize,
}

#[derive(Default)]
struct Schedule {
    pending: HashMap<String, Pending>,
    delivered: VecDeque<String>,
}

pub(super) struct Queue {
    schedule: Arc<Mutex<Schedule>>,
    ready: mpsc::Sender<String>,
}

impl Queue {
    pub fn start(network: String, events: mpsc::Sender<Event>) -> Self {
        let (queue, inbox) = Self::channel();
        tokio::spawn(worker(network, inbox, queue.schedule.clone(), events));
        queue
    }

    pub fn start_subtrees(
        network: String,
        state: Arc<tokio::sync::Mutex<crate::mining::Mining>>,
    ) -> Self {
        let (queue, mut inbox) = Self::channel();
        let schedule = queue.schedule.clone();
        tokio::spawn(async move {
            while let Some(hash) = inbox.recv().await {
                loop {
                    let Some(request) = schedule
                        .lock()
                        .expect("subtree schedule")
                        .next_source(&hash)
                    else {
                        break;
                    };
                    let result = super::subtree::receive(&network, &request, &state).await;
                    match result {
                        Ok(()) => {
                            schedule
                                .lock()
                                .expect("subtree schedule")
                                .mark_delivered(hash.clone());
                            break;
                        }
                        Err(err) => eprintln!("transaction subtree {hash} unavailable: {err}"),
                    }
                }
            }
        });
        queue
    }

    fn channel() -> (Self, mpsc::Receiver<String>) {
        let (ready, inbox) = mpsc::channel(MAX_QUEUED_HASHES);
        let schedule = Arc::new(Mutex::new(Schedule::default()));
        (Self { schedule, ready }, inbox)
    }

    pub fn offer(&self, request: Request) -> Result<()> {
        let mut schedule = self.schedule.lock().expect("download schedule lock");
        if schedule.delivered.contains(&request.hash) {
            return Ok(());
        }
        if let Some(pending) = schedule.pending.get_mut(&request.hash) {
            if pending.sources.len() < MAX_SOURCES_PER_HASH
                && !pending
                    .sources
                    .iter()
                    .any(|source| source.base == request.base)
            {
                pending.sources.push(request);
            }
            return Ok(());
        }
        self.ready
            .try_send(request.hash.clone())
            .context("unique block queue is full or closed")?;
        schedule.pending.insert(
            request.hash.clone(),
            Pending {
                sources: vec![request],
                next: 0,
            },
        );
        Ok(())
    }
}

impl Schedule {
    fn next_source(&mut self, hash: &str) -> Option<Request> {
        let pending = self.pending.get_mut(hash)?;
        if let Some(source) = pending.sources.get(pending.next) {
            pending.next += 1;
            return Some(source.clone());
        }
        self.pending.remove(hash);
        None
    }

    fn mark_delivered(&mut self, hash: String) {
        self.pending.remove(&hash);
        self.delivered.push_back(hash);
        if self.delivered.len() > 256 {
            self.delivered.pop_front();
        }
    }
}

pub(super) fn valid_base(url: &Url) -> bool {
    matches!(url.scheme(), "https" | "http")
        && url.host().is_some()
        && url.username().is_empty()
        && url.password().is_none()
        && url.fragment().is_none()
        && url.query().is_none()
}

// Mainnet gossip can come from any publisher. Resolve once, reject local/special
// addresses, pin the validated result to this request and disable redirects/proxies.
pub(crate) fn public_ip(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(v) => {
            let [a, b, c, _] = v.octets();
            !(v.is_private()
                || v.is_loopback()
                || v.is_link_local()
                || v.is_documentation()
                || a == 0
                || a >= 224
                || a == 100 && (64..128).contains(&b)
                || a == 192 && b == 0 && c == 0
                || a == 192 && b == 88 && c == 99
                || a == 198 && (b == 18 || b == 19))
        }
        IpAddr::V6(v) => {
            let s = v.segments();
            (s[0] & 0xe000) == 0x2000
                && s[0] != 0x2002
                && !(s[0] == 0x2001 && (s[1] <= 0x1ff || s[1] == 0xdb8))
        }
    }
}

pub(super) async fn download(
    network: &str,
    base: &str,
    path: &str,
    query: Option<&str>,
) -> Result<crate::body::BlockBody> {
    let mut url = Url::parse(base)?;
    ensure!(valid_base(&url), "invalid advertised Asset base URL");
    let host = url
        .host_str()
        .context("missing host")?
        .trim_matches(['[', ']'])
        .to_owned();
    let port = url.port_or_known_default().context("invalid port")?;
    let addresses: Vec<SocketAddr> = tokio::net::lookup_host((host.as_str(), port))
        .await?
        .collect();
    ensure!(
        !addresses.is_empty(),
        "asset hostname resolved to no addresses"
    );
    ensure!(
        network == "regtest" || addresses.iter().all(|a| public_ip(a.ip())),
        "mainnet Asset address is not public"
    );
    let client = reqwest::Client::builder()
        .no_proxy()
        .redirect(Policy::none())
        .resolve_to_addrs(&host, &addresses)
        .read_timeout(Duration::from_secs(45))
        .connect_timeout(Duration::from_secs(8))
        .build()?;
    url.set_path(&format!("{}/{path}", url.path().trim_end_matches('/')));
    url.set_query(query);
    let res = client.get(url).send().await?.error_for_status()?;
    ensure!(
        res.content_length().unwrap_or(0) <= body_limit() as u64,
        "Asset response exceeds configured capacity"
    );
    let mut spool = crate::body::Spool::new(body_limit());
    let mut chunks = res.bytes_stream();
    while let Some(chunk) = chunks.next().await {
        let chunk = chunk?;
        spool.append(&chunk)?;
    }
    spool.finish()
}

async fn fetch(network: &str, r: &Request) -> Result<crate::body::BlockBody> {
    // The default legacy response includes blkdat framing; request just the wire block.
    let bytes = download(
        network,
        &r.base,
        &format!("block_legacy/{}", r.hash),
        Some("wire=1"),
    )
    .await?;
    ensure!(bytes.len() > 80, "truncated Asset block");
    ensure!(
        display_hash(hash256(&bytes[..80])) == r.hash,
        "Asset block header hash mismatch"
    );
    if let Some(header) = &r.header {
        ensure!(
            bytes[..80] == header[..],
            "Asset and announcement header differ"
        );
    }
    Ok(bytes)
}

async fn worker(
    network: String,
    mut inbox: mpsc::Receiver<String>,
    schedule: Arc<Mutex<Schedule>>,
    events: mpsc::Sender<Event>,
) {
    while let Some(hash) = inbox.recv().await {
        let mut last_error = None;
        loop {
            let next = schedule
                .lock()
                .expect("download schedule lock")
                .next_source(&hash);
            let Some(r) = next else { break };
            let result = tokio::time::timeout(
                Duration::from_secs(55 + (body_limit() as u64 >> 20)),
                fetch(&network, &r),
            )
            .await;
            match result {
                Ok(Ok(raw)) => {
                    let (validated, result) = tokio::sync::oneshot::channel();
                    if events
                        .send(Event::Block {
                            source: r.base.clone(),
                            height: r.height,
                            raw,
                            validated,
                        })
                        .await
                        .is_err()
                    {
                        return;
                    }
                    // The HTTP header can match while the body fails its Merkle
                    // commitment. Keep alternate publishers eligible on failure.
                    match result.await {
                        Ok(true) => {
                            schedule
                                .lock()
                                .expect("download schedule lock")
                                .mark_delivered(hash.clone());
                            last_error = None;
                            break;
                        }
                        // The consumer already recorded a coverage reset for this body.
                        Ok(false) => last_error = None,
                        Err(_) => return,
                    }
                }
                Err(_) => last_error = Some(format!("Asset retrieval timed out at {hash}")),
                Ok(Err(e)) => last_error = Some(format!("Asset retrieval failed at {hash}: {e}")),
            }
        }
        if let Some(error) = last_error {
            if events
                .send(Event::Gap {
                    hash: Some(hash),
                    reason: error,
                })
                .await
                .is_err()
            {
                return;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn a_rejected_body_leaves_other_sources_eligible() {
        use axum::{routing::get, Router};
        use tokio::net::TcpListener;

        let header = [0; 80];
        let hash = display_hash(hash256(&header));
        let mut body = header.to_vec();
        body.push(0);
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let routes = Router::new().route(
            "/block_legacy/{hash}",
            get(move || std::future::ready(body.clone())),
        );
        let app = routes.clone().nest("/mirror", routes);
        let server = tokio::spawn(async move { axum::serve(listener, app).await });
        let (events, mut receiver) = mpsc::channel(2);
        let queue = Queue::start("regtest".into(), events);
        let request = Request {
            hash,
            height: 8,
            base,
            header: None,
        };
        queue.offer(request.clone()).unwrap();
        let mirror = Request {
            base: format!("{}/mirror", request.base),
            ..request.clone()
        };
        for valid in [false, true] {
            let event = tokio::time::timeout(Duration::from_secs(3), receiver.recv())
                .await
                .unwrap();
            let Some(Event::Block { validated, .. }) = event else {
                panic!("expected fetched block, got {event:?}")
            };
            // A burst of signed status/notice messages for an in-flight hash
            // occupies the same slot; the mirror remains available if rejected.
            for _ in 0..100 {
                queue.offer(request.clone()).unwrap();
                queue.offer(mirror.clone()).unwrap();
            }
            validated.send(valid).unwrap();
        }
        queue.offer(request).unwrap();
        drop(queue);
        assert!(receiver.recv().await.is_none());
        server.abort();
    }

    #[test]
    fn bursts_only_consume_one_slot_per_unseen_hash() {
        let (ready, mut inbox) = mpsc::channel(MAX_QUEUED_HASHES);
        let queue = Queue {
            schedule: Default::default(),
            ready,
        };
        let request = Request {
            base: "http://peer".into(),
            height: 1,
            hash: "dup".into(),
            header: None,
        };
        for i in 0..500 {
            queue
                .offer(Request {
                    base: format!("http://peer-{i}"),
                    ..request.clone()
                })
                .unwrap();
        }
        assert_eq!(
            queue.schedule.lock().unwrap().pending["dup"].sources.len(),
            MAX_SOURCES_PER_HASH
        );
        for i in 1..MAX_QUEUED_HASHES {
            queue
                .offer(Request {
                    hash: i.to_string(),
                    ..request.clone()
                })
                .unwrap();
        }
        assert!(queue
            .offer(Request {
                hash: "new".into(),
                ..request.clone()
            })
            .is_err());
        assert!(queue.offer(request.clone()).is_ok());
        assert_eq!(inbox.try_recv().unwrap(), "dup");
        queue.schedule.lock().unwrap().mark_delivered("dup".into());
        for _ in 0..100 {
            queue.offer(request.clone()).unwrap();
        }
        assert!(queue
            .offer(Request {
                hash: "new".into(),
                ..request
            })
            .is_ok());
    }

    #[test]
    fn mainnet_asset_resolution_excludes_local_networks() {
        for addr in [
            "127.0.0.1",
            "192.168.0.1",
            "172.16.1.2",
            "100.127.255.254",
            "169.254.1.2",
            "::1",
            "::ffff:127.0.0.1",
            "fe80::1",
            "fd00::1",
            "2002:7f00:1::",
        ] {
            assert!(!public_ip(addr.parse().unwrap()), "{addr}");
        }
        for addr in ["8.8.8.8", "2606:4700:4700::1111"] {
            assert!(public_ip(addr.parse().unwrap()), "{addr}");
        }
        assert!(!valid_base(
            &Url::parse("http://user@example.com/path").unwrap()
        ));
        assert!(!valid_base(
            &Url::parse("http://example.com/path?token=foo").unwrap()
        ));
    }
}
