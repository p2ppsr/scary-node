//! Direct Stratum V1 jobs for operator-owned equipment on a trusted network.
use super::{now, subsidy, wire, Hash, Mining};
use anyhow::{ensure, Context, Result};
use bsv::primitives::hash::hash256;
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;
use tokio::io::{AsyncBufRead, AsyncBufReadExt, AsyncWrite, AsyncWriteExt, BufReader};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{mpsc, Mutex, Semaphore};

const VERSION: u32 = 0x20000000;
const MAIN_SHARE_BITS: u32 = 0x1b00ffff; // difficulty 65,536
const MAX_LINE: usize = 4096;

pub(super) struct Job {
    revision: u64,
    parent_epoch: u64,
    height: u32,
    previous: Hash, // SHA digest order, as serialized into the header
    bits: u32,
    time: u32,
    prefix: Vec<u8>,
    suffix: Vec<u8>,
    branches: Vec<Hash>,
    transactions: usize,
    pool_commitment: Hash,
    directive_sequence: u32,
}

impl Job {
    fn from_state(state: &Mining) -> Result<Option<Self>> {
        if state.job_wait_reason().is_some() {
            return Ok(None);
        }
        let parent = state.headers.back().context("no parent")?;
        let height = state
            .height
            .context("no tip")?
            .checked_add(1)
            .context("height overflow")?;
        let bits = state.next_bits()?;
        let time = now()?.max(
            state
                .median_time()
                .checked_add(1)
                .context("timestamp overflow")?,
        );
        ensure!(
            time <= state.candidate_time_max()?,
            "median-time floor is in the future"
        );
        let fees = state
            .pending
            .values()
            .try_fold(0u64, |n, entry| n.checked_add(entry.fee))
            .context("fees overflow")?;
        let reward = subsidy(height, state.regtest)
            .checked_add(fees)
            .context("coinbase overflow")?;
        let (prefix, suffix) =
            coinbase_parts(height, reward, &state.payout, &state.mining_identity);
        let mut leaves = vec![[0; 32]];
        let ordered = state.ordered_pending();
        leaves.extend(ordered.iter().map(|(id, _)| *id));
        let (_, branches) = wire::merkle(leaves)?;
        let job = Self {
            revision: state.revision,
            parent_epoch: state.parent_epoch,
            height,
            previous: hash256(parent),
            bits,
            time,
            prefix,
            suffix,
            branches,
            transactions: ordered.len(),
            pool_commitment: *state
                .pending_prefixes
                .last()
                .context("missing pending commitment")?,
            directive_sequence: state.applied_sequence,
        };
        let (header, coinbase) = job.work([0; 4], [0; 4], time, 0);
        state.verify_own_block(height, &job.block(&header, &coinbase, &ordered), false)?;
        Ok(Some(job))
    }

    fn eligible(&self, state: &Mining) -> bool {
        self.parent_epoch == state.parent_epoch
            && (self.transactions == 0
                || state.pending_prefixes.get(self.transactions - 1) == Some(&self.pool_commitment))
    }

    fn id(&self) -> String {
        format!("{:x}", self.revision)
    }

    fn notification(&self, clean: bool) -> Value {
        // CGMiner concatenates these fields into its word-swapped work buffer.
        // The raw digest is therefore swapped *within* each 32-bit word here.
        let mut worker_prev = self.previous;
        for word in worker_prev.as_chunks_mut::<4>().0.iter_mut() {
            word.reverse();
        }
        json!({"id":null,"method":"mining.notify","params":[self.id(),hex::encode(worker_prev),
            hex::encode(&self.prefix),hex::encode(&self.suffix),
            self.branches.iter().map(hex::encode).collect::<Vec<_>>(),format!("{VERSION:08x}"),
            format!("{:08x}",self.bits),format!("{:08x}",self.time),clean]})
    }

    fn work(
        &self,
        extranonce1: [u8; 4],
        extranonce2: [u8; 4],
        time: u32,
        nonce: u32,
    ) -> ([u8; 80], Vec<u8>) {
        let mut coinbase = self.prefix.clone();
        coinbase.extend(extranonce1);
        coinbase.extend(extranonce2);
        coinbase.extend(&self.suffix);
        let root = self
            .branches
            .iter()
            .fold(hash256(&coinbase), |left, &right| wire::joined(left, right));
        let mut header = [0; 80];
        header[0..4].copy_from_slice(&VERSION.to_le_bytes());
        header[4..36].copy_from_slice(&self.previous);
        header[36..68].copy_from_slice(&root);
        header[68..72].copy_from_slice(&time.to_le_bytes());
        header[72..76].copy_from_slice(&self.bits.to_le_bytes());
        header[76..80].copy_from_slice(&nonce.to_le_bytes());
        (header, coinbase)
    }

    fn block(
        &self,
        header: &[u8; 80],
        coinbase: &[u8],
        ordered: &[(Hash, &super::Entry)],
    ) -> Vec<u8> {
        let mut raw = header.to_vec();
        wire::put_varint(&mut raw, self.transactions + 1);
        raw.extend(coinbase);
        for (_, entry) in ordered.iter().take(self.transactions) {
            raw.extend_from_slice(&entry.raw);
        }
        raw
    }
}

impl Mining {
    pub(super) fn job_wait_reason(&self) -> Option<&'static str> {
        if !self.observation_window_ready() {
            Some("observation window unready")
        } else if self.solved_on_tip {
            Some("awaiting independent observation after solving on the selected parent")
        } else if self.payout.is_empty() {
            Some("payout script absent")
        } else if self.pending.is_empty() {
            Some("no eligible pending transactions")
        } else {
            None
        }
    }

    fn worker_job(&mut self) -> Result<Option<Arc<Job>>> {
        if !self.observation_window_ready() || self.solved_on_tip {
            return Ok(None);
        }
        if let Some(job) = &self.worker_template {
            if job.revision == self.revision
                && job.directive_sequence == self.applied_sequence
                && job.eligible(self)
            {
                return Ok(Some(job.clone()));
            }
        }
        self.worker_template = Job::from_state(self)?.map(Arc::new);
        Ok(self.worker_template.clone())
    }

    pub fn shadow_decision(&self) -> Result<Value> {
        let tip = self.headers.back().context("shadow tip absent")?;
        let job = Job::from_state(self)?;
        let template=job.map(|j|{
            let (header,coinbase)=j.work([0;4],[0;4],j.time,0);
            json!({"height":j.height,"bits":j.bits,"header_hex":hex::encode(header),"coinbase":hex::encode(coinbase),
                   "transactions":self.ordered_pending().into_iter().map(|(id,_)|wire::display_hash(id)).collect::<Vec<_>>()})
        });
        Ok(
            json!({"observed_at":now()?,"height":self.height,"hash":wire::display_hash(hash256(tip)),
            "header_hex":hex::encode(tip),"previous":wire::display_hash(tip[4..36].try_into()?),"bits":super::bits(tip),"expected_next_bits":self.next_bits().ok(),
            "ready":self.observation_window_ready(),"coverage":self.headers.len(),"pending":self.pending.len(),
            "directives":self.directives.status(),"unsigned_candidate":template}),
        )
    }
}
fn coinbase_parts(
    height: u32,
    value: u64,
    payout: &[u8],
    identity: &[u8; 32],
) -> (Vec<u8>, Vec<u8>) {
    let height_push = wire::height_push(height);
    let tag = b"/scary-node/";
    let script_len = height_push.len() + 1 + 8 + 1 + tag.len() + 1 + identity.len();
    let mut prefix = 1u32.to_le_bytes().to_vec();
    prefix.push(1);
    prefix.extend([0; 32]);
    prefix.extend(u32::MAX.to_le_bytes());
    wire::put_varint(&mut prefix, script_len);
    prefix.extend(height_push);
    prefix.push(8); // push the concatenated connection and worker extranonces
    let mut suffix = vec![tag.len() as u8];
    suffix.extend(tag);
    suffix.push(identity.len() as u8);
    suffix.extend(identity);
    suffix.extend(u32::MAX.to_le_bytes());
    suffix.push(1);
    suffix.extend(value.to_le_bytes());
    wire::put_varint(&mut suffix, payout.len());
    suffix.extend(payout);
    suffix.extend(0u32.to_le_bytes());
    (prefix, suffix)
}

pub(super) fn framing_bytes(height: u32, payout: &[u8], total_transactions: usize) -> usize {
    let (prefix, suffix) = coinbase_parts(height, 0, payout, &[0; 32]);
    let mut count = Vec::new();
    wire::put_varint(&mut count, total_transactions);
    80 + count.len() + prefix.len() + 8 + suffix.len()
}

fn four_hex(s: &Value) -> Result<[u8; 4]> {
    let value = s.as_str().context("field must be a hex string")?;
    ensure!(value.len() == 8, "field requires exactly four bytes of hex");
    let mut bytes = [0; 4];
    hex::decode_to_slice(value, &mut bytes)?;
    Ok(bytes)
}

async fn write_message(stream: &mut (impl AsyncWrite + Unpin), value: Value) -> Result<()> {
    let mut line = serde_json::to_vec(&value)?;
    line.push(b'\n');
    tokio::time::timeout(Duration::from_secs(10), stream.write_all(&line)).await??;
    Ok(())
}

async fn line<R: AsyncBufRead + Unpin>(reader: &mut R, pending: &mut Vec<u8>) -> Result<bool> {
    loop {
        let data = reader.fill_buf().await?;
        if data.is_empty() {
            return Ok(false);
        }
        let end = data.iter().position(|&x| x == b'\n');
        let n = end.unwrap_or(data.len());
        ensure!(pending.len() + n <= MAX_LINE, "Stratum request too long");
        pending.extend(&data[..n]);
        reader.consume(n + usize::from(end.is_some()));
        if end.is_some() {
            return Ok(true);
        }
    }
}

fn share(
    state: &mut Mining,
    job: &Job,
    params: &[Value],
    worker: &str,
    x1: [u8; 4],
    seen: &mut HashSet<(u64, [u8; 4], u32, u32)>,
    solved: &mpsc::Sender<(u32, Vec<u8>)>,
) -> Result<()> {
    ensure!(params.len() == 5, "expected five mining.submit parameters");
    ensure!(params[0].as_str() == Some(worker), "unauthorized worker");
    ensure!(
        params[1].as_str() == Some(&job.id())
            && job.eligible(state)
            && state.tip() == Some(job.previous)
            && state.observation_window_ready()
            && !state.solved_on_tip,
        "stale job"
    );
    let x2 = four_hex(&params[2])?;
    let time = u32::from_be_bytes(four_hex(&params[3])?);
    let nonce = u32::from_be_bytes(four_hex(&params[4])?);
    ensure!(
        time >= job.time && time <= state.candidate_time_max()?,
        "ntime outside job window"
    );
    let share_id = (job.revision, x2, time, nonce);
    ensure!(!seen.contains(&share_id), "duplicate share");
    let (header, coinbase) = job.work(x1, x2, time, nonce);
    let digest = hash256(&header);
    let network = wire::meets_target(digest, job.bits)?;
    let share_bits = if state.regtest {
        job.bits
    } else {
        MAIN_SHARE_BITS
    };
    ensure!(
        network || wire::meets_target(digest, share_bits)?,
        "low-difficulty share"
    );
    ensure!(
        network || seen.len() < 4096,
        "share replay window full; reconnect for a new extranonce"
    );
    if network {
        let block = job.block(&header, &coinbase, &state.ordered_pending());
        state.verify_own_block(job.height, &block, true)?;
        let permit = solved
            .try_reserve()
            .context("publication queue unavailable")?;
        state.solution_body = Some(super::SolvedBody {
            verified_sequence: Some(state.applied_sequence),
            height: job.height,
            hash: digest,
            raw: Arc::from(block.as_slice()),
        });
        let subtree = (!job.branches.is_empty()).then(|| {
            job.branches
                .iter()
                .fold([0xff; 32], |root, branch| wire::joined(root, *branch))
        });
        state
            .lifecycle
            .queue(digest, job.previous, job.height, job.id(), subtree);
        state.solved_on_tip = true;
        state.solved_blocks += 1;
        state.revision += 1;
        if let Err(e) = state.save() {
            state.wait_for_body(format!("solution persistence failed: {e}"));
            return Err(e);
        }
        permit.send((job.height, block));
        state.solution_watch.send_replace(());
    }
    seen.insert(share_id);
    state.accepted_shares += 1;
    Ok(())
}

// A Stratum V1 worker cannot cancel only a subset of its templates. Ask it to
// replace its local set when any job is retired. Keep valid server-side jobs
// so solutions already in flight can still be checked against their exact work.
fn refresh_jobs(
    state: &Mining,
    jobs: &mut HashMap<u64, Arc<Job>>,
    seen: &mut HashSet<(u64, [u8; 4], u32, u32)>,
) -> bool {
    let issued = jobs.len();
    jobs.retain(|_, job| job.eligible(state));
    if jobs.len() != issued {
        seen.retain(|(revision, ..)| jobs.contains_key(revision));
        return true;
    }
    jobs.is_empty()
}

async fn session(
    stream: TcpStream,
    serial: u32,
    state: Arc<Mutex<Mining>>,
    solved: mpsc::Sender<(u32, Vec<u8>)>,
    password: Option<Arc<str>>,
) -> Result<()> {
    let (input, mut output) = stream.into_split();
    let mut reader = BufReader::new(input);
    let mut partial = Vec::new();
    let x1 = serial.to_be_bytes();
    let mut subscribed = false;
    let mut worker = None::<String>;
    // Keep every issued valid commitment. At the connection bound, coalesce
    // further refreshes until eligibility changes or the parent advances.
    const RETAINED_JOBS: usize = 4096;
    let mut jobs = HashMap::<u64, Arc<Job>>::new();
    let mut seen = HashSet::new();
    let mut announced_revision = None;
    let mut refresh = tokio::time::interval(Duration::from_millis(150));
    refresh.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    loop {
        let received_request = tokio::select! {
            result = line(&mut reader, &mut partial) => {
                if !result? { return Ok(()); }
                true
            }
            _ = refresh.tick(), if subscribed && worker.is_some() => false,
        };
        if received_request {
            let msg: Value = serde_json::from_slice(&partial)?;
            partial.clear();
            let id = msg.get("id").cloned().unwrap_or(Value::Null);
            let args = msg
                .get("params")
                .and_then(Value::as_array)
                .map(|v| &v[..])
                .unwrap_or(&[]);
            let mut code = 20;
            let result: Result<Value> = match msg["method"].as_str().unwrap_or("") {
                "mining.subscribe" => {
                    subscribed = true;
                    Ok(json!([
                        [
                            ["mining.set_difficulty", hex::encode(x1)],
                            ["mining.notify", hex::encode(x1)]
                        ],
                        hex::encode(x1),
                        4
                    ]))
                }
                "mining.authorize" => {
                    if let Some(name) = args
                        .first()
                        .and_then(Value::as_str)
                        .filter(|s| subscribed && !s.is_empty() && s.len() <= 128)
                    {
                        if password.as_ref().is_some_and(|p| {
                            !args
                                .get(1)
                                .and_then(Value::as_str)
                                .is_some_and(|v| crate::credentials_match(v, p))
                        }) {
                            code = 24;
                            Err(anyhow::anyhow!("worker authentication failed"))
                        } else if worker.as_deref().is_some_and(|existing| existing != name) {
                            code = 24;
                            Err(anyhow::anyhow!(
                                "worker is already authorized on this session"
                            ))
                        } else {
                            worker = Some(name.to_owned());
                            Ok(json!(true))
                        }
                    } else {
                        code = 24;
                        Err(anyhow::anyhow!("subscribe first and supply a worker name"))
                    }
                }
                "mining.configure" => {
                    let mut response = serde_json::Map::new();
                    if let Some(extensions) = args.first().and_then(Value::as_array) {
                        for ext in extensions.iter().filter_map(Value::as_str) {
                            response.insert(ext.into(), Value::Bool(false));
                        }
                    }
                    Ok(Value::Object(response))
                }
                "mining.extranonce.subscribe" => Ok(json!(true)),
                "mining.submit" => {
                    let id = args.get(1).and_then(Value::as_str);
                    let job = id
                        .and_then(|text| u64::from_str_radix(text, 16).ok())
                        .and_then(|revision| jobs.get(&revision));
                    if let (Some(j), Some(w)) = (job, &worker) {
                        let mut s = state.lock().await;
                        let reply = share(&mut s, j, args, w, x1, &mut seen, &solved);
                        if let Err(e) = &reply {
                            code = match e.to_string().as_str() {
                                "stale job" => 21,
                                "duplicate share" => 22,
                                "low-difficulty share" => 23,
                                "unauthorized worker" => 24,
                                _ => 20,
                            };
                        }
                        reply.map(|_| json!(true))
                    } else {
                        code = 21;
                        Err(anyhow::anyhow!("no active job"))
                    }
                }
                _ => Err(anyhow::anyhow!("unsupported method")),
            };
            let response = match result {
                Ok(v) => json!({"id":id,"result":v,"error":null}),
                Err(e) => json!({"id":id,"result":null,"error":[code,e.to_string(),null]}),
            };
            write_message(&mut output, response).await?;
        } else {
            let mut s = state.lock().await;
            if !s.observation_window_ready() && !jobs.is_empty() {
                return Ok(());
            }
            let revision = (s.revision, s.observation_window_ready());
            if announced_revision == Some(revision) {
                continue;
            }
            let first = announced_revision.is_none();
            announced_revision = Some(revision);
            let had_jobs = !jobs.is_empty();
            let clean = refresh_jobs(&s, &mut jobs, &mut seen);
            if jobs.len() >= RETAINED_JOBS && !s.solved_on_tip {
                continue;
            }
            let next_job = s.worker_job()?;
            if had_jobs && next_job.is_none() {
                // Stratum V1 has no cancellation without a replacement job.
                return Ok(());
            }
            let regtest = s.regtest;
            drop(s);
            if first {
                let difficulty = if regtest {
                    65535.0 / 8_388_607.0 / 16_777_216.0
                } else {
                    65_536.0
                };
                write_message(
                    &mut output,
                    json!({"id":null,"method":"mining.set_difficulty","params":[difficulty]}),
                )
                .await?;
            }
            if let Some(job) = next_job {
                write_message(&mut output, job.notification(clean)).await?;
                jobs.insert(job.revision, job);
            }
        }
    }
}

pub async fn run_stratum(
    addr: SocketAddr,
    state: Arc<Mutex<Mining>>,
    solved: mpsc::Sender<(u32, Vec<u8>)>,
    password: Option<Arc<str>>,
    mut lease: crate::lease::MiningLease,
) -> Result<()> {
    ensure!(
        !state.lock().await.payout.is_empty(),
        "Stratum requires a payout script"
    );
    let listener = TcpListener::bind(addr).await?;
    let sessions = Arc::new(Semaphore::new(32));
    let mut next = 0u32;
    let mut end = 0u32;
    loop {
        let (stream, _) = listener.accept().await?;
        let Ok(permit) = sessions.clone().try_acquire_owned() else {
            continue;
        };
        if next == end {
            (next, end) = lease.reserve()?;
        }
        next = next.checked_add(1).context("extranonce space exhausted")?;
        let serial = next;
        let shared = state.clone();
        let publisher = solved.clone();
        let password = password.clone();
        tokio::spawn(async move {
            let _permit = permit;
            if let Err(e) = session(stream, serial, shared, publisher, password).await {
                eprintln!("Stratum session ended: {e}");
            }
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn fixed_cgminer_work_buffer_order_and_header_fixture() {
        // CGMiner parse_notify/gen_stratum_work reads the notify prevhash
        // verbatim into its word-flipped buffer and branches as raw SHA digests.
        // This expected header and digest were independently assembled using
        // Python's struct.pack('<III',...) and hashlib.sha256 twice.
        let j=Job{revision:15,parent_epoch:0,height:1,previous:std::array::from_fn(|i| i as u8),
            bits:0x207fffff,time:1_700_000_000,
            prefix:hex::decode("01000000010000000000000000000000000000000000000000000000000000000000000000ffffffff16510408070605").unwrap(),
            suffix:hex::decode("4558414d504c45ffffffff010100000000000000015100000000").unwrap(),
            branches:vec![hash256(b"child")],transactions:0,pool_commitment:[0;32],directive_sequence:0};
        let msg = j.notification(true);
        assert_eq!(
            msg["params"][1],
            "03020100070605040b0a09080f0e0d0c13121110171615141b1a19181f1e1d1c"
        );
        assert_eq!(
            msg["params"][4][0],
            "9acf74bc0bb139da32ea10fc2781b15c52b0ea96d8604bb64264a94de1064ea8"
        );
        let (hdr, _) = j.work(*b"ABCD", *b"EFGH", 1_700_000_000, 2468);
        assert_eq!(hex::encode(hdr),"00000020000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f3b6c32043d90b01404e5d4a053d53fa6d6315e9a97c913281540b52706215ec800f15365ffff7f20a4090000");
        assert_eq!(
            hex::encode(hash256(&hdr)),
            "96e94e5a630b1f03af7fa929b66c2006123270e81106bfff2540bda90acbcfad"
        );
    }

    #[test]
    fn partial_same_parent_retirement_cleans_worker_and_keeps_valid_inflight_work() {
        let mut state = Mining::new(true, vec![0x51]).unwrap();
        let job = |revision: u64, transactions: usize, pool_commitment: Hash| {
            Arc::new(Job {
                revision,
                parent_epoch: state.parent_epoch,
                height: 26,
                previous: [0; 32],
                bits: 0x207fffff,
                time: 1_700_000_000,
                prefix: vec![],
                suffix: vec![],
                branches: vec![],
                transactions,
                pool_commitment,
                directive_sequence: 0,
            })
        };
        let first = job(1, 1, [1; 32]);
        let second = job(2, 2, [2; 32]);
        let mut issued = HashMap::from([(1, first.clone()), (2, second)]);
        let mut seen = HashSet::from([(1, [0; 4], 1, 1), (2, [0; 4], 2, 2)]);
        // Ordinary additional payments preserve both immutable prefixes.
        state.pending_prefixes = vec![[1; 32], [2; 32], [3; 32]];
        assert!(!refresh_jobs(&state, &mut issued, &mut seen));
        // A recheck drops the second payment while preserving the first.
        state.pending_prefixes.truncate(1);
        let clean = refresh_jobs(&state, &mut issued, &mut seen);
        assert_eq!(first.notification(clean)["params"][8], true);
        assert!(issued[&1].eligible(&state));
        assert!(!issued.contains_key(&2));
        assert_eq!(seen, HashSet::from([(1, [0; 4], 1, 1)]));
        state.parent_epoch += 1;
        assert!(refresh_jobs(&state, &mut issued, &mut seen));
        assert!(issued.is_empty() && seen.is_empty());
    }

    #[test]
    fn only_network_target_share_enqueues_and_retires_the_regtest_job() {
        let mut state = Mining::new(true, vec![0x51]).unwrap();
        let mut parent = [0u8; 80];
        parent[72..76].copy_from_slice(&0x207fffffu32.to_le_bytes());
        state.headers.push_back(parent);
        state.height = Some(25);
        let (prefix, suffix) = coinbase_parts(26, 5_000_000_000, &[0x51], &state.mining_identity);
        let time = now().unwrap();
        let job = Job {
            revision: state.revision,
            parent_epoch: state.parent_epoch,
            height: 26,
            previous: state.tip().unwrap(),
            bits: 0x207fffff,
            time,
            prefix,
            suffix,
            branches: vec![],
            transactions: 0,
            pool_commitment: [0; 32],
            directive_sequence: 0,
        };
        let x1 = [0, 0, 0, 1];
        let mut seen = HashSet::new();
        let (publisher, mut result) = mpsc::channel(1);
        let mut valid = None;
        let mut invalid = None;
        for n in 0..1000 {
            let (header, _) = job.work(x1, [0; 4], time, n);
            if wire::meets_target(hash256(&header), job.bits).unwrap() {
                valid = Some(n);
            } else {
                invalid = Some(n);
            }
            if valid.is_some() && invalid.is_some() {
                break;
            }
        }
        let request = |n: u32| {
            vec![
                json!("worker"),
                json!(job.id()),
                json!("00000000"),
                json!(format!("{time:08x}")),
                json!(format!("{n:08x}")),
            ]
        };
        assert!(share(
            &mut state,
            &job,
            &request(invalid.unwrap()),
            "worker",
            x1,
            &mut seen,
            &publisher
        )
        .unwrap_err()
        .to_string()
        .contains("low-difficulty"));
        assert!(result.try_recv().is_err());
        let params = request(valid.unwrap());
        share(
            &mut state, &job, &params, "worker", x1, &mut seen, &publisher,
        )
        .unwrap();
        assert_eq!(state.solved_blocks, 1);
        let (height, bytes) = result.try_recv().unwrap();
        assert_eq!(height, job.height);
        assert!(wire::meets_target(hash256(&bytes[..80]), job.bits).unwrap());
        assert!(
            share(&mut state, &job, &params, "worker", x1, &mut seen, &publisher)
                .unwrap_err()
                .to_string()
                .contains("stale")
        );
        assert!(result.try_recv().is_err());
    }
}
