"""Render confidence intervals from verify_races.py --json. Requires Matplotlib."""
import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter, MaxNLocator


def draw(summary, output):
    batches = [(f"Seed {s['seed']}", s['estimates']) for s in summary['sessions']]
    batches.append(('Pooled', summary['estimates']))
    fig, axes = plt.subplots(2, 1, figsize=(8.3, 5.8), layout='constrained', sharex=True)
    fig.suptitle('Additional stale risk of Scary Node on regtest', fontsize=16, weight='bold')
    for ax, title, conditional in zip(axes, ['All discovered blocks', 'Simultaneous discoveries'], [False, True]):
        ax.set_title(title, loc='left', fontsize=12, weight='bold')
        ax.axvline(0, linestyle='--', linewidth=1.3, color='#666666')
        ax.grid(axis='x', linewidth=.5, color='#cccccc')
        ax.spines[['top', 'right', 'left']].set_visible(False)
        ax.tick_params(axis='y', length=0)
        for i, (label, item) in enumerate(batches):
            if conditional:
                value = item['among_collisions']['witnessed_minus_full']
                interval = item['among_collisions']['difference_exact_95']
            else:
                value = item['witnessed_minus_full']
                interval = item['difference_paired_bootstrap_95']
            center = value * 100
            lo, hi = (n * 100 for n in interval)
            color = '#075c60' if label == 'Pooled' else '#355a86'
            ax.hlines(i, lo, hi, colors=color, linewidth=2)
            ax.plot([lo, hi], [i, i], '|', color=color)
            ax.plot(center, i, 'D' if label == 'Pooled' else 'o', color=color)
        ax.set_yticks(range(len(batches)), labels=[name for name, _ in batches])
        ax.set_ylim(len(batches) - .4, -.6)
        ax.xaxis.set_major_locator(MaxNLocator(nbins=8, integer=True))
        ax.xaxis.set_major_formatter(FuncFormatter(lambda x, _: f'{x:+.0f}' if x else '0'))
    axes[1].set_xlabel('Scary Node stale rate minus full-state stale rate (percentage points)')
    fig.savefig(output, dpi=160, facecolor='white')
    plt.close(fig)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('summary', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    draw(json.loads(args.summary.read_text()), args.output)
