"""Compare equal-work BSV mining jobs against the same fully validating regtest judge."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import time

from prove import API, NODES, DisposableWallet, assert_tip, block, compose, decode_block, request, rpc, status, txid
from verify_receipt import check_header, require

JUDGE = (19661, 0)
MAX_TICKS = 64
GENESIS = '0f9188f13cb7b2c71f2a335e3a4fc328bf5beb436012afca590b1a11466e2206'


def poll(label, fn, seconds=90):
    deadline = time.monotonic() + seconds
    last = 'condition pending'
    while time.monotonic() < deadline:
        try:
            result = fn()
            if result:
                return result
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            last = str(exc)
        time.sleep(0.08)
    raise RuntimeError(f'{label}: {last}')


def make_schedule(seed, count):
    rng = random.Random(seed)
    return [{'index': n, 'start_nonce': rng.randrange(2**32 - 2*MAX_TICKS + 1),
             'swap_lanes': bool(rng.getrandbits(1)), 'swap_dispatch': bool(rng.getrandbits(1))}
            for n in range(count)]


def utc():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


class Session:
    def __init__(self, seed, trials):
        self.height = 0
        self.tip = GENESIS
        self.source = None
        self.value = 0
        files = ('races.py', 'race_work.py', 'races.compose.yaml', 'race-ingress.conf')
        harness = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in files}
        self.record = {'schema_version': 1, 'harness_sha256': harness, 'network': 'regtest', 'seed': seed, 'planned_trials': trials,
                       'endpoint_depth': 2, 'started_utc': utc(), 'complete': False,
                       'schedules': make_schedule(seed, trials), 'setup_blocks': [], 'trials': []}

    def reference_block(self, miner_idx, destination, payments=(), address=None):
        """Mine normally on an independent Teranode; then validate the bytes at the judge."""
        miner, observer = NODES[miner_idx], NODES[1-miner_idx]
        def candidate_ready():
            c = rpc(miner, 'getminingcandidate')
            return c['prevhash'] == self.tip and c['num_tx'] == len(payments)
        poll('reference normal assembly', candidate_ready)
        args = ('generatetoaddress', 1, address) if address else ('generate', 1)
        result = rpc(miner, *args)[0]
        poll('reference peer adopts generated block', lambda: assert_tip(observer, result))
        raw, txs, outputs = poll('full reference body', lambda: block(miner, result))
        check_header(raw[:80], result, self.tip)
        require(set(txs[1:]) == set(payments), 'unexpected ordinary transactions in reference block')
        require(assert_tip(JUDGE, self.tip), 'judge fell off the common parent')
        require(rpc(JUDGE, 'submitblock', raw.hex()) is None, 'judge rejected reference block')
        poll('judge on generated block', lambda: assert_tip(JUDGE, result))
        destination.append({'height': self.height + 1, 'hash': result, 'parent': self.tip,
                            'wire_hex': raw.hex(), 'miner': f'tn{miner_idx + 1}'})
        self.height += 1
        self.tip = result
        return txs, outputs

    def provenance(self):
        images = {}
        for service in ('scary-node', 'tn1', 'tn2', 'judge', 'ingress'):
            container_id = compose('ps', '-q', service, capture=True).strip()
            label = subprocess.check_output(['docker', 'inspect', '--format', '{{.Config.Image}} {{.Image}}',
                                             container_id], text=True).strip().split()
            images[service] = {'image_reference': label[0], 'local_image_id': label[1]}
        info = rpc(JUDGE, 'getnetworkinfo')
        self.record['provenance'] = {
            'images': images,
            'rust_binary_sha256': compose('exec', '-T', 'scary-node', 'sha256sum', '/usr/local/bin/scary-node',
                                          capture=True).split()[0],
            'rust_version': compose('exec', '-T', 'scary-node', 'scary-node', '--version', capture=True).strip(),
            'judge_version': info['version'], 'judge_subversion': info['subversion'],
        }

    def setup(self, wallet):
        require(os.environ.get('COMPOSE_PROJECT_NAME') == 'scary-node-races' and
                os.environ.get('SCARY_LAB_RACES') == '1' and NODES == [(19641,19651),(19642,19652)],
                'requires isolated race Compose launcher and race loopback endpoints')
        for name, node in (('tn1', NODES[0]), ('tn2', NODES[1]), ('sv-judge', JUDGE)):
            info = poll(name + ' ready', lambda: rpc(node, 'getblockchaininfo'))
            require(info['chain'] == 'regtest' and info['blocks'] == 0 and info['bestblockhash'] == GENESIS,
                    f'{name} must start on fresh BSV regression genesis')
        peers = ['12D3KooWAFXWuxgdJoRsaA4J4RRRr8yu6WCrAPf8FaS7UfZg3ceG',
                 '12D3KooWG6aCkDmi5tqx4G4AvVDTQdSVvTSzzQvk1vh9CtSR8KEW']
        for node, other in zip(NODES, reversed(peers)):
            poll('Teranode peers connected', lambda: any(p.get('peerid') == other for p in rpc(node, 'getpeerinfo')))
        setup = self.record['setup_blocks']
        raw, _, _ = block(NODES[0], GENESIS)
        setup.append({'height': 0, 'hash': GENESIS, 'parent': '00'*32, 'wire_hex': raw.hex(), 'miner': 'genesis'})
        txs, outs = self.reference_block(0, setup, address=wallet.address)
        coinbase_id, (value, script) = txid(txs[0]), outs[0][0]
        require(script == wallet.script, 'reference coinbase disagrees with wallet')
        for i in range(100):
            self.reference_block(i % 2, setup)
        compose('up', '-d', '--no-build', '--no-deps', 'scary-node')
        poll('fresh entrant connects to reference peers', lambda: status()['peer']['connected_peer_ids_seen'] >= 2)
        m = poll('entrant late joins tip', lambda: self.observer_at_tip())
        require(m['headers'] == 1, 'entrant read more than its chosen entry block')
        self.provenance()
        self.record['entrant_join_height'] = self.height
        self.record['entrant_join_hash'] = self.tip
        funding = wallet.spend(coinbase_id, value, 1000)
        require(rpc(NODES[0], 'sendrawtransaction', funding.hex()) == txid(funding), 'funding admission mismatch')
        self.reference_block(0, setup, [funding])
        poll('entrant recognizes funding body', self.observer_at_tip)
        self.reference_block(1, setup)
        poll('entrant recognizes funding successor', self.observer_at_tip)
        self.source, self.value = txid(funding), value - 1000
        print(f'Prepared a common eligible source at height {self.height}.', file=sys.stderr)

    def observer_at_tip(self):
        m = status()['mining']
        return m if m['tip_hash'] == self.tip else None

    def trial(self, wallet, schedule):
        from race_work import proxy_rpc, race_pair, stratum_job, teranode_job
        index = schedule['index']
        parent, height = self.tip, self.height + 1
        full_idx = index % 2
        full = NODES[full_idx]
        payment = wallet.spend(self.source, self.value, 1000)
        pid = txid(payment)
        row = {'index': index, 'height': height, 'parent': parent, 'payment_txid': pid,
               'payment_hex': payment.hex(), 'source': {'txid': self.source, 'vout': 0,
               'value_satoshis': self.value, 'locking_script_hex': wallet.script.hex()},
               'full_producer': f'tn{full_idx+1}', 'successors': [], 'endpoint': []}
        self.record['trials'].append(row)
        require(all(assert_tip(node, parent) for node in (*NODES, JUDGE)), 'trial endpoints lack common parent')
        outpoint = json.loads(request(f'{API}/outpoints/{self.source}/0'))
        require(outpoint['known'] and outpoint['eligible'] and outpoint['value_satoshis'] == self.value,
                'common deposit is ineligible at witnessed miner')
        admitted = json.loads(request(API + '/transactions', payment, {'Content-Type':'application/octet-stream'}))
        require(admitted['txid'] == pid and rpc(full,'sendrawtransaction',payment.hex()) == pid,
                'expected both producers to accept common payment')
        m = poll('witnessed producer ready', self.observer_at_tip)
        row['pre_trial'] = {'witnessed_tip': m['tip_hash'], 'full_tip': rpc(full, 'getblockchaininfo')['bestblockhash'],
                            'judge_tip': rpc(JUDGE, 'getblockchaininfo')['bestblockhash'], 'retained_headers': m['headers'],
                            'witnessed_outputs': m['witnessed_outputs'], 'pending_transactions': m['pending_transactions'],
                            'outpoint': outpoint}
        with stratum_job(parent, payment, 19633, 19680, label='witnessed') as w, \
             teranode_job(parent, payment, full, label='full') as f:
            work = race_pair(w, f, schedule['start_nonce'],
                             lambda raw: proxy_rpc(19661,'submitblock',[raw.hex()]),
                             preflight=lambda raw: proxy_rpc(19661,'getblocktemplate',[{'mode':'proposal','data':raw.hex()}]),
                             max_ticks=MAX_TICKS, swap_lanes=schedule['swap_lanes'], swap_dispatch=schedule['swap_dispatch'])
            row['work'] = work
            solved = [arm for arm in work['arms'] if arm['solved']]
            require(solved, 'no network-target solution within the predeclared work budget')
            for arm in solved:
                proposal, relay = arm['proposal'], arm['relay']
                require(proposal and proposal['ok'] and proposal['result'] is None,
                        f"neutral proposal failed for {arm['label']}: {proposal}")
                require(relay and relay['ok'] and relay['result'] in (None, 'inconclusive'),
                        f"neutral submission failed for {arm['label']}: {relay}")
                require(isinstance(arm['relay'].get('ingress_ms'), int), 'missing neutral proxy ingress timestamp')
            active = rpc(JUDGE, 'getblockhash', height)
            row['judge'] = {'active_hash_at_height': active, 'observations': []}
            winner = next((arm for arm in solved if arm['hash'] == active), None)
            require(winner is not None, 'neutral active tip differs from every discovered candidate')
            row['judge']['selected'] = winner['label']
            tips = {tip['hash']: tip for tip in rpc(JUDGE,'getchaintips')}
            for arm in solved:
                body = rpc(JUDGE, 'getblock', arm['hash'], 0)
                require(body == arm['block_hex'], 'neutral judge did not serve exact candidate')
                decode_block(bytes.fromhex(body), arm['hash'])
                chain_tip = tips.get(arm['hash'])
                confirmations = rpc(JUDGE,'getblockheader',arm['hash'],True)['confirmations']
                chosen = active == arm['hash']
                row['judge']['observations'].append({'label': arm['label'], 'hash': arm['hash'], 'body_hex': body,
                    'body_sha256': hashlib.sha256(bytes.fromhex(body)).hexdigest(), 'active_at_trial_height': chosen,
                    'chain_tip': chain_tip, 'confirmations': confirmations, 'submitblock_result': arm['relay']['result']})
                # Side indexes may stop at TREE even after a successful proposal on the common parent.
                # Preserve that distinction. A stored full body, PoW and economic/script replay are required.
                allowed_statuses = {'active'} if chosen else {'valid-fork', 'valid-headers'}
                require(chain_tip is not None and chain_tip['height'] == height and chain_tip['status'] in allowed_statuses
                        and chain_tip['branchlen'] == (0 if chosen else 1),
                        f"unexpected neutral {arm['label']} chain status: {chain_tip}")
                require(not chosen or arm['relay']['result'] is None, 'inconclusive active submission')
            job, method = (w, 'mining.submit') if winner['label'] == 'witnessed' else (f, 'submitminingsolution')
            native = {'producer':winner['label'], 'job_id':winner['job_id'], 'nonce':winner['last_nonce'],
                      'hash':active, 'method':method, 'accepted':False, 'reference_receipts':[]}
            row['native'] = native
            native['accepted'] = job.submit_native(winner['last_nonce'])
            require(native['accepted'] is True, 'native producer rejected neutral winner')
        for name, node in zip(('tn1','tn2'),NODES):
            poll('reference adopts native selected solution', lambda: assert_tip(node, active))
            raw, txs, _ = poll('reference selected body', lambda: block(node, active))
            require(raw.hex() == winner['block_hex'] and txs[1:] == [payment], 'reference candidate body mismatch')
            native['reference_receipts'].append({'node':name, 'height':height, 'hash':active, 'tip':active,
                'body_sha256': hashlib.sha256(raw).hexdigest()})
        self.height, self.tip = height, active
        poll('entrant recognizes native winner', self.observer_at_tip)
        for miner_idx in (1-full_idx,full_idx):
            self.reference_block(miner_idx,row['successors'])
            poll('entrant recognizes full-node successor',self.observer_at_tip)
        for name, node in (('tn1',NODES[0]),('tn2',NODES[1]),('sv-judge',JUDGE)):
            info = rpc(node,'getblockchaininfo')
            endpoint = {'node':name, 'height':info['blocks'], 'tip':info['bestblockhash'],
                        'parent_hash':rpc(node,'getblockhash',height-1), 'trial_hash':rpc(node,'getblockhash',height),
                        'successor_hash':rpc(node,'getblockhash',height+1)}
            row['endpoint'].append(endpoint)
            require(endpoint['height'] == height+2 and endpoint['tip'] == self.tip and endpoint['parent_hash'] == parent
                    and endpoint['trial_hash'] == active and endpoint['successor_hash'] == row['successors'][0]['hash'],
                    'endpoint ancestry disagreement')
        self.source, self.value = pid, self.value-1000
        if (index+1) % 16 == 0 or index == 0:
            print(f'Completed {index+1}/{self.record["planned_trials"]} opportunities through height {self.height}.', file=sys.stderr)

    def run(self, wallet):
        self.setup(wallet)
        for schedule in self.record['schedules']:
            self.trial(wallet, schedule)
        self.record['complete'] = True


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seed', type=int, default=1701)
    parser.add_argument('--trials', type=int, default=128, choices=range(1,129), metavar='1..128')
    args = parser.parse_args()
    session = Session(args.seed,args.trials)
    try:
        with tempfile.TemporaryDirectory() as directory:
            session.run(DisposableWallet(Path(directory)/'key.pem'))
    except (Exception, KeyboardInterrupt) as exc:
        session.record['error'] = f'{type(exc).__name__}: {exc}'
        print(session.record['error'], file=sys.stderr)
    session.record['observed_utc'] = utc()
    print(json.dumps(session.record,separators=(',',':')))
    sys.exit(0 if session.record['complete'] else 1)
