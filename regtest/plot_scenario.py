"""Plot a verified scenario summary. Only figure generation requires Matplotlib."""
import argparse
from collections import deque
import json
from math import ceil
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator, PercentFormatter


def percent(part, whole):
    return 100 * part / whole if whole else float("nan")


def draw(summary, path):
    join = summary["join_height"]
    rows = [r for r in summary["timeline"] if r["height"] >= join]
    jobs = summary["candidates"]
    job_heights = {j["height"] for j in jobs}
    observed = [r["height"] - join for r in rows]
    teal, blue, grey = "#087a68", "#265598", "#646a74"
    fig, axes = plt.subplots(3, 1, figsize=(9.2, 8.8), layout="constrained")
    fig.suptitle("An empty entrant follows a working regtest chain", fontsize=17, fontweight="bold")
    for ax in axes:
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="y", color="#dde1e6", linewidth=0.7)
        ax.set_axisbelow(True)
        ax.tick_params(labelsize=9)
    ax = axes[0]
    ax.set_title("Share of live outputs in the entrant’s working set", loc="left", fontweight="bold", fontsize=12)
    for numerator, denominator, name, color in [
        ("witnessed_utxos", "utxos", "Whole chain, including immature coinbases", blue),
        ("actor_witnessed_utxos", "actor_utxos", "Simulated payment wallets", teal),
    ]:
        ax.plot(observed, [percent(r[numerator], r[denominator]) for r in rows],
                color=color, linewidth=2.3, label=name)
    ax.legend(loc="best", frameon=False, fontsize=9)
    coverage_max = max(percent(r["witnessed_utxos"], r["utxos"]) for r in rows)
    actor_max = max(percent(r["actor_witnessed_utxos"], r["actor_utxos"]) for r in rows)
    ax.set_ylim(0, max(10, 5 * ceil(max(coverage_max, actor_max) / 5)))
    ax.set_xlabel("Blocks after entry · dotted lines mark the three Rust blocks")
    ax = axes[1]
    ax.set_title("Checks Rust could perform on payments in reference-mined blocks", loc="left", fontweight="bold", fontsize=12)
    window = deque(maxlen=3)
    positions, inputs, transactions = [], [], []
    for r in rows:
        if r["height"] in job_heights or not r["noncoinbase_transactions"]:
            continue
        window.append(r)
        positions.append(r["height"] - join)
        inputs.append(percent(sum(v["known_inputs"] for v in window), sum(v["inputs"] for v in window)))
        transactions.append(percent(sum(v["transactions_determinate"] for v in window),
                                    sum(v["noncoinbase_transactions"] for v in window)))
    ax.plot(positions, inputs, color=blue, linewidth=2, marker=".", label="Known prevouts / inputs")
    ax.plot(positions, transactions, color=teal, linestyle="--", linewidth=2, marker="x",
            label="Determinate payments / payments")
    ax.legend(loc="best", frameon=False, fontsize=9)
    ax.set_xlabel("Blocks after entry · ratios pool the last three payment-bearing reference blocks")
    ax.set_ylim(-3, 105)
    for ax in axes[:2]:
        ax.set_xlim(0, max(observed))
        ax.yaxis.set_major_formatter(PercentFormatter(100, decimals=0))
        ax.xaxis.set_major_locator(MaxNLocator(integer=True, nbins=9))
        for height in job_heights:
            ax.axvline(height - join, color=grey, alpha=0.3, linewidth=0.8, linestyle=":")
    ax = axes[2]
    ax.set_title("Rust block sizes under the same number of submitted payments", loc="left", fontweight="bold", fontsize=12)
    for i, job in enumerate(jobs):
        ax.bar(i, job["wire_bytes"], color=teal, width=0.48)
        ax.annotate(f'{job["wire_bytes"]:,} bytes\n{job["admitted"]} of {job["offered"]} payments',
                    (i, job["wire_bytes"]), xytext=(0, 5), textcoords="offset points", ha="center", fontsize=9)
    ax.set_xticks(range(len(jobs)), [f'Height {j["height"]}' for j in jobs])
    ax.set_ylabel("Serialized block bytes")
    ax.set_ylim(0, max(j["wire_bytes"] for j in jobs) * 1.35)
    fig.savefig(path, dpi=150, facecolor="white")
    plt.close(fig)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summary", type=Path, help="JSON output of verify_scenario.py --json")
    parser.add_argument("output", type=Path, help="output figure, e.g. scenario.png")
    args = parser.parse_args()
    draw(json.loads(args.summary.read_text()), args.output)
