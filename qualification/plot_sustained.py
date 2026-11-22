"""Plot native offers, independent confirmations and actual stock node gauges in elapsed time."""

import argparse
import gzip
import itertools
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator


def plot(receipt, stock_rows, output):
    with gzip.open(receipt, "rt") as f:
        r = json.load(f)
    if stock_rows:
        with gzip.open(stock_rows, "rt") as f:
            gauges = [json.loads(line) for line in f]
    else:
        gauges = r["stock"]
    minute = 60e9
    offers = [
        v for v in r["producer"] if v["phase"] == "offer" and v["outcome"] == "sent"
    ]
    blocks = r["blocks"]
    end = max(r["duration_ns"], max((b["readback_ns"] for b in blocks), default=0))
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 10,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.spines.left": False,
        }
    )
    fig, ax = plt.subplots(figsize=(9.5, 4.6))
    fig.subplots_adjust(left=0.09, right=0.97, top=0.72, bottom=0.17)
    fig.text(
        0.045,
        0.925,
        "Native payment offers and confirmed block reads",
        fontsize=16,
        weight="bold",
        color="#182a35",
    )
    profile = (
        "Requested wallet transactions"
        if r.get("source_protocol") == "inv-getdata-tx"
        else "Direct wallet transactions"
    )
    fig.text(
        0.045,
        0.855,
        profile + "  ·  four-node regtest  ·  millions of transactions",
        fontsize=10.5,
        color="#526674",
    )
    source = list(itertools.accumulate(o["count"] / 1e6 for o in offers))
    mined = list(itertools.accumulate(b["scheduled_payments"] / 1e6 for b in blocks))
    ax.axvspan(30, end / minute, color="#f1f4f5", zorder=0)
    ax.axvline(30, color="#899aa4", ls="--", lw=1.2)
    if source:
        ax.step(
            [0] + [o["finished_ns"] / minute for o in offers] + [end / minute],
            [0] + source + [source[-1]],
            where="post",
            color="#385677",
            lw=2,
            label="Wallet offers written",
        )
    if mined:
        ax.step(
            [0] + [b["readback_ns"] / minute for b in blocks] + [end / minute],
            [0] + mined + [mined[-1]],
            where="post",
            color="#007e72",
            lw=2.6,
            label="Payments in verified blocks",
        )
    for call, key, name, c in (
        ("getblockchainactivity", "transactions", "SV validation queue", "#b76925"),
        ("getmempoolinfo", "size", "SV mempool", "#885c87"),
    ):
        values = [
            (x["elapsed_ns"] / minute, x[call][key] / 1e6)
            for x in gauges
            if key in x.get(call, {}) and x["elapsed_ns"] <= end
        ]
        if values:
            ax.plot(
                *zip(*values), label=name, color=c, marker=".", markersize=3, lw=1.65
            )
    ax.set_xlim(0, max(35, end / minute))
    ax.set_ylim(bottom=0)
    ax.yaxis.set_major_locator(MaxNLocator(nbins=7, integer=True))
    ax.set_xlabel("Minutes since first scheduled offer", labelpad=8)
    ax.set_axisbelow(True)
    ax.grid(axis="y", color="#dce3e6", lw=0.7)
    ax.tick_params(axis="y", length=0)
    ax.legend(
        frameon=False,
        ncol=4,
        bbox_to_anchor=(0, 1.05),
        loc="lower left",
        borderaxespad=0,
        fontsize=8.5,
        handlelength=2,
        columnspacing=1.7,
    )
    ax.text(
        30.3,
        0.78,
        "Drain",
        transform=ax.get_xaxis_transform(),
        fontsize=9,
        color="#516470",
    )
    fig.text(
        0.045,
        0.025,
        "Queue and mempool points reflect actual stock RPC samples. Confirmations count each scheduled payment once after three matching body reads.",
        fontsize=8.3,
        color="#546674",
    )
    fig.savefig(output, dpi=160, facecolor="white")
    plt.close(fig)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("receipt", type=Path)
    p.add_argument("output", type=Path)
    p.add_argument("--stock-gauges", type=Path)
    a = p.parse_args()
    plot(a.receipt, a.stock_gauges, a.output)
