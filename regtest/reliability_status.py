"""Check live local solution state against the archived three-node endpoint results."""
import argparse
import json
from pathlib import Path

from prove import request


def inspect(record):
    if not record['complete']:
        raise ValueError('need a complete prespecified session')
    status = json.loads(request('http://127.0.0.1:19680/status'))
    if status['network'] != 'regtest':
        raise ValueError('settlement readback requires the isolated race lab')
    expected = {trial['native']['hash']: trial for trial in record['trials']
                if trial['native']['producer'] == 'witnessed'}
    rows = status['mining']['solutions']
    if {row['hash'] for row in rows} != set(expected) or len(rows) != len(expected):
        raise ValueError('local solution set disagrees with native publications accepted by both references')
    for row in rows:
        trial = expected[row['hash']]
        if (row['chain_state'] != 'settled' or row['height'] != trial['height']
                or row['parent'] != trial['parent'] or row['publication_error']
                or row['job'] != trial['native']['job_id']
                or not all(row[key] is not None for key in (
                    'published_at_ms', 'first_inclusion_at_ms', 'first_settlement_at_ms'))
                or row['queued_at_ms'] > min(row['published_at_ms'], row['first_inclusion_at_ms'])
                or row['first_inclusion_at_ms'] > row['first_settlement_at_ms']
                or not all(peer['trial_hash'] == row['hash'] for peer in trial['endpoint'])):
            raise ValueError(f"local settlement disagrees with endpoint for {row['hash']}")
    counters = {key: status['mining'][key] for key in (
        'solved_blocks', 'reorgs', 'height', 'tip_hash')}
    if counters['solved_blocks'] != len(rows):
        raise ValueError('solution count changed during readback')
    return {'seed': record['seed'], 'mining': counters, 'solutions': rows}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('receipt', type=Path)
    args = parser.parse_args()
    print(json.dumps(inspect(json.loads(args.receipt.read_text())), separators=(',', ':')))
