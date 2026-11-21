"""Check that offline verification rejects altered copies of a valid scenario receipt."""
import argparse
from copy import deepcopy
from pathlib import Path

from verify_scenario import load_record, verify


def reject(original, field, update, expected_message):
    changed = deepcopy(original)
    obj, key = field(changed)
    obj[key] = update(obj[key])
    try:
        verify(changed)
    except ValueError as error:
        if expected_message not in str(error):
            raise AssertionError(f'wrong failure for {key}: {error}') from error
        print(f'Rejected altered {key}: {expected_message}')
        return
    raise AssertionError(f'accepted altered {key}')


def check_mutations(original):
    verify(original)
    reject(original, lambda r: (r['blocks'][2], 'height'), lambda n: n + 1, 'out of order')
    reject(original, lambda r: (r['checkpoints'][0]['mining']['validation_window'][-1], 'known_inputs'),
           lambda n: n + 1, 'known_inputs mismatch')
    # Change the final byte of the transaction's locktime without changing its size.
    reject(original, lambda r: (r['blocks'][1], 'wire_hex'),
           lambda h: h[:-2] + f'{int(h[-2:], 16) ^ 1:02x}', 'Merkle root')
    reject(original, lambda r: (r['matched_observers'], 'hash'), lambda _: '0' * 64, 'same final reference block')
    reject(original, lambda r: (r['candidates'][0]['reference_receipts'][0], 'body_sha256'),
           lambda _: '0' * 64, 'reference body or active hash mismatch')
    reject(original, lambda r: (next(o for o in r['candidates'][0]['offered'] if o['decision'] == 'deferred'), 'reason'),
           lambda _: 'wrong outpoint state', 'API deferral reason differs')
    reject(original, lambda r: (r['process_evidence']['cold'], 'body_observed_heights'),
           lambda heights: [heights[0] - 1, *heights], 'cold process log body coverage')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('receipt', type=Path, help='valid scenario JSON or gzip-compressed JSON')
    check_mutations(load_record(parser.parse_args().receipt))
