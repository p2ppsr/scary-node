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
async fn send(
    w: &mut (impl AsyncWrite + Unpin),
    regtest: bool,
    name: &str,
    body: &[u8],
) -> Result<()> {
    let mut cmd = [0u8; 12];
    cmd[..name.len()].copy_from_slice(name.as_bytes());
    let mut header = magic(regtest).to_vec();
    if body.len() >= u32::MAX as usize {
        header.extend(*b"extmsg\0\0\0\0\0\0");
        header.extend(u32::MAX.to_le_bytes());
        header.extend([0; 4]);
        header.extend(cmd);
        header.extend((body.len() as u64).to_le_bytes());
    } else {
        header.extend(cmd);
        header.extend((body.len() as u32).to_le_bytes());
        header.extend(&hash256(body)[..4]);
    }
    tokio::time::timeout(Duration::from_secs(10), async {
        w.write_all(&header).await?;
        w.write_all(body).await?;
        w.flush().await
    })
    .await??;
    Ok(())
}
#[derive(Debug)]
struct IncompleteBody(String);
impl std::fmt::Display for IncompleteBody {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}
impl std::error::Error for IncompleteBody {}

async fn receive(
    r: &mut (impl AsyncRead + Unpin),
    regtest: bool,
    limit: usize,
) -> Result<(String, crate::body::BlockBody)> {
    let mut h = [0u8; 24];
    r.read_exact(&mut h).await?;
    ensure!(h[..4] == magic(regtest), "SV peer network mismatch");
    let mut cmd = command(&h[4..16])?.to_owned();
    let mut n = u32::from_le_bytes(h[16..20].try_into()?) as u64;
    let ext = cmd == "extmsg";
    if ext {
        ensure!(
            n == u32::MAX as u64 && h[20..24] == [0; 4],
            "invalid extmsg header"
        );
        let mut more = [0u8; 20];
        r.read_exact(&mut more).await?;
        cmd = command(&more[..12])?.to_owned();
        n = u64::from_le_bytes(more[12..].try_into()?);
    }
    let cap = if cmd == "block" {
        limit
    } else if cmd == "tx" {
        crate::mining::transaction_limit()
    } else {
        limit.min(32 << 20)
    };
    if cmd == "block" && n > cap as u64 {
        return Err(IncompleteBody(format!(
            "SV body of {n} bytes exceeds configured capacity {cap}"
        ))
        .into());
    }
    ensure!(n <= cap as u64, "SV peer message exceeds local capacity");
    let mut spool = crate::body::Spool::new(cap);
    let mut input = (&mut *r).take(n);
    let mut chunk = vec![0; 64 << 10];
    loop {
        let received = input.read(&mut chunk).await?;
        if received == 0 {
            break;
        }
        if let Err(error) = spool.append(&chunk[..received]) {
            return Err(if cmd == "block" {
                IncompleteBody(format!("SV body spool failed: {error}")).into()
            } else {
                error
            });
        }
    }
    let body = spool.finish().map_err(|e| {
        if cmd == "block" {
            anyhow::Error::new(IncompleteBody(format!("SV body spool failed: {e}")))
        } else {
            e
        }
    })?;
    ensure!(body.len() as u64 == n, "truncated SV peer message");
    if !ext {
        ensure!(hash256(&body)[..4] == h[20..24], "SV message checksum");
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
                    if e.downcast_ref::<IncompleteBody>().is_some(){events.send(Event::Gap(e.to_string())).await?;}
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
                if e.downcast_ref::<IncompleteBody>().is_some() {
                    events.send(Event::Gap(e.to_string())).await?;
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
    send(
        &mut w,
        regtest,
        "version",
        &version(state.lock().await.height_hint()),
    )
    .await?;
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
    let mut last_repair = tokio::time::Instant::now();
    let mut handshake = false;
    let mut saw_version = false;
    let mut probe = None;
    let mut solved = state.lock().await.subscribe_solutions();
    let mut due = tokio::time::interval(Duration::from_secs(1));
    loop {
        tokio::select! {
            _=async { tokio::select! { _=due.tick()=>{}, _=solved.changed()=>{} } }=>{
                let s=state.lock().await;
                ensure!(!s.directives.peer_banned(&addr.ip()),"peer banned");
                if handshake && last_repair.elapsed()>=Duration::from_secs(3) {
                    let missing=s.missing_recent();last_repair=tokio::time::Instant::now();
                    for (id,_) in missing{send(&mut w,regtest,"getdata",&inventory(2,id)).await?;}
                }
                if handshake {if let Some(hash)=s.relayable_solution(){
                    if advertised!=Some(hash){
                        let raw=s.solution_bytes(&hash).expect("selected stored solution");
                        if prefers_headers {
                            let mut payload=vec![1];payload.extend(&raw[..80]);payload.push(0);
                            send(&mut w,regtest,"headers",&payload).await?;
                        } else {send(&mut w,regtest,"inv",&inventory(2,hash)).await?;}
                        send(&mut w,regtest,"block",raw).await?;
                        advertised=Some(hash);
                        drop(s);
                        events.send(Event::Delivery{peer:format!("sv:{addr}"),hash:wire::display_hash(hash),direction:"sent_body"}).await?;
                    }
                }else{advertised=None;}}
            }
            incoming=rx.recv()=>{
                let (cmd,body)=incoming.context("SV connection closed")??;
                match cmd.as_str(){
                    "version"=>{ensure!(!saw_version && body.len()>=80,"invalid SV handshake");saw_version=true;send(&mut w,regtest,"verack",&[]).await?;}
                    "verack"=>{ensure!(saw_version && !handshake && body.is_empty(),"unexpected verack");handshake=true;events.send(Event::Connected(format!("sv:{addr}"))).await?;
                        probe=state.lock().await.header_probe();
                        if let Some((parent,_,_))=probe {
                            let mut locator=VERSION.to_le_bytes().to_vec();
                            locator.push(1);locator.extend(parent);locator.extend([0u8;32]);
                            send(&mut w,regtest,"getheaders",&locator).await?;
                        }}
                    "sendheaders" if handshake && body.is_empty()=>{prefers_headers=true;}
                    "getheaders" if handshake=>{
                        let mut locator=&body[..];wire::take(&mut locator,4)?;
                        let n=wire::varint(&mut locator)?;ensure!(n<=101 && locator.len()==32*(n+1),"invalid header locator");
                        let s=state.lock().await;
                        let mut reply=vec![0];
                        if let Some(id)=s.relayable_solution(){
                            let b=s.solution_bytes(&id).expect("selected stored solution");
                            let stop=&locator[n*32..];
                            if (stop==id || stop==[0;32]) && (locator[..n*32].as_chunks::<32>().0.iter().any(|h|h==&b[4..36]) || (n==0 && stop==id)){
                                reply[0]=1;reply.extend(&b[..80]);reply.push(0);
                            }
                        }
                        send(&mut w,regtest,"headers",&reply).await?;
                    }
                    "headers" if handshake=>{
                        let mut input=&body[..];let n=wire::varint(&mut input)?;ensure!(n<=2000,"too many peer headers");
                        let mut req=vec![];
                        let mut last=None;
                        let mut current=state.lock().await.tip();
                        for i in 0..n {
                            let h=wire::take(&mut input,80)?;ensure!(wire::varint(&mut input)?==0,"headers has transactions");
                            let id=hash256(h);
                            if i==0 && n==1 && probe.is_some_and(|(parent,tip,_)| h[4..36]==parent && id==tip) {
                                last=probe.map(|(_,tip,height)|(tip,height));
                            }
                            if n<=24 && current.is_some_and(|tip|h[4..36]==tip) {
                                req.push(id);current=Some(id);
                            }
                        }
                        probe=None;
                        if let Some((tip,height))=last {
                            events.send(Event::Status{peer:format!("sv:{addr}"),height,hash:wire::display_hash(tip)}).await?;
                        }
                        ensure!(input.is_empty(),"trailing header bytes");
                        for id in req{send(&mut w,regtest,"getdata",&inventory(2,id)).await?;}
                    }
                    "ping" if body.len()==8=>send(&mut w,regtest,"pong",&body).await?,
                    "inv" if handshake=>{let mut req=Vec::new();let mut count=0;
                        for (kind,hash) in items(&body)?{if kind==1 || kind==2{req.extend(kind.to_le_bytes());req.extend(hash);count+=1;}}
                        if count>0{let mut packed=Vec::new();wire::put_varint(&mut packed,count);packed.extend(req);send(&mut w,regtest,"getdata",&packed).await?;}
                    }
                    "tx" if handshake=>{let _=state.lock().await.submit_tx(&body);}
                    "block" if handshake=>{
                        let height=block_height(&body)?;
                        let parent:Hash=body[4..36].try_into()?;
                        let current=state.lock().await.height_hint();
                        if current>0 && height>current.saturating_add(1) && height<=current.saturating_add(24) {
                            send(&mut w,regtest,"getdata",&inventory(2,parent)).await?;
                        }
                        let hash=wire::display_hash(hash256(&body[..80]));let (ack,done)=tokio::sync::oneshot::channel();
                        events.send(Event::Block{height,source:format!("sv:{addr}"),raw:body,validated:ack}).await?;
                        if done.await.unwrap_or(false){events.send(Event::Delivery{peer:format!("sv:{addr}"),hash,direction:"received_body"}).await?;}
                    }
                    "getdata" if handshake=>{
                        for (kind,id) in items(&body)?{
                            let s=state.lock().await;
                            if kind==2 && s.relayable_solution()==Some(id){
                                if let Some(raw)=s.solution_bytes(&id){send(&mut w,regtest,"block",raw).await?;drop(s);events.send(Event::Delivery{peer:format!("sv:{addr}"),hash:wire::display_hash(id),direction:"sent_body"}).await?;}
                            }else {drop(s);send(&mut w,regtest,"notfound",&inventory(kind,id)).await?;}
                        }
                    }
                    _=>{}
                }
            }
        }
    }
}
