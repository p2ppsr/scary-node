"""Replay a Scary Node scenario receipt offline with Python 3 and OpenSSL."""
import argparse
import gzip
import hashlib
import json
import struct
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from prove import decode_block, dsha, read_varint, txid
from verify_receipt import check_header, require

GENESIS = "0f9188f13cb7b2c71f2a335e3a4fc328bf5beb436012afca590b1a11466e2206"
NULL = ("00" * 32, 0xffffffff)
MAX_MONEY = 21_000_000 * 100_000_000
SECP_ORDER = 0xfffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd0364141


def p2pkh(script):
    return len(script) == 25 and script[:3] == b"\x76\xa9\x14" and script[-2:] == b"\x88\xac"


def verify_address(party):
    script = bytes.fromhex(party['locking_script_hex'])
    require(p2pkh(script), 'expected a P2PKH party script')
    alphabet = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    value = 0
    for char in party['address']:
        value = value * 58 + alphabet.index(char)
    payload = b'\x6f' + script[3:23]
    require(value.to_bytes(25, 'big') == payload + dsha(payload)[:4] and not party['address'].startswith('1'),
            'party address differs from regtest locking script')
    return script


def compact(n):
    if n < 253:
        return bytes([n])
    if n <= 0xffff:
        return b"\xfd" + n.to_bytes(2, "little")
    if n <= 0xffffffff:
        return b"\xfe" + n.to_bytes(4, "little")
    return b"\xff" + n.to_bytes(8, "little")


@dataclass(frozen=True)
class Input:
    point: tuple
    wire_point: bytes
    script: bytes
    sequence: bytes


@dataclass
class Transaction:
    id: str
    raw: bytes
    inputs: list
    outputs: list
    serialized_outputs: bytes

    @classmethod
    def parse(cls, data):
        count, pos = read_varint(data, 4)
        require(0 < count <= len(data) // 41, "bad transaction input count")
        inputs = []
        for _ in range(count):
            point = data[pos:pos+36]
            size, pos = read_varint(data, pos+36)
            script = data[pos:pos+size]
            pos += size
            index = struct.unpack_from("<I", point, 32)[0]
            inputs.append(Input((point[:32][::-1].hex(), index), point, script, data[pos:pos+4]))
            pos += 4
        count, pos = read_varint(data, pos)
        require(0 < count <= len(data) // 9, "bad transaction output count")
        first_output = pos
        outputs = []
        for _ in range(count):
            value = struct.unpack_from("<Q", data, pos)[0]
            size, pos = read_varint(data, pos+8)
            outputs.append((value, data[pos:pos+size]))
            pos += size
        require(pos + 4 == len(data), "transaction has trailing or missing bytes")
        require(sum(value for value, _ in outputs) <= MAX_MONEY, "transaction exceeds money range")
        return cls(txid(data), data, inputs, outputs, data[first_output:pos])

    def scoped_context(self):
        return (self.raw[:4] == b"\x01\x00\x00\x00" and self.raw[-4:] == bytes(4)
                and all(i.sequence == b"\xff" * 4 for i in self.inputs))


@dataclass(frozen=True)
class Coin:
    value: int
    script: bytes
    height: int
    coinbase: bool

    def eligible(self, tip_height):
        return tip_height > self.height and (not self.coinbase or tip_height + 1 - self.height >= 100)


def unlock_parts(script):
    if len(script) < 10:
        return None
    sig_size = script[0]
    if not 9 <= sig_size <= 73 or len(script) != 1 + sig_size + 34:
        return None
    sig, pub = script[1:sig_size], script[sig_size+2:]
    if script[sig_size] != 0x41 or script[sig_size+1] != 33 or pub[0] not in (2, 3):
        return None
    if len(sig) < 8 or sig[0] != 0x30 or sig[1] != len(sig)-2 or sig[2] != 2:
        return None
    end_r = 4 + sig[3]
    if end_r + 2 > len(sig) or sig[end_r] != 2 or end_r + 2 + sig[end_r+1] != len(sig):
        return None
    r, s = sig[4:end_r], sig[end_r+2:]
    for n in (r, s):
        if not n or n[0] & 128 or (len(n) > 1 and n[0] == 0 and not n[1] & 128):
            return None
    if not 0 < int.from_bytes(r, 'big') < SECP_ORDER or not 0 < int.from_bytes(s, 'big') <= SECP_ORDER // 2:
        return None
    return sig, pub


class Signatures:
    """Verify the scenario's compressed-key P2PKH ALL|FORKID signatures independently."""
    def __init__(self, directory):
        self.directory = Path(directory)
        self.checked = set()

    def verify(self, tx, idx, coin):
        parts = unlock_parts(tx.inputs[idx].script)
        if not p2pkh(coin.script) or not parts:
            return False
        key = (tx.id, idx, coin.value, coin.script)
        if key in self.checked:
            return True
        signature, pub = parts
        require(hashlib.new("ripemd160", hashlib.sha256(pub).digest()).digest() == coin.script[3:23],
                f"P2PKH public-key hash mismatch in {tx.id} input {idx}")
        item = tx.inputs[idx]
        preimage = (tx.raw[:4] + dsha(b''.join(i.wire_point for i in tx.inputs))
                    + dsha(b''.join(i.sequence for i in tx.inputs)) + item.wire_point
                    + compact(len(coin.script)) + coin.script + struct.pack('<Q', coin.value)
                    + item.sequence + dsha(tx.serialized_outputs) + tx.raw[-4:] + struct.pack('<I', 0x41))
        # SubjectPublicKeyInfo for a named secp256k1 curve and compressed point.
        public_file = self.directory / (pub.hex() + '.der')
        if not public_file.exists():
            public_file.write_bytes(bytes.fromhex('3036301006072a8648ce3d020106052b8104000a032200') + pub)
        sig_file = self.directory / 'signature.der'
        sig_file.write_bytes(signature)
        result = subprocess.run(['openssl', 'pkeyutl', '-verify', '-pubin', '-keyform', 'DER',
                                 '-inkey', str(public_file), '-sigfile', str(sig_file)],
                                input=dsha(preimage), capture_output=True)
        require(result.returncode == 0, f"P2PKH signature failed in {tx.id} input {idx}")
        self.checked.add(key)
        return True


class Ledger:
    def __init__(self, join, signatures):
        self.join = join
        self.signatures = signatures
        self.live = {}
        self.witnessed = {}
        self.consumed = set()
        self.inclusion_heights = {}

    def connect(self, height, transactions):
        counters = dict.fromkeys(('known_inputs', 'unknown_inputs', 'same_block_inputs',
                                  'script_checks_passed', 'script_checks_deferred', 'amount_checks_passed',
                                  'amount_checks_deferred', 'transactions_determinate', 'transactions_deferred'), 0)
        actual_fees = 0
        for pos, tx in enumerate(transactions):
            require(tx.id not in self.inclusion_heights, f'duplicate scenario transaction {tx.id}')
            self.inclusion_heights[tx.id] = height
            if pos == 0:
                require(len(tx.inputs) == 1 and tx.inputs[0].point == NULL, 'missing first coinbase')
            else:
                total_in = 0
                all_inputs_known = True
                all_scripts_checked = True
                for idx, item in enumerate(tx.inputs):
                    point = item.point
                    require(point in self.live, f'missing or already consumed full-ledger input {point} at {height}')
                    coin = self.live.pop(point)
                    require(not coin.coinbase or height - coin.height >= 100,
                            f'immature coinbase in block {height}')
                    total_in += coin.value
                    checked = self.signatures.verify(tx, idx, coin)
                    require(checked, f'fixture input outside independently verified P2PKH scope: {tx.id}:{idx}')
                    # Seeing the input consumed reveals that it is spent even
                    # when its creation predates the observation boundary.
                    if height >= self.join:
                        self.consumed.add(point)
                    if point in self.witnessed:
                        self.witnessed.pop(point)
                        counters['known_inputs'] += 1
                        counters['same_block_inputs'] += coin.height == height
                        counters['script_checks_passed' if checked else 'script_checks_deferred'] += 1
                        all_scripts_checked &= checked
                    else:
                        counters['unknown_inputs'] += 1
                        all_inputs_known = False
                output_value = sum(v for v, _ in tx.outputs)
                require(output_value <= total_in <= MAX_MONEY, f'value imbalance in {tx.id}')
                actual_fees += total_in - output_value
                counters['amount_checks_passed' if all_inputs_known else 'amount_checks_deferred'] += 1
                complete = all_inputs_known and all_scripts_checked and tx.scoped_context()
                counters['transactions_determinate' if complete else 'transactions_deferred'] += 1
            for i, (value, script) in enumerate(tx.outputs):
                # Genesis reward is permanently unspendable. Exclude conventional data outputs.
                if height == 0 or script.startswith(b'\x00\x6a'):
                    continue
                point = (tx.id, i)
                coin = Coin(value, script, height, pos == 0)
                self.live[point] = coin
                if height >= self.join and p2pkh(script):
                    self.witnessed[point] = coin
        reward = sum(value for value, _ in transactions[0].outputs)
        require(reward <= (5_000_000_000 >> (height // 150)) + actual_fees, f'excess coinbase reward at {height}')
        counters['noncoinbase_transactions'] = len(transactions) - 1
        counters['coinbase_reward_checked'] = (counters['amount_checks_deferred'] == 0)
        return counters, actual_fees

    def counts(self, actors, height):
        return {'utxos': len(self.live), 'utxos_p2pkh': sum(p2pkh(c.script) for c in self.live.values()),
                'immature_coinbase_utxos': sum(c.coinbase and height + 1 - c.height < 100
                                              for c in self.live.values()),
                'witnessed_utxos': len(self.witnessed) if height >= self.join else None,
                'eligible_utxos': sum(c.eligible(height) for c in self.witnessed.values()) if height >= self.join else None,
                'actor_utxos': sum(c.script in actors for c in self.live.values()),
                'actor_witnessed_utxos': sum(c.script in actors for c in self.witnessed.values()) if height >= self.join else None}


def indexed(rows, field, label):
    result = {row[field]: row for row in rows}
    require(len(result) == len(rows), f'duplicate {label} {field}')
    return result


def verify_checkpoint(checkpoint, block, ledger, counters, counts):
    height = block['height']
    mining = checkpoint['mining']
    require(checkpoint['tip_hash'] == mining['tip_hash'] == block['hash'], f'wrong checkpoint tip at {height}')
    require(mining['height'] == height and mining['headers'] == min(147, height-ledger.join+1),
            f'noncontiguous observation at {height}')
    require(mining['witnessed_outputs'] == len(ledger.witnessed) and mining['omitted_outputs'] == 0,
            f'witnessed output count mismatch at {height}')
    require(mining['awaiting_height'] is None and mining['suspended_reason'] is None,
            f'incomplete entrant state at {height}')
    require(mining['min_observed_blocks'] == 13 and mining['observation_window_ready'] is (mining['headers'] >= 13),
            f'observation-window gate mismatch at {height}')
    for field, computed in [('utxo_count', 'utxos'), ('actor_utxo_count', 'actor_utxos'),
                            ('witnessed_actor_utxo_count', 'actor_witnessed_utxos')]:
        require(checkpoint[field] == counts[computed], f'{field} denominator mismatch at {height}')
    observations = [row for row in mining['validation_window'] if row['height'] == height]
    require(len(observations) == 1 and observations[0]['hash'] == block['hash'],
            f'missing local validation observation at {height}')
    actual = observations[0]
    for key, expected in counters.items():
        require(actual[key] == expected, f'{key} mismatch at {height}: observed {actual[key]}, replayed {expected}')
    require(actual['consumed_inputs'] == 0, f'accepted block has recently consumed input at {height}')
    require(actual['script_checks_failed'] == 0, f'accepted block has a failed signature at {height}')
    for row in checkpoint['outpoints']:
        point = (row['txid'], row['vout'])
        coin = ledger.witnessed.get(point)
        require(row['known'] is bool(coin) and row['consumed'] is (point in ledger.consumed),
                f'point lookup mismatch at {height}: {point}')
        require(row['eligible'] is bool(coin and coin.eligible(height)), f'point eligibility mismatch for {point}')
        if coin:
            require((row['value_satoshis'], row['height'], row['coinbase']) == (coin.value, coin.height, coin.coinbase),
                    f'point metadata mismatch at {height}: {point}')
    return actual


def verify_control(row, ledger):
    tx = Transaction.parse(bytes.fromhex(row['tx_hex']))
    require(tx.id == row['txid'], f'control ID mismatch for {row["name"]}')
    coins = [ledger.witnessed.get(item.point) for item in tx.inputs]
    known = sum(bool(c) for c in coins)
    consumed = sum(i.point in ledger.consumed for i in tx.inputs)
    unknown = len(coins) - consumed - known
    passed = sum(ledger.signatures.verify(tx, idx, coin) for idx, coin in enumerate(coins) if coin)
    if consumed:
        decision = 'invalid'
    elif unknown or passed != known or not tx.scoped_context():
        decision = 'deferred'
    else:
        require(sum(c.value for c in coins) >= sum(v for v, _ in tx.outputs), 'control overspend')
        decision = 'determinate'
    response = row['response']
    require(row['http_status'] == 200 and decision == row['decision'] == response['outcome'],
            f'wrong control outcome for {row["name"]}')
    for key, expected in [('known_inputs', known), ('unknown_inputs', unknown), ('consumed_inputs', consumed),
                          ('script_checks_passed', passed), ('script_checks_deferred', known - passed),
                          ('script_checks_failed', 0), ('amount_checks_passed', int(not (unknown or consumed))),
                          ('amount_checks_deferred', int(bool(unknown or consumed))),
                          ('transactions_determinate', int(decision == 'determinate')),
                          ('transactions_deferred', int(decision == 'deferred'))]:
        require(response[key] == expected, f'wrong control {key} for {row["name"]}')
    return {'name': row['name'], 'height': row['height'], 'txid': tx.id, 'decision': decision,
            'known_inputs': known, 'unknown_inputs': unknown, 'consumed_inputs': consumed,
            'script_checks_passed': passed}


def verify_offers(candidate, ledger, height):
    seen = set()
    admitted = set()
    cohort = set()
    known_amount = 0
    reasons = Counter()
    for offer in candidate['offered']:
        tx = Transaction.parse(bytes.fromhex(offer['tx_hex']))
        require(tx.id == offer['txid'] == offer['reference_admitted_txid'], 'offered/reference txid mismatch')
        require(tx.id not in seen and offer['reference_node'] in ('tn1', 'tn2'), 'missing or duplicate reference admission')
        seen.add(tx.id)
        require(offer['account'] not in cohort, 'duplicate payer in offered cohort')
        cohort.add(offer['account'])
        input_coins = []
        for idx, item in enumerate(tx.inputs):
            require(item.point in ledger.live, f'offer spends unavailable full-ledger outpoint {item.point}')
            coin = ledger.live[item.point]
            require(not coin.coinbase or height - coin.height >= 100, 'offer spends immature coinbase')
            require(ledger.signatures.verify(tx, idx, coin), 'offer outside fixture signature class')
            input_coins.append(coin)
        fee = sum(coin.value for coin in input_coins) - sum(value for value, _ in tx.outputs)
        require(fee >= 0 and tx.scoped_context() and all(p2pkh(script) for _, script in tx.outputs),
                'offer outside fixture payment class')
        # The admission API checks each input in wire order.
        reason = None
        for item, coin in zip(tx.inputs, input_coins):
            if item.point not in ledger.witnessed:
                reason = 'input missing from witnessed spendable set'
                break
            if coin.height == height - 1:
                reason = 'creation block has no observed successor'
                break
            if not coin.eligible(height - 1):
                reason = 'immature coinbase'
                break
        eligible = reason is None
        expected = 'admitted' if eligible else 'deferred'
        require(offer['decision'] == expected, f'offer decision disagrees with replay for {tx.id}')
        require(offer['http_status'] == (202 if eligible else 422), f'HTTP outcome disagrees for {tx.id}')
        if eligible:
            require(offer['response']['txid'] == tx.id, 'admission response names different transaction')
            admitted.add(tx.id)
            known_amount += fee
        else:
            require(offer['response']['error'] == offer['reason'] == reason, 'API deferral reason differs from replay')
            reasons[reason] += 1
    return cohort, admitted, known_amount, dict(reasons)


def verify_candidate(candidate, block, txs, raw, admitted, fees):
    require(candidate['hash'] == block['hash'] and candidate['parent'] == raw[4:36][::-1].hex(), 'wrong job parent or hash')
    require(bool(candidate['job_id']) and candidate['wire_bytes'] == len(raw), 'wrong candidate ID or size')
    mined = [t.id for t in txs[1:]]
    require(candidate['mined_txids'] == mined and set(mined) == admitted, 'job body differs from recorded admissions')
    require(sum(v for v, _ in txs[0].outputs) == (5_000_000_000 >> (block['height'] // 150)) + fees,
            f'candidate coinbase does not collect the exact fees at {block["height"]}')
    peers = indexed(candidate['reference_receipts'], 'node', 'reference')
    require(set(peers) == {'tn1', 'tn2'}, 'two independent reference receipts required')
    digest = hashlib.sha256(raw).hexdigest()
    all_sources = set()
    for node, row in peers.items():
        require((row['hash'], row['height'], row['body_sha256']) == (block['hash'], block['height'], digest),
                f'reference body or active hash mismatch on {node}')
        # A native relay may reach a reference before the original publisher.
        require(row['native_block_sources'], f'missing native block notification at {node}')
        all_sources.update(row['native_block_sources'])
        if 'transaction_count' in row:
            require(row['transaction_count'] == len(txs), f'wrong tx count at {node}')
    require('http://scary-node:9090/api/v1' in all_sources, 'no reference recorded the entrant native source')
    require('block' in candidate['asset_requests'], 'entrant did not observe native block asset request')
    n = len(candidate['offered'])
    return {'height': block['height'], 'hash': block['hash'], 'offered': n, 'admitted': len(admitted),
            'deferred': n - len(admitted), 'noncoinbase_transactions': len(mined), 'wire_bytes': len(raw),
            'fees_satoshis': fees, 'reference_nodes': sorted(peers)}


def verify_matched(pair, blocks, cold, cold_checks, mature, mature_checks):
    tip = blocks[-1]
    height, hash_ = tip['height'], tip['hash']
    require(pair['cold_join_height'] == height - 1 and pair['cold_join_hash'] == blocks[-2]['hash'],
            'cold observer did not start at the penultimate recorded block')
    require((pair['height'], pair['hash']) == (height, hash_) and tip['miner'] in ('tn1', 'tn2'),
            'observers did not measure the same final reference block')
    digest = hashlib.sha256(bytes.fromhex(tip['wire_hex'])).hexdigest()
    require(digest == pair['body_sha256'], 'matched observer body digest mismatch')
    result = {'height': height, 'hash': hash_, 'cold_join_height': cold.join, 'body_sha256': digest}
    keys = ('noncoinbase_transactions', 'known_inputs', 'unknown_inputs', 'script_checks_passed',
            'amount_checks_passed', 'transactions_determinate', 'transactions_deferred')
    for label, ledger, counts in [('cold', cold, cold_checks), ('mature', mature, mature_checks)]:
        status = pair[label]
        state, peer = status['mining'], status['peer']
        require(status['network'] == 'regtest' and (state['height'], state['tip_hash']) == (height, hash_),
                f'{label} matched observer at wrong tip')
        require(state['headers'] == min(147, height - ledger.join + 1)
                and state['witnessed_outputs'] == len(ledger.witnessed) and state['awaiting_height'] is None,
                f'{label} observer did not retain its expected contiguous window')
        require(peer['connected_peer_ids_seen'] >= 2 and peer['coverage_gaps'] == 0,
                f'{label} matched observer lacks contiguous two-peer evidence')
        announcements = [row for row in (peer['last_block_notice'], peer['last_peer_status']) if row
                         and (row['height'], row['hash']) == (height, hash_) and row['peer']]
        require(announcements, f'{label} matched observation lacks a native announcement for its tip')
        receipt = state['validation_window'][-1]
        require((receipt['height'], receipt['hash']) == (height, hash_), f'{label} missing matched local observation')
        for key, value in counts.items():
            require(value == receipt[key], f'{label} matched observer {key} differs from replay')
        require(receipt['script_checks_failed'] == 0 and receipt['consumed_inputs'] == 0,
                f'{label} matched observer recorded a violation')
        result[label] = {'headers': state['headers'], 'witnessed_utxos': len(ledger.witnessed),
                         'native_peers_seen': peer['connected_peer_ids_seen'],
                         'tip_announcements': [row['event'] for row in announcements],
                         **{key: receipt[key] for key in keys}}
    return result


def verify_process_evidence(record, final):
    executables = record['rust_binary_sha256']
    require(set(executables) == {'scary-node', 'cold-observer'}, 'missing running executable digests')
    digest = executables['scary-node']
    require(isinstance(digest, str) and len(digest) == 64 and all(c in '0123456789abcdef' for c in digest)
            and executables['cold-observer'] == digest, 'observers report different or malformed executable hashes')
    logs = record['process_evidence']
    require(set(logs) == {'mature', 'cold'}, 'missing observer process log coverage')
    for label, start in [('mature', record['join_height']), ('cold', final - 1)]:
        evidence = logs[label]
        require(evidence['sha256'] == digest and evidence['coverage_resets'] == 0
                and evidence['body_observed_heights'] == list(range(start, final + 1)),
                f'{label} process log body coverage differs from the clean scenario interval')
    return digest


def verify(record):
    require(record['network'] == 'regtest' and record['teranode_release'] == 'v0.16.1', 'wrong network or reference release')
    blocks = record['blocks']
    join = record['join_height']
    require(0 < join < len(blocks) and blocks[0]['hash'] == GENESIS, 'invalid genesis or join')
    binary = verify_process_evidence(record, len(blocks) - 1)
    require(blocks[join]['phase'] == 'join', 'entry height does not identify the observation anchor')
    checkpoints = indexed(record['checkpoints'], 'height', 'checkpoint')
    candidates = indexed(record['candidates'], 'height', 'candidate')
    controls = defaultdict(list)
    for row in record['controls']:
        require(join <= row['height'] < len(blocks), 'control outside observation interval')
        controls[row['height']].append(row)
    require(set(checkpoints) == set(range(join, len(blocks))), 'one runtime checkpoint per observed height required')
    history = [b for b in blocks if b['phase'] == 'history']
    require([b['height'] for b in history] == list(range(join - 40, join)), 'expected 40 pre-entry history blocks')
    require({b['miner'] for b in history} == {'tn1', 'tn2'}
            and all(a['miner'] != b['miner'] for a, b in zip(history, history[1:])), 'history miners did not alternate')
    require([b['height'] for b in blocks if b['phase'] == 'observation'] == list(range(join+1, join+13)),
            'expected 12 contiguous observation successors')
    actor_scripts = {verify_address(p) for p in record['parties']}
    require(len(actor_scripts) == len(indexed(record['parties'], 'id', 'party')) >= 2,
            'party keys must identify distinct P2PKH owners')
    timeline, job_results, control_results, history_outputs = [], [], [], []
    cohorts = []
    previous = None
    with tempfile.TemporaryDirectory() as directory:
        ledger = Ledger(join, Signatures(directory))
        cold = Ledger(len(blocks) - 2, ledger.signatures)
        for height, block in enumerate(blocks):
            require(height == block['height'], 'block sequence missing or out of order')
            raw = bytes.fromhex(block['wire_hex'])
            check_header(raw[:80], block['hash'], previous)
            previous = block['hash']
            _, wire_txs, _ = decode_block(raw, block['hash'])
            txs = [Transaction.parse(wire) for wire in wire_txs]
            candidate = candidates.get(height)
            require(bool(candidate) == (block['phase'] == 'stratum'), 'unreported or mislabelled Stratum round')
            if candidate:
                parent = checkpoints[height-1]['mining']
                require(parent['tip_hash'] == candidate['parent'] and parent['observation_window_ready']
                        and parent['headers'] >= 13, 'Stratum candidate preceded a ready matching parent')
                cohort, admitted, expected_fees, reasons = verify_offers(candidate, ledger, height)
                cohorts.append(cohort)
                require(height+1 < len(blocks) and blocks[height+1]['phase'] == 'continuation',
                        'no recorded active reference successor of Rust job')
                require(candidate['successor'] == blocks[height+1]['hash'], 'claimed candidate successor differs')
            if block['phase'] == 'history':
                require(height < join and len(txs) == 9, 'history block must contain 8 ordinary payments')
                history_outputs.extend([(tx.id, len(tx.outputs)) for tx in txs[1:]])
            if block['phase'] == 'observation':
                require(len(txs) > 1 and block['miner'] in ('tn1', 'tn2'), 'empty or non-reference observation successor')
            if height == cold.join:
                # Copy only the independent replay's full-history baseline. The
                # cold witnessed ledger starts empty and sees exactly two bodies.
                cold.live = ledger.live.copy()
                cold.inclusion_heights = ledger.inclusion_heights.copy()
            if height >= cold.join:
                cold_checks, _ = cold.connect(height, txs)
            counters, fees = ledger.connect(height, txs)
            if candidate:
                require(fees == expected_fees, 'offer fees disagree with block fees')
                job = verify_candidate(candidate, block, txs, raw, admitted, fees)
                job['deferred_by_reason'] = reasons
                job_results.append(job)
            if height == join:
                fates = dict.fromkeys(('all', 'some', 'none'), 0)
                history_outputs_created = sum(n for _, n in history_outputs)
                history_outputs_live_at_entry = 0
                for id_, n in history_outputs:
                    remaining = sum((id_, i) in ledger.live for i in range(n))
                    history_outputs_live_at_entry += remaining
                    fates['all' if remaining == n else 'some' if remaining else 'none'] += 1
                require(record['history_output_fates'] == fates and all(fates.values()),
                        f'history transactions must have all three remaining-output fates at entry: {fates}')
            counts = ledger.counts(actor_scripts, height)
            actual = verify_checkpoint(checkpoints[height], block, ledger, counters, counts) if height >= join else {}
            control_results.extend(verify_control(row, ledger) for row in controls[height])
            point = {key: block[key] for key in ('height', 'hash', 'phase', 'miner')}
            point.update(counts)
            point.update({'wire_bytes': len(raw), 'noncoinbase_transactions': len(txs) - 1,
                          'data_outputs_created': sum(script.startswith(b'\x00\x6a') for t in txs for _, script in t.outputs),
                          'inputs': sum(len(t.inputs) for t in txs[1:])})
            point.update({key: actual.get(key) for key in counters if key != 'noncoinbase_transactions'})
            timeline.append(point)
        independent_signatures = len(ledger.signatures.checked)
        paying_scripts = {script for _, _, _, script in ledger.signatures.checked}
        require(actor_scripts <= paying_scripts, 'listed party has no independently verified signed payment')
        matched = verify_matched(record['matched_observers'], blocks, cold, cold_checks, ledger, counters)
    require(len(job_results) == len(candidates) == 3 and all(c == set(range(16)) for c in cohorts),
            'three repeated opportunities with accounts 0 through 15 required')
    for job in job_results:
        deferred_ids = [offer['txid'] for offer in candidates[job['height']]['offered']
                        if offer['decision'] == 'deferred']
        require(all(ledger.inclusion_heights.get(txid_) == job['height'] + 1 for txid_ in deferred_ids),
                f'deferred offers missing from reference successor of {job["height"]}')
        job['deferred_confirmed_in_successor'] = len(deferred_ids)
    for control in control_results:
        control['included_height'] = ledger.inclusion_heights.get(control['txid'])
    controls_by_name = indexed(control_results, 'name', 'control')
    for family, known, unknown in [('historical', 0, 1), ('mixed', 1, 1), ('same-spend', 1, 0)]:
        before = controls_by_name[f'{family}-before-consumption']
        after = controls_by_name[f'{family}-after-consumption']
        require((before['known_inputs'], before['unknown_inputs'], before['script_checks_passed']) ==
                (known, unknown, known), f'{family} control has the wrong input mix')
        require(before['decision'] == ('deferred' if unknown else 'determinate')
                and after['decision'] == 'invalid' and after['consumed_inputs'] == known + unknown
                and before['txid'] == after['txid'] and before['included_height'] is not None
                and before['height'] < before['included_height'] <= after['height'],
                f'{family} control does not bracket the mined spend of the same bytes')
    require(any(row['same_block_inputs'] for row in timeline if row['phase'] == 'observation'),
            'missing observed spend of a same-block output')
    return {'join_height': join, 'final_height': len(blocks) - 1, 'history_output_fates': fates,
            'history_outputs_created': history_outputs_created,
            'history_outputs_live_at_entry': history_outputs_live_at_entry,
            'utxo_denominator': 'Active unspent outputs including immature coinbases. Genesis reward and '
                                'OP_FALSE OP_RETURN outputs are excluded. Maturity is evaluated at the next block.',
            'timeline': timeline, 'candidates': job_results, 'controls': control_results,
            'matched_observers': matched,
            'rust_binary_sha256': binary, 'process_evidence': record['process_evidence'],
            'admitted_delta': job_results[-1]['admitted'] - job_results[0]['admitted'],
            'wire_bytes_delta': job_results[-1]['wire_bytes'] - job_results[0]['wire_bytes'],
            'independently_verified_p2pkh_signatures': independent_signatures}


def load_record(path):
    data = path.read_bytes()
    if data.startswith(b'\x1f\x8b'):
        data = gzip.decompress(data)
    return json.loads(data)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('receipt', type=Path, help='scenario JSON or gzip-compressed JSON')
    parser.add_argument('--json', action='store_true', help='emit verified per-block measurements')
    args = parser.parse_args()
    try:
        report = verify(load_record(args.receipt))
    except (OSError, KeyError, ValueError, TypeError, IndexError, struct.error, EOFError) as exc:
        sys.exit(f'Scenario verification failed: {exc}')
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"Verified {len(report['timeline'])} linked regtest blocks, entry at height {report['join_height']}, "
              f"and {report['independently_verified_p2pkh_signatures']} P2PKH signatures.")
        print('History transaction fates at entry (remaining outputs):', report['history_output_fates'])
        for candidate in report['candidates']:
            print(f"Height {candidate['height']}: {candidate['admitted']}/{candidate['offered']} offers mined, "
                  f"{candidate['wire_bytes']} bytes, accepted by {', '.join(candidate['reference_nodes'])}.")
