//! Forward-only Bitcoin SV peer transport. Only announced inventory and
//! missing recent parents are requested. The common mining state decides
//! whether an offered body can advance the active branch.
use crate::{
    mining::{
        wire::{self, Hash},
        Mining,
    },
    peer::Event,
};
use anyhow::{ensure, Context, Result};
use bsv::primitives::hash::hash256;
use std::{net::SocketAddr, sync::Arc, time::Duration};
use tokio::{
    io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt},
    net::TcpStream,
    sync::{mpsc, Mutex},
};

const VERSION: i32 = 70016;
const EXTENDED_PAYLOAD_VERSION: i32 = 70016;
fn magic(regtest: bool) -> [u8; 4] {
    if regtest {
        [0xda, 0xb5, 0xbf, 0xfa]
    } else {
        [0xe3, 0xe1, 0xf3, 0xe8]
    }
}
fn command(raw: &[u8]) -> Result<&str> {
    let end = raw.iter().position(|b| *b == 0).unwrap_or(raw.len());
    ensure!(
        end > 0
            && raw[end..].iter().all(|&b| b == 0)
            && raw[..end].iter().all(u8::is_ascii_graphic),
        "invalid command"
    );
    Ok(std::str::from_utf8(&raw[..end])?)
}
const IO_PROGRESS: Duration = Duration::from_secs(10);

fn frame_header(regtest: bool, name: &str, size: u64, checksum: [u8; 4]) -> Vec<u8> {
    let mut cmd = [0u8; 12];
    cmd[..name.len()].copy_from_slice(name.as_bytes());
    let mut header = magic(regtest).to_vec();
    if size > u64::from(u32::MAX) {
        header.extend(*b"extmsg\0\0\0\0\0\0");
        header.extend(u32::MAX.to_le_bytes());
        header.extend([0; 4]);
        header.extend(cmd);
        header.extend(size.to_le_bytes());
    } else {
        header.extend(cmd);
        header.extend((size as u32).to_le_bytes());
        header.extend(checksum);
    }
    header
}

fn header_for(regtest: bool, name: &str, bytes: &[u8]) -> Vec<u8> {
    let checksum = if bytes.len() <= u32::MAX as usize {
        hash256(bytes)[..4].try_into().expect("four checksum bytes")
    } else {
        [0; 4]
    };
    frame_header(regtest, name, bytes.len() as u64, checksum)
}

async fn write_progress(
    w: &mut (impl AsyncWrite + Unpin),
    mut bytes: &[u8],
    max_idle: Duration,
) -> Result<()> {
    while !bytes.is_empty() {
        let n = tokio::time::timeout(max_idle, w.write(bytes)).await??;
        ensure!(n != 0, "SV peer closed during send");
        bytes = &bytes[n..];
    }
    Ok(())
}

async fn send(
    w: &mut (impl AsyncWrite + Unpin),
    regtest: bool,
    name: &str,
    body: &[u8],
) -> Result<()> {
    write_progress(w, &header_for(regtest, name, body), IO_PROGRESS).await?;
    write_progress(w, body, IO_PROGRESS).await?;
    tokio::time::timeout(IO_PROGRESS, w.flush()).await??;
    Ok(())
}

// A transfer holds immutable bytes. A peer waiting on TCP must not hold the
// mining lock. Release the final byte only while its exact hash is still eligible.
async fn send_block(
    w: &mut tokio::net::tcp::OwnedWriteHalf,
    regtest: bool,
    peer: SocketAddr,
    state: &Mutex<Mining>,
    hash: &str,
    raw: axum::body::Bytes,
) -> Result<()> {
    let (last, body) = raw.split_last().context("empty solution")?;
    let header = header_for(regtest, "block", &raw);
    write_progress(w, &header, IO_PROGRESS).await?;
    for chunk in body.chunks(1 << 20) {
        {
            let s = state.lock().await;
            ensure!(
                s.observation_window_ready() && !s.directives.peer_banned(&peer.ip()),
                "block delivery suspended"
            );
        }
        write_progress(w, chunk, IO_PROGRESS).await?;
    }
    loop {
        tokio::time::timeout(IO_PROGRESS, w.writable()).await??;
        let s = state.lock().await;
        ensure!(
            !s.directives.peer_banned(&peer.ip()) && s.requested_solution(hash, false).is_some(),
            "block retired during delivery"
        );
        match w.try_write(std::slice::from_ref(last)) {
            Ok(1) => return Ok(()),
            Ok(_) => anyhow::bail!("SV peer closed before block completion"),
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => continue,
            Err(e) => return Err(e.into()),
        }
    }
}

#[derive(Debug)]
struct IncompleteBody {
    hash: Option<String>,
    reason: String,
}
impl std::fmt::Display for IncompleteBody {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.reason)
    }
}
impl std::error::Error for IncompleteBody {}

const READ_PROGRESS: Duration = Duration::from_secs(30);

async fn receive_header(
    r: &mut (impl AsyncRead + Unpin),
    regtest: bool,
    budget: Duration,
) -> Result<(String, u64, Option<[u8; 4]>)> {
    // An established peer may stay quiet between frames. Once its first byte
    // arrives, the entire 24- or 44-byte header shares a single deadline.
    let mut h = [0u8; 24];
    h[0] = r.read_u8().await?;
    let deadline = tokio::time::Instant::now() + budget;
    tokio::time::timeout_at(deadline, r.read_exact(&mut h[1..]))
        .await
        .context("SV frame header timed out")??;
    ensure!(h[..4] == magic(regtest), "SV peer network mismatch");
    let cmd = command(&h[4..16])?;
    let size = u32::from_le_bytes(h[16..20].try_into()?);
    let checksum = h[20..24].try_into()?;
    if cmd != "extmsg" {
        return Ok((cmd.to_owned(), u64::from(size), Some(checksum)));
    }
    ensure!(
        size == u32::MAX && checksum == [0; 4],
        "invalid extmsg header"
    );
    let mut more = [0u8; 20];
    tokio::time::timeout_at(deadline, r.read_exact(&mut more))
        .await
        .context("SV extended frame header timed out")??;
    Ok((
        command(&more[..12])?.to_owned(),
        u64::from_le_bytes(more[12..].try_into()?),
        None,
    ))
}

async fn receive(
    r: &mut (impl AsyncRead + Unpin),
    regtest: bool,
    limit: usize,
) -> Result<(String, crate::body::BlockBody)> {
    let (cmd, n, checksum) = receive_header(r, regtest, READ_PROGRESS).await?;
    let cap = if cmd == "block" {
        limit
    } else if cmd == "tx" {
        crate::mining::transaction_limit()
    } else {
        limit.min(32 << 20)
    };
    let mut prefix = Vec::with_capacity(80);
    let failure = |prefix: &[u8], reason: String| -> anyhow::Error {
        if cmd == "block" {
            IncompleteBody {
                hash: (prefix.len() == 80).then(|| wire::display_hash(hash256(prefix))),
                reason,
            }
            .into()
        } else {
            anyhow::anyhow!(reason)
        }
    };
    if n > cap as u64 {
        return Err(failure(
            &prefix,
            format!("SV {cmd} of {n} bytes exceeds configured capacity {cap}"),
        ));
    }
    let mut spool = crate::body::Spool::new(cap);
    let mut input = (&mut *r).take(n);
    let mut chunk = vec![0; 64 << 10];
    let mut left = n;
    while left != 0 {
        // A quiet peer may legitimately send no new message for a long time.
        // Once a header promises a body, require its stream to make progress.
        let received = tokio::time::timeout(READ_PROGRESS, input.read(&mut chunk))
            .await
            .map_err(|_| failure(&prefix, format!("SV {cmd} stopped mid-message")))?
            .map_err(|e| failure(&prefix, format!("SV {cmd} read failed: {e}")))?;
        if received == 0 {
            return Err(failure(
                &prefix,
                format!("truncated SV {cmd} with {left} bytes outstanding"),
            ));
        }
        if cmd == "block" && prefix.len() < 80 {
            prefix.extend_from_slice(&chunk[..received.min(80 - prefix.len())]);
        }
        spool
            .append(&chunk[..received])
            .map_err(|e| failure(&prefix, format!("SV {cmd} spool failed: {e}")))?;
        left -= received as u64;
    }
    let body = spool
        .finish()
        .map_err(|e| failure(&prefix, format!("SV {cmd} spool failed: {e}")))?;
    if checksum.is_some_and(|expected| hash256(&body)[..4] != expected) {
        return Err(failure(
            &prefix,
            format!("SV {cmd} message checksum mismatch"),
        ));
    }
    Ok((cmd, body))
}
fn version(height: u32) -> Vec<u8> {
    let mut raw = VERSION.to_le_bytes().to_vec();
    raw.extend(0u64.to_le_bytes());
    raw.extend((crate::mining::now().unwrap_or(0) as i64).to_le_bytes());
    raw.extend([0; 52]);
    let nonce = libp2p::identity::Keypair::generate_ed25519()
        .public()
        .encode_protobuf();
    raw.extend(&hash256(&nonce)[..8]);
    let agent = b"/scary-node/";
    wire::put_varint(&mut raw, agent.len());
    raw.extend(agent);
    raw.extend(height.to_le_bytes());
    raw.push(1);
    raw
}
async fn send_headers_reply(
    w: &mut (impl AsyncWrite + Unpin),
    regtest: bool,
    state: &Mutex<Mining>,
    mut locator: &[u8],
) -> Result<()> {
    wire::take(&mut locator, 4)?;
    let n = wire::varint(&mut locator)?;
    ensure!(
        n <= 101 && locator.len() == 32 * (n + 1),
        "invalid header locator"
    );
    let (ancestors, stop) = locator.split_at(n * 32);
    let mut reply = vec![0];
    {
        let mining = state.lock().await;
        if let Some(id) = mining.relayable_solution() {
            let block = mining
                .solution_bytes(&id)
                .expect("selected stored solution");
            let has_parent = ancestors
                .as_chunks::<32>()
                .0
                .iter()
                .any(|h| h == &block[4..36]);
            if (stop == id || stop == [0; 32]) && (has_parent || (n == 0 && stop == id)) {
                reply[0] = 1;
                reply.extend(&block[..80]);
                reply.push(0);
            }
        }
    }
    send(w, regtest, "headers", &reply).await
}

fn header_locator(parent: Hash) -> Vec<u8> {
    let mut locator = VERSION.to_le_bytes().to_vec();
    locator.push(1);
    locator.extend(parent);
    locator.extend([0; 32]);
    locator
}

// An SV peer commonly suppresses an announcement back to the peer that sent
// it the block. Query the active branch without repeatedly publishing work.
struct SolutionReadback {
    parent: Hash,
    hash: Hash,
    height: u32,
    attempts: usize,
    reported_active: bool,
    next: tokio::time::Instant,
}

impl SolutionReadback {
    fn new(parent: Hash, hash: Hash, height: u32) -> Self {
        Self {
            parent,
            hash,
            height,
            attempts: 0,
            reported_active: false,
            next: tokio::time::Instant::now() + Duration::from_secs(1),
        }
    }

    fn due(&mut self, now: tokio::time::Instant) -> bool {
        // An immediate first read can race the receiver's block validation.
        const RETRY_DELAYS: [u64; 4] = [2, 4, 8, 16];
        if self.attempts > RETRY_DELAYS.len() || now < self.next {
            return false;
        }
        if let Some(delay) = RETRY_DELAYS.get(self.attempts) {
            self.next = now + Duration::from_secs(*delay);
        }
        self.attempts += 1;
        true
    }
}
fn inventory(kind: u32, hash: Hash) -> Vec<u8> {
    let mut p = vec![1];
    p.extend(kind.to_le_bytes());
    p.extend(hash);
    p
}
fn items(mut raw: &[u8]) -> Result<Vec<(u32, Hash)>> {
    let n = wire::varint(&mut raw)?;
    ensure!(
        n <= 50000 && raw.len() == 36 * n,
        "invalid inventory length"
    );
    raw.as_chunks::<36>()
        .0
        .iter()
        .map(|e| Ok((u32::from_le_bytes(e[..4].try_into()?), e[4..].try_into()?)))
        .collect()
}
fn block_height(raw: &[u8]) -> Result<u32> {
    let mut r = raw;
    wire::take(&mut r, 80)?;
    ensure!(wire::varint(&mut r)? > 0, "empty block");
    wire::take(&mut r, 4)?;
    ensure!(wire::varint(&mut r)? == 1, "coinbase inputs");
    let prev = wire::take(&mut r, 36)?;
    ensure!(
        prev[..32] == [0; 32] && prev[32..] == [255; 4],
        "missing coinbase"
    );
    let n = wire::varint(&mut r)?;
    let s = wire::take(&mut r, n)?;
    let op = *s.first().context("missing height")?;
    if (0x51..=0x60).contains(&op) {
        return Ok((op - 0x50) as u32);
    }
    ensure!(
        (1..=5).contains(&op) && s.len() > op as usize,
        "invalid coinbase height"
    );
    let mut h = [0; 8];
    h[..op as usize].copy_from_slice(&s[1..=op as usize]);
    let height = u32::try_from(u64::from_le_bytes(h))?;
    ensure!(
        s.starts_with(&wire::height_push(height)),
        "noncanonical coinbase height"
    );
    Ok(height)
}
/// Keep a bounded set of live SV paths. Each failed session frees its slot;
/// the next dial uses another address supplied by the pinned reference seeds.
pub async fn discover(
    state: Arc<Mutex<Mining>>,
    events: mpsc::Sender<Event>,
    explicit: Vec<SocketAddr>,
) -> Result<()> {
    const SEEDS: [&str; 3] = [
        "seed.bitcoinsv.io",
        "seed.satoshisvision.network",
        "seed.bitcoinseed.directory",
    ];
    let mut pool = tokio::task::JoinSet::new();
    let mut in_use: std::collections::HashSet<_> = explicit.into_iter().collect();
    let mut remaining = std::collections::VecDeque::new();
    let mut refresh = tokio::time::interval(Duration::from_secs(300));
    let mut dial = tokio::time::interval(Duration::from_secs(3));
    for timer in [&mut refresh, &mut dial] {
        timer.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    }
    loop {
        tokio::select! {
            _=events.closed()=>return Ok(()),
            _=refresh.tick()=>{
                let results=futures::future::join_all(SEEDS.map(|seed|tokio::time::timeout(Duration::from_secs(6),tokio::net::lookup_host((seed,8333))))).await;
                let mut unique=std::collections::HashSet::new();
                for peers in results.into_iter().flatten().flatten(){
                    unique.extend(peers.filter(|addr|crate::peer::public_ip(addr.ip()) && !in_use.contains(addr)));
                }
                remaining=unique.into_iter().take(128).collect();
            }
            _=dial.tick(), if pool.len()<4 && !remaining.is_empty()=>{
                let addr=remaining.pop_front().expect("pending seed address");
                if state.lock().await.directives.peer_banned(&addr.ip()) || !in_use.insert(addr){continue;}
                let (s,e)=(state.clone(),events.clone());
                pool.spawn(async move{(addr,session(addr,false,&s,&e).await)});
            }
            Some(outcome)=pool.join_next(),if !pool.is_empty()=>{
                let (addr,result)=outcome?;
                in_use.remove(&addr);
                events.send(Event::Disconnected(format!("sv:{addr}"))).await?;
                if let Err(e)=result{
                    if let Some(gap)=e.downcast_ref::<IncompleteBody>(){events.send(Event::Gap { hash: gap.hash.clone(), reason: e.to_string() }).await?;}
                    eprintln!("discovered SV peer {addr} disconnected: {e}");
                }
            }
        }
    }
}

pub async fn run(
    addr: SocketAddr,
    regtest: bool,
    state: Arc<Mutex<Mining>>,
    events: mpsc::Sender<Event>,
) -> Result<()> {
    loop {
        if !state.lock().await.directives.peer_banned(&addr.ip()) {
            if let Err(e) = session(addr, regtest, &state, &events).await {
                if let Some(gap) = e.downcast_ref::<IncompleteBody>() {
                    events
                        .send(Event::Gap {
                            hash: gap.hash.clone(),
                            reason: e.to_string(),
                        })
                        .await?;
                }
                eprintln!("SV peer {addr} disconnected: {e}");
            }
            events
                .send(Event::Disconnected(format!("sv:{addr}")))
                .await?;
        }
        tokio::time::sleep(Duration::from_secs(3)).await;
    }
}
async fn session(
    addr: SocketAddr,
    regtest: bool,
    state: &Arc<Mutex<Mining>>,
    events: &mpsc::Sender<Event>,
) -> Result<()> {
    let stream = tokio::time::timeout(Duration::from_secs(6), TcpStream::connect(addr)).await??;
    let (mut r, mut w) = stream.into_split();
    let height = state.lock().await.height_hint();
    send(&mut w, regtest, "version", &version(height)).await?;
    let (tx, mut rx) = mpsc::channel(2);
    // The reader owns each entire frame. A timer firing between header and
    // payload cannot cancel a partial read and lose framing.
    let reader = tokio::spawn(async move {
        loop {
            let msg = receive(&mut r, regtest, crate::mining::body_limit()).await;
            let failed = msg.is_err();
            if tx.send(msg).await.is_err() || failed {
                break;
            }
        }
    });
    struct Abort(tokio::task::JoinHandle<()>);
    impl Drop for Abort {
        fn drop(&mut self) {
            self.0.abort();
        }
    }
    let _guard = Abort(reader);
    let mut prefers_headers = false;
    let mut advertised = None;
    let mut readback: Option<SolutionReadback> = None;
    let mut last_repair = tokio::time::Instant::now();
    let mut handshake = false;
    let handshake_deadline = tokio::time::Instant::now() + Duration::from_secs(15);
    let mut saw_version = false;
    let mut clock_sample = None;
    let mut peer_version = 0;
    let mut probe = None;
    let mut solved = state.lock().await.subscribe_solutions();
    let mut due = tokio::time::interval(Duration::from_secs(1));
    loop {
        tokio::select! {
            _=tokio::time::sleep_until(handshake_deadline), if !handshake=>{
                anyhow::bail!("SV peer did not complete its handshake")
            }
            _=async { tokio::select! { _=due.tick()=>{}, _=solved.changed()=>{} } }=>{
                let s=state.lock().await;
                ensure!(!s.directives.peer_banned(&addr.ip()),"peer banned");
                let (missing, locator) = if handshake && last_repair.elapsed()>=Duration::from_secs(3) {
                    last_repair=tokio::time::Instant::now();
                    (s.missing_recent(), s.repair_locator())
                } else { (Vec::new(),Vec::new()) };
                let offer = if handshake { s.relayable_solution() } else { None };
                drop(s);
                if !locator.is_empty() {
                    let mut payload=VERSION.to_le_bytes().to_vec();
                    wire::put_varint(&mut payload,locator.len());
                    for id in locator { payload.extend(id); }
                    payload.extend([0;32]);
                    send(&mut w,regtest,"getheaders",&payload).await?;
                }
                for (id,_) in missing{send(&mut w,regtest,"getdata",&inventory(2,id)).await?;}
                if handshake {if let Some(hash)=offer{
                    if advertised!=Some(hash){
                        let hash_name = wire::display_hash(hash);
                        let Some((_, height, raw)) = state.lock().await.requested_solution(&hash_name, false) else {continue};
                        if prefers_headers {
                            let mut payload=vec![1];payload.extend(&raw[..80]);payload.push(0);
                            send(&mut w,regtest,"headers",&payload).await?;
                        } else {send(&mut w,regtest,"inv",&inventory(2,hash)).await?;}
                        let parent = raw[4..36].try_into()?;
                        if raw.len() > u32::MAX as usize && peer_version < EXTENDED_PAYLOAD_VERSION {
                            advertised = Some(hash);
                            events.send(Event::Delivery{peer:format!("sv:{addr}"),hash:hash_name,direction:"peer_lacks_extended_framing"}).await?;
                            continue;
                        }
                        send_block(&mut w,regtest,addr,state,&hash_name,raw).await?;
                        advertised=Some(hash);
                        readback = Some(SolutionReadback::new(parent, hash, height));
                        events.send(Event::Delivery{peer:format!("sv:{addr}"),hash:wire::display_hash(hash),direction:"sent_body"}).await?;
                    } else if let Some(check) = &mut readback {
                        if check.hash == hash && check.due(tokio::time::Instant::now()) {
                            probe = Some((check.parent, hash, check.height));
                            send(&mut w, regtest, "getheaders", &header_locator(check.parent)).await?;
                            events.send(Event::Delivery {
                                peer: format!("sv:{addr}"), hash: wire::display_hash(hash),
                                direction: "requested_active_headers",
                            }).await?;
                        }
                    }
                }else{advertised=None;readback=None;}}
            }
            incoming=rx.recv()=>{
                let (cmd,body)=incoming.context("SV connection closed")??;
                match cmd.as_str(){
                    "version"=>{ensure!(!saw_version && body.len()>=80,"invalid SV handshake");saw_version=true;peer_version=i32::from_le_bytes(body[..4].try_into()?);
                        clock_sample=crate::mining::version_clock(i64::from_le_bytes(body[12..20].try_into()?));send(&mut w,regtest,"verack",&[]).await?;}
                    "verack"=>{ensure!(saw_version && !handshake && body.is_empty(),"unexpected verack");handshake=true;state.lock().await.record_peer_clock(addr,clock_sample);
                        events.send(Event::Connected(format!("sv:{addr}"))).await?;
                        probe=state.lock().await.header_probe();
                        if let Some((parent,_,_))=probe {
                            send(&mut w, regtest, "getheaders", &header_locator(parent)).await?;
                        }}
                    "sendheaders" if handshake && body.is_empty()=>{prefers_headers=true;}
                    "getheaders" if handshake=>send_headers_reply(&mut w,regtest,state,&body).await?,
                    "headers" if handshake=>{
                        let mut input=&body[..];let n=wire::varint(&mut input)?;ensure!(n<=2000,"too many peer headers");
                        let mut headers=Vec::with_capacity(n);
                        let mut last=None;
                        for i in 0..n {
                            let h=wire::take(&mut input,80)?;ensure!(wire::varint(&mut input)?==0,"headers has transactions");
                            let id=hash256(h);
                            if i==0 && n==1 && probe.is_some_and(|(parent,tip,_)| h[4..36]==parent && id==tip) {
                                last=probe.map(|(_,tip,height)|(tip,height));
                            }
                            headers.push(h.try_into()?);
                        }
                        probe=None;
                        ensure!(input.is_empty(),"trailing header bytes");
                        let req=state.lock().await.repair_headers(&headers)?;
                        if let Some((tip,height))=last {
                            if let Some(check) = readback.as_mut().filter(|r| r.hash == tip) {
                                check.reported_active = true;
                                if req.is_empty() { readback = None; }
                            }
                            events.send(Event::Status{peer:format!("sv:{addr}"),height,hash:wire::display_hash(tip)}).await?;
                        }
                        for id in req{send(&mut w,regtest,"getdata",&inventory(2,id)).await?;}
                    }
                    "ping" if body.len()==8=>send(&mut w,regtest,"pong",&body).await?,
                    "inv" if handshake=>{let mut req=Vec::new();let mut count=0;
                        {
                            let entries = items(&body)?;
                            let s = state.lock().await;
                            let mut unique = std::collections::HashSet::new();
                            for (kind, hash) in entries {
                                if (kind == 2 || (kind == 1 && !s.knows_offer(&hash))) && unique.insert((kind, hash)) {
                                    req.extend(kind.to_le_bytes()); req.extend(hash); count += 1;
                                }
                            }
                        }
                        if count>0{let mut packed=Vec::new();wire::put_varint(&mut packed,count);packed.extend(req);send(&mut w,regtest,"getdata",&packed).await?;}
                    }
                    "tx" if handshake=>{state.lock().await.peer_tx(crate::mining::TxSource::Sv, &body);}
                    "block" if handshake=>{
                        let header = body.get(..80).context("short SV block header")?;
                        let hash = hash256(header);
                        let parent: Hash = header[4..36].try_into()?;
                        let (current, linked) = {
                            let state = state.lock().await;
                            (state.height_hint(), state.linked_height(hash, parent))
                        };
                        let height = match (regtest, linked) {
                            (true, Some(height)) => height,
                            _ => match block_height(&body) {
                                Ok(height) => height,
                                Err(_) if regtest => {
                                    events.send(Event::Gap {
                                        hash: Some(wire::display_hash(hash)),
                                        reason: "awaiting regtest block height from linked ancestry".into(),
                                    }).await?;
                                    continue;
                                }
                                Err(error) => return Err(error),
                            }
                        };
                        if current>0 && height>current.saturating_add(1) && height<=current.saturating_add(24) {
                            send(&mut w,regtest,"getdata",&inventory(2,parent)).await?;
                        }
                        let hash=wire::display_hash(hash);let (ack,done)=tokio::sync::oneshot::channel();
                        events.send(Event::Block{height,source:format!("sv:{addr}"),raw:body,validated:ack}).await?;
                        if done.await.unwrap_or(false) {
                            if readback.as_ref().is_some_and(|r| r.reported_active && wire::display_hash(r.hash) == hash) {
                                readback = None;
                            }
                            events.send(Event::Delivery{peer:format!("sv:{addr}"),hash,direction:"received_body"}).await?;
                        }
                    }
                    "getdata" if handshake=>{
                        for (kind,id) in items(&body)?{
                            let hash = wire::display_hash(id);
                            let s = state.lock().await;
                            ensure!(!s.directives.peer_banned(&addr.ip()), "peer banned");
                            if kind == 2 {
                                if let Some((_, _, raw)) = s.requested_solution(&hash, false) {
                                    drop(s);
                                    if raw.len() > u32::MAX as usize && peer_version < EXTENDED_PAYLOAD_VERSION {
                                        send(&mut w, regtest, "notfound", &inventory(kind,id)).await?;
                                        continue;
                                    }
                                    send_block(&mut w, regtest, addr, state, &hash, raw).await?;
                                    events.send(Event::Delivery {
                                        peer: format!("sv:{addr}"), hash, direction: "sent_body",
                                    }).await?;
                                    continue;
                                }
                            }
                            drop(s);
                            send(&mut w, regtest, "notfound", &inventory(kind,id)).await?;
                        }
                    }
                    _=>{}
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn active_header_readback_has_five_timed_attempts_per_solution() {
        let mut check = SolutionReadback::new([1; 32], [2; 32], 18);
        let first = check.next;
        for (seconds, expected) in [
            (0, true),
            (1, false),
            (2, true),
            (5, false),
            (6, true),
            (13, false),
            (14, true),
            (29, false),
            (30, true),
            (600, false),
        ] {
            assert_eq!(check.due(first + Duration::from_secs(seconds)), expected);
        }
        let locator = header_locator(check.parent);
        assert_eq!(locator[4], 1);
        assert_eq!(locator[5..37], check.parent);
        assert_eq!(locator[37..], [0; 32]);
    }
}

#[cfg(test)]
mod frame_tests {
    use super::*;

    #[test]
    fn sv_reference_extends_only_lengths_above_uint32_max() {
        let max = u64::from(u32::MAX);
        let regular = frame_header(true, "block", max, [1, 2, 3, 4]);
        assert_eq!(regular.len(), 24);
        assert_eq!(command(&regular[4..16]).unwrap(), "block");
        assert_eq!(&regular[16..20], &u32::MAX.to_le_bytes());
        assert_eq!(&regular[20..24], &[1, 2, 3, 4]);
        let extended = frame_header(true, "block", max + 1, [1, 2, 3, 4]);
        assert_eq!(extended.len(), 44);
        assert_eq!(command(&extended[4..16]).unwrap(), "extmsg");
        assert_eq!(&extended[16..20], &u32::MAX.to_le_bytes());
        assert_eq!(&extended[20..24], &[0; 4]);
        assert_eq!(command(&extended[24..36]).unwrap(), "block");
        assert_eq!(&extended[36..44], &(max + 1).to_le_bytes());
    }

    #[tokio::test]
    async fn quiet_peer_can_start_an_extended_frame_after_the_header_budget() {
        let budget = Duration::from_millis(40);
        let (mut r, mut w) = tokio::io::duplex(64);
        let size = u64::from(u32::MAX) + 1;
        let (header, ()) = tokio::join!(receive_header(&mut r, true, budget), async {
            tokio::time::sleep(budget * 2).await;
            w.write_all(&frame_header(true, "block", size, [0; 4]))
                .await
                .unwrap();
        });
        assert_eq!(header.unwrap(), ("block".into(), size, None));
    }

    #[tokio::test]
    async fn incomplete_standard_and_extended_headers_expire() {
        let header = frame_header(true, "block", u64::from(u32::MAX) + 1, [0; 4]);
        for (length, message) in [
            (1, "SV frame header timed out"),
            (30, "SV extended frame header timed out"),
        ] {
            let (mut r, mut w) = tokio::io::duplex(64);
            w.write_all(&header[..length]).await.unwrap();
            let err = tokio::time::timeout(
                Duration::from_secs(2),
                receive_header(&mut r, true, Duration::from_millis(30)),
            )
            .await
            .unwrap()
            .unwrap_err();
            assert_eq!(err.to_string(), message);
        }
    }

    #[tokio::test]
    async fn a_pending_header_reply_releases_the_mining_state() {
        let state = Mutex::new(Mining::new(true, vec![0x51]).unwrap());
        let (mut reader, mut writer) = tokio::io::duplex(1);
        let locator = header_locator([7; 32]);
        let (sent, ()) = tokio::join!(
            send_headers_reply(&mut writer, true, &state, &locator),
            async {
                let mut reply = vec![reader.read_u8().await.unwrap()];
                assert!(state.try_lock().is_ok());
                let mut rest = [0; 24];
                reader.read_exact(&mut rest).await.unwrap();
                reply.extend(rest);
                let (command, body) = receive(&mut reply.as_slice(), true, 128).await.unwrap();
                assert_eq!(command, "headers");
                assert_eq!(&*body, &[0]);
            }
        );
        sent.unwrap();
    }

    #[tokio::test]
    async fn healthy_partial_writes_can_span_several_idle_intervals() {
        let (mut sender, mut receiver) = tokio::io::duplex(1);
        let idle = Duration::from_millis(300);
        let start = tokio::time::Instant::now();
        let (sent, bytes) = tokio::join!(write_progress(&mut sender, &[42; 16], idle), async {
            let mut bytes = [0; 16];
            for byte in &mut bytes {
                *byte = receiver.read_u8().await.unwrap();
                tokio::time::sleep(Duration::from_millis(30)).await;
            }
            bytes
        });
        sent.unwrap();
        assert_eq!(bytes, [42; 16]);
        assert!(start.elapsed() > idle);
    }

    #[tokio::test]
    async fn interrupted_body_is_attributed_only_after_its_entire_header_arrives() {
        let raw = [71; 128];
        for size in [29, 80, 100] {
            let mut input = header_for(true, "block", &raw);
            input.extend_from_slice(&raw[..size]);
            let gap = receive(&mut &input[..], true, 2048)
                .await
                .unwrap_err()
                .downcast::<IncompleteBody>()
                .unwrap();
            assert_eq!(
                gap.hash,
                (size >= 80).then(|| wire::display_hash(hash256(&raw[..80])))
            );
            assert!(gap.reason.contains("truncated SV block"));
        }
        let (mut reader, mut writer) = tokio::io::duplex(7);
        let (send_result, received) = tokio::join!(
            send(&mut writer, true, "block", &raw),
            receive(&mut reader, true, 2048),
        );
        send_result.unwrap();
        let (name, body) = received.unwrap();
        assert_eq!(name, "block");
        assert_eq!(&*body, &raw);
    }
}
