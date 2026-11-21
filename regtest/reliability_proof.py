"""Prove an older real Stratum template survives new same-parent intake and settles."""
import argparse
import hashlib
import os
import subprocess
import json
import sys
import tempfile
import time
from pathlib import Path

from prove import API, NODES, DisposableWallet, assert_tip, block, compose, ready, request, rpc, solve, status, txid, wait
from scenario import Coin, pay, require


def admit(raw):
    return json.loads(request(API + '/transactions', raw,
                              {'Content-Type': 'application/octet-stream'}))['txid']


def confirm(miner, other, label):
    h = rpc(miner, 'generate', 1)[0]
    wait(label, lambda: all(assert_tip(n, h) for n in (miner, other)))
    return h


def run(reorg=False):
    a, b = NODES
    for node in NODES:
        genesis = rpc(node, 'getblockchaininfo')
        require(genesis['chain'] == 'regtest' and genesis['blocks'] == 0, 'start on fresh regtest')
    with tempfile.TemporaryDirectory() as directory:
        wallet = DisposableWallet(Path(directory) / 'key.pem')
        compose('stop', 'scary-node')
        paid = rpc(a, 'generatetoaddress', 1, wallet.address)[0]
        wait('coinbase propagated', lambda: assert_tip(b, paid))
        _, txs, outputs = block(a, paid)
        coin = Coin(txid(txs[0]), 0, outputs[0][0][0], 0)
        for _ in range(100):
            tip = confirm(a, b, 'shared maturity block')
        compose('start', 'scary-node')
        wait('entrant observes fresh tip', lambda: status()['mining']['tip_hash'] == tip)
        value = coin.value // 2
        funding = pay([wallet], [coin], [(0, value), (0, coin.value - value - 1000)])
        require(rpc(a, 'sendrawtransaction', funding.raw.hex()) == funding.id, 'funding intake')
        wait('funding is in the reference job', lambda: rpc(a, 'getminingcandidate')['num_tx'] == 1)
        funding_hash = confirm(a, b, 'funding accepted')
        funding_wire = block(a, funding_hash)[0]
        wait('funding observed', lambda: status()['mining']['tip_hash'] == funding_hash)
        parent = confirm(b, a, 'common parent')
        parent_wire = block(b, parent)[0]
        wait('parent observed', lambda: status()['mining']['tip_hash'] == parent)
        first, second = [pay([wallet], [source], [(0, source.value-1000)]) for source in funding.outputs]
        require(admit(first.raw) == first.id, 'first payment intake mismatch')
        job_id, header, coinbase, old, new = solve(
            'late-share', parent, first.id, refresh_payment=lambda: admit(second.raw))
        require(funding.raw in funding_wire, 'known funding transaction missing')
        h = txid(header)
        wire = header + b'\x02' + coinbase + first.raw
        references = []
        for name, node in zip(('tn1', 'tn2'), NODES):
            wait('both full validators adopt old job', lambda: assert_tip(node, h))
            observed = block(node, h)
            require(observed[0] == wire and observed[1][1:] == [first.raw], 'old job was changed')
            references.append({'node': name, 'hash': h, 'sha256': hashlib.sha256(observed[0]).hexdigest()})
        wait('Rust records first inclusion', lambda: status()['mining']['solutions'][-1]['chain_state'] == 'observed_active')
        require(status()['mining']['pending_transactions'] == 1, 'independent offer was not retained')
        successors = []
        for miner, other in ((a, b), (b, a)):
            expected = h if not successors else successors[-1]['hash']
            wait('reference assembles on current winner', lambda: rpc(miner, 'getminingcandidate')['prevhash'] == expected)
            tip = confirm(miner, other, 'successor accepted')
            successors.append({'hash': tip, 'wire_hex': block(miner, tip)[0].hex()})
            wait('Rust observes successor', lambda: status()['mining']['tip_hash'] == tip)
        local = status()['mining']
        solution = local['solutions'][-1]
        require(solution['hash'] == h and solution['job'] == job_id and solution['chain_state'] == 'settled'
                and solution['published_at_ms'] is not None and solution['first_settlement_at_ms'] is not None,
                'missing local settlement lifecycle')
        result = {'network':'regtest','funding_wire':funding_wire.hex(),'funding_id':funding.id,
                'parent_wire':parent_wire.hex(),'candidate_wire':wire.hex(),'old_job':old,'new_job':new,
                'first_payment':first.raw.hex(),'second_payment':second.raw.hex(),'references':references,
                'successors':successors,'solution':solution}
        if reorg:
            result['reorg'] = verify_reorg(tip, funding.outputs[1], second)
        return result


def switch_peer_configuration(separated):
    """Restart only the two test references, preserving their isolated stores."""
    require(os.environ.get('COMPOSE_PROJECT_NAME') == 'scary-node-reliability',
            'branch test requires its dedicated project')
    if separated:
        directory = Path(__file__).resolve().parent
        subprocess.run(['docker', 'compose', '-f', str(directory/'compose.yaml'),
                        '-f', str(directory/'reliability.compose.yaml'),
                        '-f', str(directory/'reliability-separated.compose.yaml'),
                        'up', '-d', '--no-deps', 'tn1', 'tn2'], stdout=sys.stderr, check=True)
    else:
        compose('up', '-d', '--no-deps', 'tn2')
    # Recreated services may have new bridge addresses. Clear cached DNS in the test ingress.
    compose('restart','ingress')
    for node in NODES:
        require(wait('reference ready after restart',lambda: rpc(node,'getblockchaininfo'))['chain']=='regtest',
                'references must stay on regtest')


def verify_reorg(parent, funding, payment):
    left, right = NODES
    by_txid = f'{API}/outpoints/'
    def lookup(ident, number=0):
        return json.loads(request(f'{by_txid}{ident}/{number}'))
    switch_peer_configuration(True)
    # Scary redials every 25 seconds. Allow one full redial interval plus
    # mesh heartbeats before the live Stratum worker submits.
    time.sleep(30)
    print('Isolated references retained the shared prefix.', file=sys.stderr)
    wait('separate full nodes retain existing common parent',lambda: all(assert_tip(n,parent) for n in NODES))
    wait('left assembler ready',lambda: rpc(left,'getminingcandidate')['prevhash']==parent)

    # Work produced in the first mining cell consumes a witnessed output.
    job, header, coinbase = solve('first-branch', parent, payment.id)
    detached = txid(header)
    wait('left reference validates the branch candidate',lambda: assert_tip(left,detached))
    wait('entrant tracks active first branch',lambda: status()['mining']['tip_hash']==detached)
    before = status()['mining']
    print(f'Observed locally mined branch {detached}.', file=sys.stderr)
    old_source = lookup(funding.txid,funding.index)
    old_output = lookup(payment.id)
    require(old_source['consumed'] and old_output['known'],'missing first branch effects')

    # The second full node independently mines a different valid sibling.
    require(assert_tip(right,parent),'right ref must still be on the shared parent')
    wait('right assembler ready',lambda: rpc(right,'getminingcandidate')['prevhash']==parent)
    sibling = rpc(right,'generate',1)[0]
    require(sibling != detached,'expected independent valid branch')
    sibling_wire = block(right,sibling)[0]
    require(sibling_wire[4:36][::-1].hex()==parent,'wrong competing parent')
    switch_peer_configuration(False)
    # Do not choose a winner by rewriting validator state. Extend whichever
    # ordinary tip the right node retained when its peer came back.
    require(assert_tip(right,sibling),'this engineering trial did not retain the competing tip')
    wait('entrant observes the equal-work sibling',lambda:
         status()['mining']['buffered_bodies']>before['buffered_bodies'],seconds=30)
    wait('right assembly on competing sibling',lambda: rpc(right,'getminingcandidate')['prevhash']==sibling)
    next_tip = rpc(right,'generate',1)[0]
    wait('references choose greater-work chain',lambda: all(assert_tip(n,next_tip) for n in NODES))
    wait('entrant reorganizes onto the reference winner',lambda: status()['mining']['tip_hash']==next_tip,seconds=45)
    after_source = lookup(funding.txid,funding.index)
    after_output = lookup(payment.id)
    after = status()['mining']
    require(after['reorgs']==before['reorgs']+1 and after['headers']==before['headers']+1
            and after_source['eligible'] and not after_source['consumed'] and not after_output['known'],
            'witnessed state did not roll back to the common parent')
    wait('right assembler on heavier branch',lambda: rpc(right,'getminingcandidate')['prevhash']==next_tip)
    last = confirm(right,left,'both references extend heavier branch')
    wait('local stale-at-depth classification',lambda: status()['mining']['solutions'][-1]['chain_state']=='stale_at_depth')
    return {'common_parent':parent,'job':job,'detached':detached,
            'detached_wire':(header+b'\x02'+coinbase+payment.raw).hex(),
            'replacement':[sibling,next_tip,last],
            'replacement_bodies':[block(right,h)[0].hex() for h in (sibling,next_tip,last)],
            'reference_tips':[rpc(n,'getbestblockhash') for n in NODES],
            'source_before':old_source,'source_after':after_source,'output_before':old_output,'output_after':after_output,
            'retained_headers_before':before['headers'],'retained_headers_after':after['headers'],
            'reorg_count':after['reorgs'],'solution':status()['mining']['solutions'][-1]}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reorg',action='store_true',help='run the isolated short-reorg fixture as well')
    args = parser.parse_args()
    if args.reorg:
        require(NODES == [(19741,19751),(19742,19752)],'dedicated, loopback reliability lab required')
        wait('entrant connects to left reference',lambda: status()['peer']['connected_peer_ids_seen']>=1)
        for node in NODES:
            wait('both RPC endpoints ready',lambda: rpc(node,'getblockchaininfo'))
    else:
        ready()
    print(json.dumps(run(args.reorg), indent=2))
