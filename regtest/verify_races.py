"""Verify valid-sibling regtest race receipts and calculate finite-sample uncertainty."""
import argparse
import hashlib
import json
import math
import random
import re
import struct
import sys
import tempfile
from collections import Counter
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from pathlib import Path

from prove import decode_block, dsha, txid
from verify_receipt import check_header, coinbase_height, require
from verify_scenario import GENESIS, Ledger, NULL, Signatures, Transaction, indexed, load_record

SIDES = ('witnessed', 'full')
TARGET = 0x7fffff << 232
HARNESS_FILES = {'races.py', 'race_work.py', 'races.compose.yaml', 'race-ingress.conf'}
TERANODE_IMAGE = 'ghcr.io/bsv-blockchain/teranode:v0.16.1@sha256:84971b2863301c7527382768dbe5a094cd8c4d6e2f426f5557d3c12ba114822a'
REFERENCE_IMAGES = {
    'tn1': TERANODE_IMAGE, 'tn2': TERANODE_IMAGE,
    'judge': 'bitcoinsv/bitcoin-sv:1.2.3@sha256:d96fe4786a00e5fc10bc9e0e76a6006376f9472d4eb74d07e4c1e0d019493d33',
    'ingress': 'nginx:1.27.4@sha256:09369da6b10306312cd908661320086bf87fbae1b6b0c49a1f50ba531fef2eab',
    'scary-node': 'scary-node:local',
}


def check_provenance(record, harness_directory):
    harness = record.get('harness_sha256')
    if harness is not None:
        require(isinstance(harness, dict) and set(harness) == HARNESS_FILES, 'incomplete harness manifest')
        for name, saved_hash in harness.items():
            digest = hashlib.sha256((harness_directory / name).read_bytes()).hexdigest()
            require(saved_hash == digest, f'harness hash differs for {name}')
    elif record['seed'] in (1701, 1702) and record['planned_trials'] == 128:
        require(False, 'formal receipt missing harness hashes')

    provenance = record.get('provenance')
    if provenance is None:
        require(not record['complete'] and not record['trials'], 'missing running image and binary records')
        return
    images = provenance['images']
    require(set(images) == set(REFERENCE_IMAGES), 'running image inventory differs from declared lab')
    for service, expected_ref in REFERENCE_IMAGES.items():
        image = images[service]
        require(image['image_reference'] == expected_ref, f'unexpected pinned image for {service}')
        # A platform-specific image ID can differ from the registry's index digest.
        require(re.fullmatch(r'sha256:[0-9a-f]{64}', image['local_image_id']), f'invalid running image ID for {service}')
    require(images['tn1'] == images['tn2'], 'the two Teranodes ran different images')
    require(re.fullmatch(r'[0-9a-f]{64}', provenance['rust_binary_sha256'])
            and re.fullmatch(r'scary-node \d+\.\d+\.\d+-alpha(?:\.\d+)?', provenance['rust_version']),
            'missing observed Rust alpha binary identity')
    require(provenance['judge_version'] == 101020300 and provenance['judge_subversion'] == '/Bitcoin SV:1.2.3/',
            'unexpected running neutral judge version')


def binomial_cdf(k, n, p):
    """Pr[X <= k] for Bin(n,p). A log sum keeps small tail probabilities usable."""
    if k < 0:
        return 0.0
    if k >= n or p == 0:
        return 1.0
    if p == 1:
        return 0.0
    first = math.lgamma(n + 1)
    log_p, log_q = math.log(p), math.log1p(-p)
    return min(1.0, math.fsum(math.exp(first - math.lgamma(i + 1) - math.lgamma(n - i + 1)
                                      + i * log_p + (n - i) * log_q) for i in range(k + 1)))


def binomial_lower(successes, n, tail):
    if successes == 0:
        return 0.0
    lo, hi = 0.0, 1.0
    for _ in range(70):
        mid = (lo + hi) / 2
        if binomial_cdf(n - successes, n, 1 - mid) < tail:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def exact_interval(successes, n, alpha=.05):
    """Two-sided equal-tailed Clopper–Pearson binomial interval."""
    require(isinstance(n, int) and n > 0 and isinstance(successes, int) and 0 <= successes <= n,
            'invalid binomial counts')
    require(0 < alpha < 1, 'invalid uncertainty level')
    return [binomial_lower(successes, n, alpha / 2), 1 - binomial_lower(n - successes, n, alpha / 2)]


def quantile(values, probability):
    ordered = sorted(values)
    if not ordered:
        return None
    at = (len(ordered) - 1) * probability
    low = int(at)
    return ordered[low] + (at - low) * (ordered[math.ceil(at)] - ordered[low])


def counts(outcomes):
    # Rows preserve both miners' first-tick discoveries and the winning label.
    found = {side: 0 for side in SIDES}
    stale = {side: 0 for side in SIDES}
    for witnessed, full, winner in outcomes:
        for side, solved in zip(SIDES, (witnessed, full)):
            found[side] += solved
            stale[side] += solved and winner != side
    return found, stale


def rate_difference(outcomes):
    found, stale = counts(outcomes)
    if not all(found.values()):
        return None
    return stale['witnessed'] / found['witnessed'] - stale['full'] / found['full']


def bootstrap_difference(groups, repeats=10000):
    # Resample entire paired opportunities within each recorded session. Never
    # bootstrap individual blocks: the two stale indicators share a winner.
    rng = random.Random(90210)
    differences = []
    for _ in range(repeats):
        sample = [event for group in groups for event in rng.choices(group, k=len(group))]
        value = rate_difference(sample)
        if value is not None:
            differences.append(value)
    if len(differences) != repeats or len(set(differences)) < 2:
        return None
    return [quantile(differences, .025), quantile(differences, .975)]


def estimates(groups, repeats=10000):
    require(groups and all(groups), 'no completed opportunities')
    rows = [row for group in groups for row in group]
    for w, f, winner in rows:
        require((w or f) and winner in SIDES and ((w and winner == 'witnessed') or (f and winner == 'full')),
                'canonical winner must be a first-tick discovery')
    found, stale = counts(rows)
    n = len(rows)
    collisions = sum(w and f for w, f, _ in rows)
    result = {'opportunities': n, 'collisions': collisions, 'collision_fraction': collisions/n,
              'single_discoveries': n - collisions, 'discovered': found, 'stale': stale,
              'per_discovered_block': {s: stale[s]/found[s] if found[s] else None for s in SIDES},
              'witnessed_minus_full': rate_difference(rows)}
    # Each conditional stale proportion is binomial under iid opportunities.
    # Combining two 97.5% CP intervals with Bonferroni gives at least 95%
    # simultaneous coverage without any independence assumption between arms.
    intervals = {s: exact_interval(stale[s], found[s], .025) if found[s] else [0., 1.] for s in SIDES}
    w, f = intervals['witnessed'], intervals['full']
    result['difference_conservative_binomial_95'] = [w[0] - f[1], w[1] - f[0]]
    result['difference_paired_bootstrap_95'] = bootstrap_difference(groups, repeats)
    result['bootstrap_replicates'] = repeats
    if collisions:
        low, high = exact_interval(stale['witnessed'], collisions)
        result['among_collisions'] = {
            'witnessed_stale': stale['witnessed'], 'full_stale': stale['full'],
            'witnessed_stale_fraction': stale['witnessed']/collisions,
            'witnessed_minus_full': (stale['witnessed']-stale['full'])/collisions,
            'witnessed_stale_exact_95': [low, high], 'difference_exact_95': [2*low-1, 2*high-1],
        }
    else:
        result['among_collisions'] = None
    return result


def verify_schedule(record):
    rng = random.Random(record['seed'])
    n = record['planned_trials']
    require(type(n) is int and n > 0 and len(record['schedules']) == n, 'invalid trial schedule length')
    for i, row in enumerate(record['schedules']):
        # Match the three prespecified draws. The nonce bound reserves two
        # disjoint lanes through all 64 ticks without wrapping a uint32.
        nonce = rng.randrange(2**32 - 2*64 + 1)
        lane, dispatch = bool(rng.getrandbits(1)), bool(rng.getrandbits(1))
        require(row['index'] == i and row['start_nonce'] == nonce
                and row['swap_lanes'] is lane and row['swap_dispatch'] is dispatch,
                f'draw or assignment differs from preregistered seed at trial {i}')


def apply_block(ledger, raw, height, hash_, parent):
    check_header(raw[:80], hash_, parent)
    _, txs, _ = decode_block(raw, hash_)
    transactions = [Transaction.parse(tx) for tx in txs]
    ledger.connect(height, transactions)
    return transactions


def check_call(receipt, elapsed, name, results=(None,)):
    require(isinstance(receipt, dict) and receipt['ok'] is True and receipt['result'] in results,
            f'{name} lacks a successful reference RPC result')
    start, end = receipt['started_ns'], receipt['finished_ns']
    require(type(start) is int and type(end) is int and 0 <= start <= end <= elapsed,
            f'inconsistent local timer for {name}')


def ingress_ms(relay):
    stamp, duration = Decimal(relay['proxy_time']), Decimal(relay['proxy_request_time'])
    require(stamp.is_finite() and duration.is_finite() and stamp > 0 and duration >= 0,
            'invalid proxy response clock or request duration')
    recomputed = int(1000 * (stamp - duration))
    require(relay['ingress_ms'] == recomputed, 'proxy ingress bucket differs from source clock fields')
    return recomputed


def validate_payment(trial, ledger):
    payment = Transaction.parse(bytes.fromhex(trial['payment_hex']))
    require(payment.id == trial['payment_txid'] and len(payment.inputs) == 1, 'wrong common payment')
    point = payment.inputs[0].point
    source = trial['source']
    require(point == (source['txid'], source['vout']), 'payment outpoint disagrees with recorded source')
    coin = ledger.live.get(point)
    require(coin is not None and ledger.witnessed.get(point) == coin and coin.eligible(trial['height']-1),
            'common payment source is unavailable, unwitnessed or ineligible')
    require(coin.value == source['value_satoshis'] and coin.script.hex() == source['locking_script_hex'],
            'payment source amount or script differs from full replay')
    require(ledger.signatures.verify(payment, 0, coin), 'fixture payment signature unsupported')
    fee = coin.value - sum(value for value, _ in payment.outputs)
    require(fee >= 0 and payment.scoped_context(), 'ineligible payment context or negative fee')
    return payment, fee


def verify_work(trial, schedule, payment, fee):
    work = trial['work']
    height, parent = trial['height'], trial['parent']
    require(work['parent'] == parent and work['payment_txid'] == payment.id and work['target_bits'] == '207fffff',
            'jobs do not share the claimed parent, payment and target')
    for key in ('start_nonce', 'swap_lanes', 'swap_dispatch'):
        require(work[key] == schedule[key], f'{key} differs from schedule')
    ticks = work['attempts_per_arm']
    require(work['nonce_stride'] == 2 and work['max_ticks'] == 64 and 1 <= ticks <= 64, 'wrong work budget')
    arms = indexed(work['arms'], 'label', 'arm')
    require(set(arms) == set(SIDES), 'missing one of the two arms')
    solved = {}
    block_sizes = {}
    for index, side in enumerate(SIDES):
        arm = arms[side]
        require(arm['kind'] == ('stratum' if side == 'witnessed' else 'teranode') and arm['job_id'],
                f'missing {side} native job identity')
        prefix = bytes.fromhex(arm['header_prefix_hex'])
        require(len(prefix) == 76 and prefix[4:36][::-1].hex() == parent
                and prefix[72:76] == struct.pack('<I', 0x207fffff), 'candidate parent or target mismatch')
        coinbase = Transaction.parse(bytes.fromhex(arm['coinbase_hex']))
        require(len(coinbase.inputs) == 1 and coinbase.inputs[0].point == NULL
                and 2 <= len(coinbase.inputs[0].script) <= 100, 'invalid candidate coinbase')
        require(coinbase_height(coinbase.inputs[0].script) == height, 'candidate coinbase height mismatch')
        require(sum(n for n, _ in coinbase.outputs) == (5_000_000_000 >> (height // 150)) + fee,
                'candidate does not pay the same subsidy and common fee')
        root = dsha(dsha(coinbase.raw) + dsha(payment.raw))
        require(prefix[36:68] == root, 'common ordinary payment is not committed in candidate')
        first = schedule['start_nonce'] + (index ^ int(schedule['swap_lanes']))
        last = first + 2 * (ticks-1)
        require(arm['first_nonce'] == first and arm['last_nonce'] == last and last < 2**32,
                'nonce range differs from equal-work assignment')
        for tick in range(ticks):
            header = prefix + struct.pack('<I', first + 2*tick)
            hit = int.from_bytes(dsha(header), 'little') <= TARGET
            require(not hit or tick == ticks-1, 'earlier successful nonce omitted')
        require(arm['solved'] is hit, 'first-hit label disagrees with recomputed proof of work')
        body = header + b'\x02' + coinbase.raw + payment.raw
        block_sizes[side] = len(body)
        if hit:
            require(arm['hash'] == txid(header) and arm['header_hex'] == header.hex()
                    and arm['block_hex'] == body.hex(), 'solved block bytes differ from assigned nonce and template')
            check_call(arm['proposal'], work['elapsed_ns'], f'{side} proposal')
            check_call(arm['relay'], work['elapsed_ns'], f'{side} submitblock', (None, 'inconclusive'))
            ingress_ms(arm['relay'])
            solved[side] = body
        else:
            unexpected = any(arm[key] is not None for key in ('hash', 'header_hex', 'block_hex', 'proposal', 'relay'))
            require(not unexpected, 'unsolved job has a submission or solved body receipt')
    require(solved, 'completed opportunity has no valid solution')
    require(len(set(solved.values())) == len(solved), 'tied discoveries must be two distinct block hashes')
    order = list(reversed(SIDES)) if schedule['swap_dispatch'] else list(SIDES)
    expected_order = [s for s in order if s in solved]
    require(work['dispatch_order'] == expected_order, 'submit task dispatch differs from preset slot order')
    require(max(arms[s]['proposal']['finished_ns'] for s in solved) <= min(arms[s]['relay']['started_ns'] for s in solved),
            'submissions started before neutral preflight of all solutions')
    if len(solved) == 2:
        first, second = expected_order
        require(arms[first]['proposal']['finished_ns'] <= arms[second]['proposal']['started_ns'],
                'RPC proposal order differs from planned order')
    return arms, solved, block_sizes


def verify_judge(trial, arms, solved):
    judge = trial['judge']
    active = judge['active_hash_at_height']
    winner = judge['selected']
    require(winner in solved and arms[winner]['hash'] == active, 'neutral active block differs from selected job')
    observations = indexed(judge['observations'], 'label', 'neutral observation')
    require(set(observations) == set(solved), 'missing reference body for a discovery, including a stale sibling')
    for side, body in solved.items():
        observation = observations[side]
        require(observation['hash'] == arms[side]['hash'] and observation['body_hex'] == body.hex()
                and observation['body_sha256'] == hashlib.sha256(body).hexdigest(),
                'neutral node served a different sibling body')
        require(observation['active_at_trial_height'] is (side == winner), 'incorrect neutral ancestry observation')
        # At this point the common parent, full source script/value, signature,
        # winning nonce, candidate commitments, and exact served body passed
        # independent checks. verify_trial still requires the three H+2 reads.
        check_chain_tip(observation, arms[side], trial['height'], side == winner, replayed_body=body)
    native = trial['native']
    require(native['producer'] == winner and native['hash'] == active and native['job_id'] == arms[winner]['job_id']
            and native['nonce'] == arms[winner]['last_nonce'] and native['accepted'] is True
            and native['method'] == ('mining.submit' if winner == 'witnessed' else 'submitminingsolution'),
            'native producer did not submit the measured neutral winner')
    nodes = indexed(native['reference_receipts'], 'node', 'native receipt')
    require(set(nodes) == {'tn1', 'tn2'}, 'missing independent Teranode winner receipts')
    for node in nodes.values():
        require(node['height'] == trial['height'] and node['hash'] == node['tip'] == active
                and node['body_sha256'] == hashlib.sha256(solved[winner]).hexdigest(),
                'native Teranode winner receipt disagrees with selected body')
    return winner


def check_chain_tip(observation, arm, height, active, replayed_body=None):
    result = arm['relay']['result']
    tip = observation['chain_tip']
    require(result == observation['submitblock_result'] and tip['hash'] == arm['hash'] and tip['height'] == height,
            'wrong submitted branch or recorded RPC verdict')
    if active:
        require(result is None and tip['status'] == 'active' and tip['branchlen'] == 0,
                'active result lacks a definitive active branch receipt')
    else:
        require(result in (None, 'inconclusive') and tip['status'] in ('valid-fork', 'valid-headers')
                and tip['branchlen'] == 1, 'stale result lacks a corroborated one-block fork receipt')
        if tip['status'] == 'valid-headers':
            # This index status alone does not establish SCRIPT_VALID. Require
            # the common-parent TestBlockValidity proposal and independently
            # replayed full body (including PoW and the common signed spend).
            proposal = arm.get('proposal') or {}
            require(replayed_body is not None and observation.get('body_hex') == replayed_body.hex()
                    and proposal.get('ok') is True and 'result' in proposal and proposal['result'] is None
                    and proposal.get('finished_ns', math.inf) <= arm['relay'].get('started_ns', -1),
                    'valid-headers lacks successful common-parent preflight and independently verified full body')


def verify_trial(trial, schedule, ledger, height, parent):
    i = schedule['index']
    require((trial['height'], trial['parent'], trial['index']) == (height, parent, i), 'trial chronology differs')
    require(trial['full_producer'] == ('tn1' if i % 2 == 0 else 'tn2'), 'full producer did not follow rotation')
    before = trial['pre_trial']
    require(before['witnessed_tip'] == before['full_tip'] == before['judge_tip'] == parent,
            'producers and neutral node did not report a common parent')
    require(before['retained_headers'] == min(147, height-ledger.join)
            and before['witnessed_outputs'] == len(ledger.witnessed) and before['pending_transactions'] == 1,
            'witnessed prefix or offered workload disagrees with replay')
    payment, fee = validate_payment(trial, ledger)
    coin = ledger.witnessed[payment.inputs[0].point]
    outpoint = before['outpoint']
    require(outpoint['known'] is True and outpoint['eligible'] is True and outpoint['consumed'] is False
            and outpoint['height'] == coin.height and outpoint['coinbase'] is coin.coinbase
            and outpoint['value_satoshis'] == coin.value, 'entrant outpoint observation mismatch')
    arms, solved, sizes = verify_work(trial, schedule, payment, fee)
    winner = verify_judge(trial, arms, solved)
    candidate_hash = arms[winner]['hash']
    # Test every losing body on its own copy of the true parent ledger. The
    # cryptographic verification cache is safe to share across these branches.
    for side, body in solved.items():
        if side != winner:
            fork = deepcopy(ledger, {id(ledger.signatures): ledger.signatures})
            apply_block(fork, body, height, arms[side]['hash'], parent)
    apply_block(ledger, solved[winner], height, candidate_hash, parent)
    previous = candidate_hash
    require(len(trial['successors']) == 2, 'ordinary two-successor endpoint required')
    for offset, desc in enumerate(trial['successors'], 1):
        require(desc['height'] == height+offset and desc['parent'] == previous
                and desc['miner'] == ('tn1' if (i+offset) % 2 == 0 else 'tn2'), 'successor rotation or ancestry differs')
        apply_block(ledger, bytes.fromhex(desc['wire_hex']), height+offset, desc['hash'], previous)
        previous = desc['hash']
    endpoints = indexed(trial['endpoint'], 'node', 'endpoint')
    require(set(endpoints) == {'tn1', 'tn2', 'sv-judge'}, 'missing active-chain endpoint')
    for node in endpoints.values():
        require(node['height'] == height+2 and node['tip'] == previous and node['trial_hash'] == candidate_hash
                and node['parent_hash'] == parent and node['successor_hash'] == trial['successors'][0]['hash'],
                'active endpoint does not contain measured candidate and its successors')
    row = {'index': i, 'height': height, 'winner': winner, 'attempts_per_arm': trial['work']['attempts_per_arm'],
           'discoveries': {s: s in solved for s in SIDES}, 'candidate_wire_bytes': sizes,
           'swap_dispatch': schedule['swap_dispatch'], 'swap_lanes': schedule['swap_lanes'],
           'submitblock_results': {s: arms[s]['relay']['result'] for s in solved},
           'chain_tip_statuses': {o['label']:o['chain_tip']['status'] for o in trial['judge']['observations']},
           'proxy_ingress_ms': {s: ingress_ms(arms[s]['relay']) for s in solved},
           'relay_elapsed_ms': {s: (arms[s]['relay']['finished_ns']-arms[s]['relay']['started_ns'])/1e6 for s in solved},
           'client_start_ns': {s: arms[s]['relay']['started_ns'] for s in solved}, 'endpoint_hash': previous}
    return row, previous


def verify(record, harness_directory=None):
    require(record['schema_version'] == 1 and record['network'] == 'regtest' and record['endpoint_depth'] == 2,
            'wrong race network, endpoint or schema')
    verify_schedule(record)
    require(type(record['complete']) is bool, 'missing explicit completion state')
    check_provenance(record, harness_directory or Path(__file__).resolve().parent)
    planned = record['planned_trials']
    trials = record['trials']
    require(len(trials) <= planned, 'more attempted rounds than prespecified')
    if record['complete']:
        require(len(trials) == planned and not record.get('error'), 'completed receipt missing scheduled rounds or has an error')
    else:
        require(record.get('error'), 'incomplete receipt missing recorded failure')
    verified = []
    setup = record['setup_blocks']
    if setup:
        require(setup[0]['hash'] == GENESIS, 'setup is not rooted at regtest genesis')
    join = record.get('entrant_join_height')
    if join is None:
        require(not record['complete'] and not trials, 'missing entrant observation boundary')
    else:
        require(0 <= join < len(setup) and setup[join]['hash'] == record['entrant_join_hash'], 'false observation boundary')
    if trials or record['complete']:
        require(join == 101 and len(setup) == 104, 'setup differs from the late join and common funding schedule')
    with tempfile.TemporaryDirectory() as directory:
        ledger = Ledger(join if join is not None else len(setup), Signatures(directory))
        parent = '00'*32
        for h, block in enumerate(setup):
            require(block['height'] == h and block['parent'] == parent, 'broken setup chronology')
            apply_block(ledger, bytes.fromhex(block['wire_hex']), h, block['hash'], parent)
            parent = block['hash']
        height = len(setup)
        for i, trial in enumerate(trials):
            require(trial['index'] == i, 'attempt prefix does not match planned sequence')
            if not record['complete'] and i == len(trials)-1 and len(trial.get('endpoint', [])) < 3:
                # No stale/surviving classifications for an incomplete opportunity.
                break
            row, parent = verify_trial(trial, record['schedules'][i], ledger, height, parent)
            verified.append(row)
            height += 3
    return {'seed': record['seed'], 'complete': record['complete'], 'planned': planned,
            'started': len(trials), 'verified': len(verified), 'unresolved_or_unrun': planned-len(verified),
            'error': record.get('error'), 'provenance': record.get('provenance'),
            'harness_sha256': record.get('harness_sha256'), 'rounds': verified}


def latency_summary(rows):
    ties = [r for r in rows if all(r['discoveries'].values())]
    def earlier(a, b):
        return 'same_bucket' if a == b else 'witnessed_first' if a < b else 'full_first'
    ingress = {label: Counter() for label in ('witnessed_first', 'full_first', 'same_bucket')}
    signed_proxy, signed_client = [], []
    dispatch = {label: Counter() for label in SIDES}
    verdicts = {label: Counter() for label in SIDES}
    index_status = {label: Counter() for label in SIDES}
    for row in rows:
        for label, value in row['submitblock_results'].items():
            verdicts[label]['null' if value is None else value] += 1
            index_status[label][row['chain_tip_statuses'][label]] += 1
    for row in ties:
        marks = row['proxy_ingress_ms']
        label = earlier(marks['witnessed'], marks['full'])
        ingress[label][row['winner']] += 1
        dispatch['full' if row['swap_dispatch'] else 'witnessed'][row['winner']] += 1
        signed_proxy.append(marks['witnessed'] - marks['full'])
        starts = row['client_start_ns']
        signed_client.append((starts['witnessed'] - starts['full']) / 1e6)
    return {'tie_winners_by_proxy_ingress_bucket': {k: dict(v) for k,v in ingress.items()},
            'submitblock_result_counts': {k: dict(v) for k,v in verdicts.items()},
            'chain_tip_status_counts': {k: dict(v) for k,v in index_status.items()},
            'tie_winners_by_first_dispatched_role': {k: dict(v) for k,v in dispatch.items()},
            'signed_proxy_ingress_difference_median_ms': quantile(signed_proxy,.5),
            'absolute_proxy_ingress_difference_p95_ms': quantile([abs(x) for x in signed_proxy],.95),
            'signed_client_start_difference_median_ms': quantile(signed_client,.5),
            'absolute_client_start_difference_p95_ms': quantile([abs(x) for x in signed_client],.95),
            'relay_elapsed_median_ms': {s: quantile([r['relay_elapsed_ms'][s] for r in rows if s in r['relay_elapsed_ms']],.5) for s in SIDES},
            'candidate_wire_bytes_median': {s: quantile([r['candidate_wire_bytes'][s] for r in rows],.5) for s in SIDES}}


def pooled_identity(provenance):
    # BuildKit can attach a different provenance attestation to a reproducible
    # local build, changing its enclosing manifest-list ID. Compare the ELF
    # actually executed and keep the original image records in each session.
    return (provenance['rust_binary_sha256'], provenance['rust_version'],
            provenance['images']['scary-node']['image_reference'],
            provenance['judge_version'], provenance['judge_subversion'],
            {s: image for s, image in provenance['images'].items() if s != 'scary-node'})


def summarize(sessions):
    require(len({r['seed'] for r in sessions}) == len(sessions), 'duplicate session seed')
    result = {'sessions': [{k: v for k,v in session.items() if k != 'rounds'} for session in sessions]}
    if any(not session['complete'] for session in sessions):
        result['estimates'] = None
        return result
    if len(sessions) > 1:
        first = sessions[0]
        require(first['harness_sha256'] is not None and all(s['harness_sha256'] == first['harness_sha256'] for s in sessions),
                'pooling requires identical verified harness hashes')
        require(all(pooled_identity(s['provenance']) == pooled_identity(first['provenance']) for s in sessions),
                'pooling requires identical recorded binary, image and judge identities')
    groups = [[(r['discoveries']['witnessed'], r['discoveries']['full'], r['winner']) for r in s['rounds']] for s in sessions]
    all_rows = [r for s in sessions for r in s['rounds']]
    for output, group, session in zip(result['sessions'], groups, sessions):
        output['estimates'] = estimates([group])
        output['timing'] = latency_summary(session['rounds'])
    result['estimates'] = estimates(groups) if len(sessions) > 1 else result['sessions'][0]['estimates']
    result['timing'] = latency_summary(all_rows)
    result['hashes_per_role'] = sum(r['attempts_per_arm'] for r in all_rows)
    result['scope'] = ('Each solved candidate is scored against the independently observed active block at its height '
                       'after two successors. Discovery uses synchronous equal-work regtest ticks and one neutral RPC ingress. '
                       'Uncertainty describes repeated laboratory opportunities under an IID model within sessions. '
                       'The exact pooled binomial intervals additionally assume common conditional rates.')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('receipts', type=Path, nargs='+', help='one or more JSON/gzip session receipts')
    parser.add_argument('--json', action='store_true', help='emit verified session and pooled measurements')
    parser.add_argument('--harness-dir', type=Path, help='directory containing the four recorded lab files (default: this checkout)')
    args = parser.parse_args()
    try:
        analysis = summarize([verify(load_record(p), args.harness_dir) for p in args.receipts])
    except (ValueError, KeyError, IndexError, TypeError, OSError, InvalidOperation, struct.error, EOFError) as exc:
        sys.exit(f'Race verification failed: {exc}')
    if args.json:
        print(json.dumps(analysis, indent=2))
    else:
        for session in analysis['sessions']:
            print(f"Seed {session['seed']}: verified {session['verified']}/{session['planned']} opportunities.")
        if analysis['estimates']:
            s = analysis['estimates']
            for role in SIDES:
                print(f"{role}: {s['stale'][role]} stale of {s['discovered'][role]} discovered blocks.")
            print('Paired opportunity bootstrap interval for witnessed minus full:', s['difference_paired_bootstrap_95'])
    if analysis['estimates'] is None:
        sys.exit('Incomplete prespecified session. No final risk estimate.')
