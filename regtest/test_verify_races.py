"""Offline tests for paired denominators, exact limits and saved race evidence."""
import argparse
import unittest
from copy import deepcopy
from pathlib import Path

from verify_races import (REFERENCE_IMAGES, binomial_cdf, check_chain_tip, estimates, exact_interval,
                          ingress_ms, load_record, pooled_identity, summarize, verify)


class RaceInferenceTest(unittest.TestCase):
    def test_known_exact_binomial_limits(self):
        self.assertAlmostEqual(binomial_cdf(2, 5, .5), .5)
        self.assertAlmostEqual(exact_interval(0, 10)[1], 1-.025**(.1))
        self.assertAlmostEqual(exact_interval(10, 10)[0], .025**(.1))
        self.assertAlmostEqual(exact_interval(5, 10)[0], .18708602844739852)
        self.assertAlmostEqual(exact_interval(5, 10)[1], .8129139715526015)
        with self.assertRaises(ValueError):
            exact_interval(1,0)

    def test_singles_do_not_count_as_a_failed_discovery_for_the_other_arm(self):
        rows = [(True,False,'witnessed')]*5 + [(False,True,'full')]*2 + [(True,True,'witnessed')]*2 + [(True,True,'full')]
        result = estimates([rows],500)
        self.assertEqual(result['discovered'], {'witnessed':8, 'full':5})
        self.assertEqual(result['stale'], {'witnessed':1, 'full':2})
        self.assertAlmostEqual(result['witnessed_minus_full'], 1/8-2/5)
        self.assertAlmostEqual(result['among_collisions']['witnessed_minus_full'], -1/3)
        low, high = exact_interval(1,3)
        self.assertEqual(result['among_collisions']['difference_exact_95'], [2*low-1,2*high-1])
        self.assertEqual(result['opportunities'],10)
        self.assertEqual(result['single_discoveries'],7)

    def test_missing_arm_or_missing_ties_does_not_imply_equivalence(self):
        result = estimates([[(True,False,'witnessed')]*5],100)
        self.assertIsNone(result['among_collisions'])
        self.assertIsNone(result['witnessed_minus_full'])
        self.assertIsNone(result['difference_paired_bootstrap_95'])
        self.assertGreater(result['difference_conservative_binomial_95'][1], 0)
        with self.assertRaises(ValueError):
            estimates([[(False,True,'witnessed')]])

    def test_matched_resampling_is_deterministic_and_role_symmetric(self):
        rows = [(True,True,'witnessed')]*4 + [(True,True,'full')]*2 + [(False,True,'full')]
        mirror = [(f,w,'full' if winner=='witnessed' else 'witnessed') for w,f,winner in rows]
        left = estimates([rows],300)
        right = estimates([mirror],300)
        self.assertEqual(left, estimates([rows],300))
        self.assertAlmostEqual(left['witnessed_minus_full'], -right['witnessed_minus_full'])
        self.assertAlmostEqual(left['difference_paired_bootstrap_95'][1], -right['difference_paired_bootstrap_95'][0])

    def test_proxy_timestamp_uses_decimal_millisecond_buckets(self):
        r = {'proxy_time':'1795200000.124','proxy_request_time':'0.003','ingress_ms':1795200000121}
        self.assertEqual(ingress_ms(r), r['ingress_ms'])
        with self.assertRaises(ValueError):
            ingress_ms(dict(r, ingress_ms=r['ingress_ms']+1))

    def test_incomplete_enrollment_has_no_primary_estimate(self):
        self.assertIsNone(summarize([{'seed':1,'complete':False,'rounds':[]}])['estimates'])

    def test_pooled_runs_must_have_identical_harness_and_binary(self):
        images = {s: {'image_reference': tag, 'local_image_id': 'sha256:'+'a'*64} for s,tag in REFERENCE_IMAGES.items()}
        provenance = {'rust_binary_sha256': 'b'*64, 'rust_version': 'scary-node 0.1.0-alpha.2',
                      'images': images, 'judge_version':101020300, 'judge_subversion':'/Bitcoin SV:1.2.3/'}
        session = {'seed': 1701, 'complete': True, 'rounds': [], 'harness_sha256': {'races.py': 'a'*64},
                   'provenance': provenance}
        for field, value, error in (('harness_sha256', {'races.py': 'b'*64}, 'harness'),
                                    ('provenance', dict(provenance,rust_binary_sha256='c'*64), 'binary')):
            with self.assertRaisesRegex(ValueError, error):
                summarize([session, dict(session, seed=1702, **{field: value})])
        rebuild = deepcopy(provenance)
        rebuild['images']['scary-node']['local_image_id'] = 'sha256:'+'d'*64
        self.assertEqual(pooled_identity(provenance), pooled_identity(rebuild))
        rebuild['images']['judge']['local_image_id'] = 'sha256:'+'e'*64
        self.assertNotEqual(pooled_identity(provenance), pooled_identity(rebuild))

    def test_inconclusive_submit_needs_a_positive_valid_fork_receipt(self):
        arm = {'hash':'42'*32, 'relay':{'result':'inconclusive'}}
        tip = {'hash':arm['hash'], 'height':120, 'status':'valid-fork', 'branchlen':1}
        obs = {'chain_tip':tip, 'submitblock_result':'inconclusive'}
        check_chain_tip(obs, arm, 120, False)
        for status in ('headers-only', 'valid-headers', 'unknown', 'active'):
            with self.assertRaises(ValueError):
                check_chain_tip(dict(obs, chain_tip=dict(tip, status=status)), arm, 120, False)
        with self.assertRaises(ValueError):
            check_chain_tip(dict(obs, chain_tip=dict(tip, status='active',branchlen=0)), arm, 120, True)

    def test_valid_headers_alone_does_not_corroborate_inconclusive(self):
        arm = {'hash':'42'*32, 'relay':{'result':'inconclusive', 'started_ns':16},
               'proposal':{'ok':True,'result':None,'finished_ns':8}}
        body = b'previously replayed body'
        obs = {'chain_tip':{'hash':arm['hash'],'height':120,'status':'valid-headers','branchlen':1},
               'submitblock_result':'inconclusive','body_hex':body.hex()}
        with self.assertRaises(ValueError):
            check_chain_tip(obs, arm, 120, False)
        for proposal in ({'ok':False,'result':None,'finished_ns':8}, {'ok':True,'result':'inconclusive','finished_ns':8},
                         {'ok':True,'result':None,'finished_ns':17}):
            with self.assertRaises(ValueError):
                check_chain_tip(obs, dict(arm, proposal=proposal), 120, False, replayed_body=body)
        check_chain_tip(obs, arm, 120, False, replayed_body=body)


def check_mutations(original):
    """Change local copies only. Each alteration must fail its intended integrity check."""
    verify(original)
    tie = next(i for i,t in enumerate(original['trials']) if all(a['solved'] for a in t['work']['arms']))
    fields = [
        (lambda r:(r['schedules'][0],'swap_dispatch'), lambda x:not x, 'draw or assignment'),
        (lambda r:(r['trials'][0],'payment_txid'), lambda _: '00'*32, 'wrong common payment'),
        (lambda r:(r['trials'][tie]['work']['arms'][0],'last_nonce'), lambda n:n+2, 'nonce range'),
        (lambda r:(r['trials'][tie]['work']['arms'][1],'solved'), lambda _:False, 'first-hit label'),
        (lambda r:(r['trials'][tie]['work']['arms'][0]['relay'],'ingress_ms'), lambda n:n+1, 'proxy ingress bucket'),
        (lambda r:(r['trials'][tie]['judge']['observations'][0],'body_sha256'), lambda _:'00'*32, 'different sibling body'),
        (lambda r:(r['trials'][0]['endpoint'][0],'trial_hash'), lambda _:'00'*32, 'active endpoint'),
        (lambda r:(r['provenance']['images']['judge'],'image_reference'), lambda _:'wrong-image', 'unexpected pinned image'),
    ]
    if original.get('harness_sha256'):
        fields.append((lambda r:(r['harness_sha256'],'race_work.py'), lambda _:'00'*32, 'harness hash differs'))
    for field, change, expected in fields:
        altered = deepcopy(original)
        owner, key = field(altered)
        owner[key] = change(owner[key])
        try:
            verify(altered)
        except ValueError as error:
            if expected not in str(error):
                raise AssertionError(f'{key}: wrong failure {error}') from error
        else:
            raise AssertionError(f'verifier accepted false {key}')
    skipped = deepcopy(original)
    work = skipped['trials'][tie]['work']
    work['attempts_per_arm'] += 1
    for arm in work['arms']:
        arm['last_nonce'] += 2
    try:
        verify(skipped)
    except ValueError as error:
        if 'earlier successful nonce omitted' not in str(error):
            raise AssertionError(f'wrong skipped-discovery failure: {error}') from error
    else:
        raise AssertionError('accepted a later tick after a real on-target discovery')
    print(f'Correctly rejected {len(fields)+1} local evidence alterations.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('receipt', nargs='?', type=Path, help='also test altered copies of a complete receipt with a tie')
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(RaceInferenceTest)
    if not unittest.TextTestRunner().run(suite).wasSuccessful():
        raise SystemExit(1)
    if args.receipt:
        check_mutations(load_record(args.receipt))
