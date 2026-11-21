"""Replay the reliability evidence and match each local settlement to three validators."""
import argparse
import hashlib
import json
import random
import tempfile
from pathlib import Path

from prove import decode_block, dsha, txid
from verify_receipt import check_header, require
from verify_scenario import Coin, Signatures, Transaction, load_record
from verify_races import quantile, summarize, verify


def block_fields(wire, parent=None):
    raw = bytes.fromhex(wire)
    ident = txid(raw[:80])
    check_header(raw[:80], ident, parent)
    return ident, decode_block(raw, ident)[1]


def check_settlement(solution, trial):
    require(solution['hash'] == trial['native']['hash'] and solution['job'] == trial['native']['job_id']
            and solution['parent'] == trial['parent'] and solution['height'] == trial['height']
            and solution['chain_state'] == 'settled' and solution['publication_error'] is None,
            'local solution identity or result differs from the selected native candidate')
    queued, published, included, settled = (solution[key] for key in (
        'queued_at_ms', 'published_at_ms', 'first_inclusion_at_ms', 'first_settlement_at_ms'))
    require(all(type(value) is int and value > 0 for value in (queued, published, included, settled))
            and queued <= min(published, included) <= settled and included <= settled,
            'local solution timings disagree')
    require(solution['transitions'][-1]['state'] == 'settled'
            and solution['transitions'][-1]['height'] == trial['height'] + 2,
            'wrong settlement transition height')
    require(len(trial['endpoint']) == 3 and all(
        peer['trial_hash'] == solution['hash'] and peer['height'] == trial['height'] + 2
        for peer in trial['endpoint']), 'settlement does not match all three full-state endpoints')


def session_interval(sessions):
    """Second sensitivity interval resampling whole fresh sessions."""
    rng = random.Random(270102)
    records = [session['estimates'] for session in sessions]
    samples = []
    for _ in range(10000):
        selected = rng.choices(records, k=len(records))
        rates = [sum(s['stale'][role] for s in selected) /
                 sum(s['discovered'][role] for s in selected)
                 for role in ('witnessed','full')]
        samples.append(rates[0] - rates[1])
    return [quantile(samples,.025),quantile(samples,.975)]


def cohort(bundle):
    sessions, local_count = [], 0
    runs = bundle['runs']
    require(bundle['network'] == 'regtest' and bundle['schema'] == 1
            and [run['race']['seed'] for run in runs] == list(range(2701, 2709)),
            'expected the exact preregistered eight-session cohort')
    for run in runs:
        race, status = run['race'], run['settlement']
        require(race['planned_trials'] == 128 and race['complete'] and status['seed'] == race['seed'],
                'incomplete or mismatched session')
        sessions.append(verify(race))
        expected = {t['native']['hash']: t for t in race['trials'] if t['native']['producer'] == 'witnessed'}
        rows = {s['hash']: s for s in status['solutions']}
        require(len(rows) == len(status['solutions']) == status['mining']['solved_blocks']
                and set(rows) == set(expected), 'local solution inventory differs from measured native winners')
        final = race['trials'][-1]['successors'][-1]
        require(status['mining']['tip_hash'] == final['hash'] and status['mining']['height'] == final['height'],
                'local settlement endpoint is on a different ancestry')
        for h, trial in expected.items():
            check_settlement(rows[h], trial)
        local_count += len(rows)
    report = summarize(sessions)
    require(report['estimates']['opportunities'] == 1024, 'incomplete pooled sample')
    return {'native_rust_settlements': local_count,
            'difference_session_bootstrap_95':session_interval(report['sessions']), **report}


def proof(record):
    require(record['network'] == 'regtest','incorrect proof scope')
    funding, funding_txs = block_fields(record['funding_wire'])
    parent, _ = block_fields(record['parent_wire'], funding)
    solved, txs = block_fields(record['candidate_wire'], parent)
    old, new = record['old_job'], record['new_job']
    raw = bytes.fromhex(record['candidate_wire'])
    for move in (old, new):
        prev = bytes.fromhex(move[1])
        previous = b''.join(prev[i:i+4][::-1] for i in range(0, 32, 4))[::-1].hex()
        require(previous == parent, 'notification parent differs from validated chain')
    first, second = (Transaction.parse(bytes.fromhex(record[k])) for k in ('first_payment','second_payment'))
    require(record['funding_id'] in {txid(tx) for tx in funding_txs}
            and first.inputs[0].point == (record['funding_id'],0)
            and second.inputs[0].point == (record['funding_id'],1),
            'independent jobs must spend the two distinct witnessed outputs')
    deposit = Transaction.parse(next(tx for tx in funding_txs if txid(tx) == record['funding_id']))
    require(len(deposit.outputs) == 2, 'the proof must witness both source outputs')
    with tempfile.TemporaryDirectory() as directory:
        signatures = Signatures(directory)
        for tx,(value,script) in zip((first,second),deposit.outputs):
            require(len(tx.inputs) == 1 and signatures.verify(tx,0,Coin(value,script,102,False))
                    and value-sum(amount for amount,_ in tx.outputs) == 1000,
                    'incorrect signed spend of the witnessed amount')
    expected = sorted([bytes.fromhex(t.id)[::-1] for t in (first,second)])
    require(old[0] != new[0] and old[8] is True and new[8] is False
            and old[4] == [bytes.fromhex(first.id)[::-1].hex()]
            and new[4] == [expected[0].hex(),dsha(expected[1]*2).hex()]
            and txs[1:] == [first.raw], 'block or refresh is not the claimed same-parent job')
    coinbase = txs[0]
    prefix, suffix = bytes.fromhex(old[2]), bytes.fromhex(old[3])
    require(coinbase.startswith(prefix) and coinbase.endswith(suffix)
            and len(coinbase) == len(prefix)+len(suffix)+8
            and raw[0:4][::-1].hex() == old[5] and raw[68:72][::-1].hex() == old[7]
            and raw[72:76][::-1].hex() == old[6], 'late solution differs from the original coinbase or header fields')
    require({r['node'] for r in record['references']} == {'tn1','tn2'}
            and all(r['hash'] == solved and r['sha256'] == hashlib.sha256(raw).hexdigest()
                    for r in record['references']), 'reference body evidence differs')
    parent = solved
    for successor in record['successors']:
        parent, _ = block_fields(successor['wire_hex'], parent)
        require(parent == successor['hash'], 'wrong successor hash')
    require(len(record['successors']) == 2 and record['solution']['hash'] == solved
            and record['solution']['chain_state'] == 'settled' and record['solution']['job'] == old[0],
            'late job did not settle')

    fork = record['reorg']
    require(fork['reorg_count'] > 0 and fork['common_parent'] == parent,'wrong retained fork point')
    detached, detached_txs = block_fields(fork['detached_wire'], parent)
    require(detached == fork['detached'] and detached_txs[1:] == [second.raw], 'wrong detached branch spend')
    hashes = []
    for index,wire in enumerate(fork['replacement_bodies']):
        parent, txs = block_fields(wire, parent)
        hashes.append(parent)
        if index < 2:
            opposing_inputs = [item.point for raw_tx in txs[1:] for item in Transaction.parse(raw_tx).inputs]
            require(second.inputs[0].point not in opposing_inputs,
                    'replacement prefix also spends the restored input')
    require(len(hashes) == 3 and hashes == fork['replacement'] and fork['reference_tips'] == [parent,parent],
            'two reference nodes did not settle the heavier branch')
    require(fork['source_before']['consumed'] and fork['source_after']['eligible']
            and not fork['source_after']['consumed'] and fork['output_before']['known']
            and not fork['output_after']['known'], 'reorg did not undo both sides of the detached payment')
    require(fork['retained_headers_after'] == fork['retained_headers_before'] + 1
            and fork['solution']['hash'] == detached and fork['solution']['chain_state'] == 'stale_at_depth',
            'missing retained-witness or displaced-solution evidence')
    return {'late_job_block':solved,'detached':detached,'heavier_tip':parent}


def pack(directory):
    return {'network':'regtest','schema':1,'runs':[
        {'race':load_record(directory / f'{seed}.json'),
         'settlement':load_record(directory / f'{seed}-settlement.json')}
        for seed in range(2701,2709)]}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind',choices=('proof','cohort','pack'))
    parser.add_argument('receipt',type=Path)
    args = parser.parse_args()
    if args.kind == 'pack':
        print(json.dumps(pack(args.receipt),separators=(',',':')))
    else:
        print(json.dumps((proof if args.kind == 'proof' else cohort)(load_record(args.receipt)),indent=2))
