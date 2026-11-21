"""Mine a signed, multi-wallet BSV regtest workload with a late-joining miner."""
import argparse
from collections import deque
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import random
import struct
import sys
import tempfile
import time
import urllib.error
import urllib.request

from prove import (API, NODES, DisposableWallet, assert_tip, block, compose, dsha,
                   read_varint, request, rpc, solve, status, txid, wait)

FEE = 1000


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


@dataclass(frozen=True)
class Coin:
    txid: str
    index: int
    value: int
    owner: int

    def prevout(self):
        return bytes.fromhex(self.txid)[::-1] + struct.pack('<I', self.index)


@dataclass
class Payment:
    raw: bytes
    outputs: list

    @property
    def id(self):
        return txid(self.raw)


def pay(wallets, inputs, outputs):
    """Sign one SIGHASH_ALL|FORKID input per owner and pay named P2PKH recipients."""
    require(sum(c.value for c in inputs) == sum(v for _, v in outputs) + FEE, 'payment fee mismatch')
    require(all(v >= 546 for _, v in outputs), 'dust in fixture payment')
    version, sequence, locktime = struct.pack('<I', 1), b'\xff' * 4, bytes(4)
    destinations = b''.join(struct.pack('<Q', value) + bytes([25]) + wallets[owner].script
                            for owner, value in outputs)
    prevouts = dsha(b''.join(coin.prevout() for coin in inputs))
    sequences = dsha(sequence * len(inputs))
    encoded = []
    for coin in inputs:
        wallet = wallets[coin.owner]
        digest = dsha(version + prevouts + sequences + coin.prevout() + bytes([25]) + wallet.script
                      + struct.pack('<Q', coin.value) + sequence + dsha(destinations) + locktime
                      + struct.pack('<I', 0x41))
        script = wallet.unlocking_script(digest)
        encoded.append(coin.prevout() + bytes([len(script)]) + script + sequence)
    raw = version + bytes([len(inputs)]) + b''.join(encoded) + bytes([len(outputs)]) + destinations + locktime
    return Payment(raw, [Coin(txid(raw), i, value, owner) for i, (owner, value) in enumerate(outputs)])


def input_keys(raw):
    count, pos = read_varint(raw, 4)
    for _ in range(count):
        yield raw[pos:pos+32][::-1].hex(), struct.unpack_from('<I', raw, pos + 32)[0]
        length, pos = read_varint(raw, pos + 36)
        pos += length + 4


def node_events(name):
    for line in compose('logs', '--no-color', '--no-log-prefix', name, capture=True).splitlines():
        try:
            yield json.loads(line)
        except ValueError:
            continue


def api_post(path, raw):
    req = urllib.request.Request(API + path, data=raw, headers={'Content-Type': 'application/octet-stream'})
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


class Scenario:
    def __init__(self, wallets, seed):
        self.wallets = wallets
        self.rng = random.Random(seed)
        self.script_owners = {wallet.script: i for i, wallet in enumerate(wallets)}
        self.lanes = []
        self.accounts = [deque() for _ in range(16)]
        self.dormant = []
        self.ledger = {}
        self.history_outputs = {}
        self.ref_index = 0
        self.capture = False
        self.activity = 0
        self.record = {'schema_version': 1, 'network': 'regtest', 'teranode_release': 'v0.16.1',
                       'seed': seed, 'parties': [{'id': i, 'address': w.address, 'locking_script_hex': w.script.hex()}
                                                for i, w in enumerate(wallets)],
                       'blocks': [], 'checkpoints': [], 'candidates': [], 'controls': []}

    @property
    def height(self):
        return len(self.record['blocks']) - 1

    @property
    def tip(self):
        return self.record['blocks'][-1]['hash']

    def save_block(self, data, miner, phase):
        raw, txs, outputs = data
        if self.record['blocks']:
            require(raw[4:36][::-1].hex() == self.tip, 'reference returned a discontinuous chain')
        h = self.height + 1
        self.record['blocks'].append({'height': h, 'hash': txid(raw[:80]), 'miner': miner,
                                      'phase': phase, 'wire_hex': raw.hex()})
        if h > 0:
            for i, (tx, vouts) in enumerate(zip(txs, outputs)):
                if i:
                    for key in input_keys(tx):
                        require(self.ledger.pop(key, None) is not None, f'unknown source in full replay: {key}')
                keys = []
                for index, (_, script) in enumerate(vouts):
                    require(len(script) == 25 and script.startswith(b'\x76\xa9\x14') and script.endswith(b'\x88\xac'),
                            'unexpected output script in scoped workload')
                    key = txid(tx), index
                    self.ledger[key] = (h, self.script_owners.get(script))
                    keys.append(key)
                if i and phase == 'history':
                    self.history_outputs[txid(tx)] = keys

    def checkpoint(self):
        wait('entrant current observed tip', lambda: status()['mining']['tip_hash'] == self.tip)
        view = status()['mining']
        born = self.record['join_height']
        observed = sum(h >= born for h, _ in self.ledger.values())
        require(view['witnessed_outputs'] == observed, 'witnessed working set differs from replay')
        coins = self.dormant[:1] + [queue[0] for queue in self.accounts[:4] if queue]
        probes = []
        for coin in coins:
            probe = json.loads(request(f'{API}/outpoints/{coin.txid}/{coin.index}'))
            key = coin.txid, coin.index
            require(probe['known'] == (key in self.ledger and self.ledger[key][0] >= born), 'outpoint membership mismatch')
            probes.append({'txid': coin.txid, 'vout': coin.index, **probe})
        self.record['checkpoints'].append({'height': self.height, 'tip_hash': self.tip, 'mining': view,
            'utxo_count': len(self.ledger), 'actor_utxo_count': sum(w is not None for _, w in self.ledger.values()),
            'witnessed_actor_utxo_count': sum(h >= born and w is not None for h, w in self.ledger.values()),
            'outpoints': probes})

    def reference(self, phase, payments=(), payout=None, already=()):
        idx = self.ref_index
        miner, other = NODES[idx], NODES[1-idx]
        for payment in payments:
            require(rpc(miner, 'sendrawtransaction', payment.raw.hex()) == payment.id, 'reference admission mismatch')
        ids = {payment.id for payment in (*payments, *already)}
        def assembled():
            template = rpc(miner, 'getminingcandidate')
            return template['prevhash'] == self.tip and template['num_tx'] == len(ids)
        wait('reference has parent and all scheduled transactions', assembled)
        args = ('generatetoaddress', 1, payout.address) if payout else ('generate', 1)
        bh = rpc(miner, *args)[0]
        wait('independent reference adopts scheduled block', lambda: assert_tip(other, bh))
        data = wait('scheduled block body', lambda: block(miner, bh))
        require({txid(tx) for tx in data[1][1:]} == ids, f'unexpected transaction set in {phase} block')
        self.save_block(data, f'tn{idx+1}', phase)
        self.ref_index = 1 - idx
        if self.capture:
            self.checkpoint()
        return data

    def destination(self, owner):
        return (owner + 1 + self.rng.randrange(len(self.wallets) - 1)) % len(self.wallets)

    def transfer(self, coin, change=False):
        amount = coin.value - FEE
        outputs = [(self.destination(coin.owner), amount - (20000 if change else 0))]
        if change:
            outputs.append((coin.owner, 20000))
        return pay(self.wallets, [coin], outputs)

    def lane_payments(self, count=8):
        result = []
        for _ in range(count):
            n = self.activity
            coin = self.lanes[n % len(self.lanes)]
            item = self.transfer(coin, n % 3 != 0)
            self.lanes[n % len(self.lanes)] = item.outputs[0]
            self.activity += 1
            result.append(item)
        return result

    def account_payment(self, index):
        fifo = self.accounts[index]
        item = self.transfer(fifo.popleft())
        fifo.append(item.outputs[0])
        return item

    def control(self, name, payment, outcome):
        code, response = api_post('/check-transaction', payment.raw)
        require(code == 200 and response['outcome'] == outcome, f'{name}: {code} {response}')
        self.record['controls'].append({'name': name, 'height': self.height, 'txid': payment.id,
            'tx_hex': payment.raw.hex(), 'decision': outcome, 'http_status': code, 'response': response})

    def prepare(self):
        for node in NODES:
            info = wait('fresh reference RPC', lambda: rpc(node, 'getblockchaininfo'))
            require(info['chain'] == 'regtest' and info['blocks'] == 0, 'scenario requires fresh regtest references')
        self.save_block(block(NODES[0], info['bestblockhash']), 'genesis', 'genesis')
        # Wait for the two peers to exchange signed status before the first local block.
        ids = ['12D3KooWAFXWuxgdJoRsaA4J4RRRr8yu6WCrAPf8FaS7UfZg3ceG', '12D3KooWG6aCkDmi5tqx4G4AvVDTQdSVvTSzzQvk1vh9CtSR8KEW']
        for node, other in zip(NODES, reversed(ids)):
            wait('reference peering', lambda: any(p.get('peerid') == other for p in rpc(node, 'getpeerinfo')))
        _, first, outputs = self.reference('maturity', payout=self.wallets[0])
        coin = Coin(txid(first[0]), 0, outputs[0][0][0], 0)
        for _ in range(100):
            self.reference('maturity')
        # Eight background balances, sixteen FIFO cash accounts with 1, 2 or 3
        # deposits, two held savings deposits and one later mixed-source payment.
        depths = [1]*8 + [2]*4 + [3]*4
        slots = 8 + sum(depths) + 3
        amount, rem = divmod(coin.value - FEE, slots)
        fanout = pay(self.wallets, [coin], [(i % 4, amount + (rem if i == 0 else 0)) for i in range(slots)])
        self.reference('seed', [fanout])
        self.lanes = fanout.outputs[:8]
        cursor = 8
        for queue, depth in zip(self.accounts, depths):
            queue.extend(fanout.outputs[cursor:cursor+depth])
            cursor += depth
        self.dormant = fanout.outputs[cursor:]
        for _ in range(40):
            self.reference('history', self.lane_payments())
        self.reference('join', self.lane_payments(3))
        self.record['join_height'] = self.height
        fates = {'all': 0, 'some': 0, 'none': 0}
        for outs in self.history_outputs.values():
            left = sum(key in self.ledger for key in outs)
            fates['none' if left == 0 else 'all' if left == len(outs) else 'some'] += 1
        self.record['history_output_fates'] = fates
        require(all(fates.values()), 'history must expose all three lifecycle states')
        print(f'Prepared {len(self.history_outputs)} payments. Fresh entrant joins at {self.height}.', file=sys.stderr)
        compose('up', '-d', '--no-build', '--no-deps', 'scary-node')
        wait('fresh entrant connects to native peers', lambda: status()['peer']['connected_peer_ids_seen'] == 2)
        self.capture = True
        self.checkpoint()
        initial = self.record['checkpoints'][-1]['mining']
        require(initial['headers'] == 1 and not initial['observation_window_ready'], 'fresh observation gate mismatch')

    def observe(self):
        for i in range(12):
            items = self.lane_payments(3)
            items.append(self.account_payment(i % 4))
            if i == 4:
                # Both spends are valid. The dormant source predates the entrant,
                # while the other source is from a block in its observed window.
                old, new = self.dormant.pop(), self.lanes[(self.activity+1) % 8]
                mixed = pay(self.wallets, [old, new], [(self.destination(new.owner), old.value+new.value-FEE)])
                self.control('mixed-before-consumption', mixed, 'deferred')
                self.lanes[(self.activity+1) % 8] = mixed.outputs[0]
                items.append(mixed)
            if i == 6:
                historic = self.transfer(self.dormant.pop())
                self.control('historical-before-consumption', historic, 'deferred')
                items.append(historic)
            if i == 8:
                source = self.lanes[(self.activity+1) % 8]
                valid = self.transfer(source)
                self.control('same-spend-before-consumption', valid, 'determinate')
                self.lanes[(self.activity+1) % 8] = valid.outputs[0]
                items.append(valid)
            if i == 10:
                # Parent and child enter the same honest miner's assembly in dependency order.
                parent = items[0]
                child = self.transfer(parent.outputs[0])
                self.lanes[(self.activity - 3) % len(self.lanes)] = child.outputs[0]
                items.append(child)
            self.reference('observation', items)
            if i == 4:
                self.control('mixed-after-consumption', mixed, 'invalid')
            if i == 6:
                self.control('historical-after-consumption', historic, 'invalid')
            if i == 8:
                self.control('same-spend-after-consumption', valid, 'invalid')
        ready = self.record['checkpoints'][-1]['mining']
        require(ready['headers'] == 13 and ready['observation_window_ready'], 'window did not open after 12 successors')

    def candidate(self, round_index):
        parent = self.tip
        before_job = status()['mining']
        require(before_job['observation_window_ready'], 'worker job requested before local gate opened')
        next_ref = NODES[self.ref_index]
        offered, deferred, admitted = [], [], []
        for account in range(len(self.accounts)):
            payment = self.account_payment(account)
            reference_id = rpc(next_ref, 'sendrawtransaction', payment.raw.hex())
            require(reference_id == payment.id, 'common reference offer ID differs')
            code, response = api_post('/transactions', payment.raw)
            require(code in (202, 422), f'expected entrant admission decision: {code} {response}')
            if code == 202:
                require(response['txid'] == payment.id, 'admitted ID mismatch')
                admitted.append(payment)
            else:
                deferred.append(payment)
            offered.append({'account': account, 'txid': payment.id, 'tx_hex': payment.raw.hex(),
                'decision': 'admitted' if code == 202 else 'deferred', 'http_status': code,
                'reason': response.get('error'), 'response': response, 'reference_node': f'tn{self.ref_index+1}',
                'reference_admitted_txid': reference_id})
        require(admitted, 'no observed payments to mine')
        ids = [p.id for p in admitted]
        job, header, cb = solve(f'scenario-worker-{round_index}', parent, ids)
        bh = txid(header)
        wait('entrant published candidate natively', lambda: status()['peer']['last_published'] == bh)
        receipts = []
        for name, node in zip(('tn1', 'tn2'), NODES):
            wait(f'{name} independently adopts candidate', lambda: assert_tip(node, bh))
            data = wait('reference serves candidate', lambda: block(node, bh))
            raw, txs, _ = data
            require(raw[:80] == header and txs[0] == cb and set(txs[1:]) == {p.raw for p in admitted}, 'candidate differs from Stratum work')
            marker = f'Processing block {bh} from peer '
            lines = compose('logs', '--no-color', '--no-log-prefix', name, capture=True).splitlines()
            sources = sorted({s.split(' at ',1)[1].strip() for line in lines if marker in line
                              for s in [line.split(marker,1)[1]] if ' at ' in s})
            receipts.append({'node': name, 'height': rpc(node, 'getblock', bh, 1)['height'], 'hash': bh,
                             'body_sha256': hashlib.sha256(raw).hexdigest(), 'native_block_sources': sources})
        require(receipts[0]['body_sha256'] == receipts[1]['body_sha256'], 'candidate body digests differ')
        self.save_block(data, 'scary-node', 'stratum')
        self.checkpoint()
        routes = set()
        for item in node_events('scary-node'):
            if item.get('event') == 'asset_request' and item.get('block') == bh:
                routes.add(item['asset'])
        require(routes, 'no native Asset request for solved candidate')
        row = {'height': self.height, 'hash': bh, 'parent': parent, 'job_id': job,
               'parent_observed_headers': before_job['headers'], 'parent_window_ready': before_job['observation_window_ready'],
               'offered': offered, 'mined_txids': [txid(t) for t in txs[1:]], 'wire_bytes': len(raw),
               'reference_receipts': receipts, 'asset_requests': sorted(routes)}
        self.record['candidates'].append(row)
        print(f'Rust block {self.height} contains {len(admitted)} of 16 offers ({len(raw)} bytes).', file=sys.stderr)
        self.reference('continuation', self.lane_payments(3), already=deferred)
        row['successor'] = self.tip
        if round_index == 2:
            compose('up', '-d', '--no-build', '--no-deps', 'cold-observer')
            cold_url = 'http://127.0.0.1:19581/status'
            def cold_at(tip):
                view = json.loads(request(cold_url))
                return (view if view['network'] == 'regtest' and view['peer']['connected_peer_ids_seen'] >= 2
                        and view['mining']['tip_hash'] == tip else None)
            entry = wait('second observer joins at the candidate successor', lambda: cold_at(self.tip))
            require(entry['mining']['headers'] == 1, 'passive observer already has prior history')
        data = self.reference('continuation', self.lane_payments(3))
        if round_index == 2:
            mature, cold = status(), wait('both observers evaluate same last block', lambda: cold_at(self.tip))
            old, new = mature['mining']['validation_window'][-1], cold['mining']['validation_window'][-1]
            require(old['hash'] == new['hash'] == self.tip and new['height'] == old['height'] == self.height,
                    'matched observers did not inspect the same block')
            require(cold['mining']['headers'] == 2 and mature['mining']['headers'] > 20,
                    'matched observers do not have different contiguous observation spans')
            self.record['matched_observers'] = {'cold_join_height': entry['mining']['height'],
                'cold_join_hash': entry['mining']['tip_hash'], 'height': self.height, 'hash': self.tip,
                'body_sha256': hashlib.sha256(data[0]).hexdigest(), 'cold': cold, 'mature': mature}

    def run(self):
        self.prepare()
        self.observe()
        for i in range(3):
            self.candidate(i)
        batches = self.record['candidates']
        require(all(len(b['offered']) == 16 for b in batches), 'offered demand differs across rounds')
        digest = {name: compose('exec', '-T', name, 'sha256sum', '/usr/local/bin/scary-node',
                                capture=True).split()[0] for name in ('scary-node', 'cold-observer')}
        require(digest['scary-node'] == digest['cold-observer'], 'matched observers ran different binaries')
        self.record['rust_binary_sha256'] = digest
        evidence = {}
        for label, service in (('mature', 'scary-node'), ('cold', 'cold-observer')):
            events = list(node_events(service))
            evidence[label] = {'sha256': digest[service],
                'body_observed_heights': [event['height'] for event in events if event.get('event') == 'body_observed'],
                'coverage_resets': sum(event.get('event') == 'coverage_reset' for event in events)}
        self.record['process_evidence'] = evidence
        self.record['observed_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        return self.record


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seed', type=int, default=7, help='destination schedule seed; keys and PoW are fresh each run')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        wallets = [DisposableWallet(Path(tmp) / f'{i}.pem') for i in range(4)]
        print(json.dumps(Scenario(wallets, args.seed).run(), separators=(',', ':')))
