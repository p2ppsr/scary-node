"""Plot recorded mining opportunities and independently reconstructed input ages."""

import gzip
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

EVIDENCE = Path(__file__).resolve().parents[1] / "docs/evidence"
TEXT = "#18313c"
MUTED = "#516775"
TEAL = "#006d6b"


def render():
    with gzip.open(
        EVIDENCE / "chronicle-sustained-endorsed-interruption.json.gz", "rt"
    ) as source:
        record = json.load(source)
    membership = json.loads(
        (EVIDENCE / "chronicle-sustained-endorsed-membership.json").read_bytes()
    )
    active = {b["hash"] for b in record["blocks"]}
    distances = membership["source_parent_ages"]["distances_by_miner"]
    miners = ("scary", "sv")
    labels = ("Scary Node", "Stock SV Node")
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 10,
            "text.color": TEXT,
            "axes.labelcolor": MUTED,
            "xtick.color": MUTED,
            "ytick.color": TEXT,
        }
    )
    fig, (slots, ages) = plt.subplots(2, 1, figsize=(11, 5.7))
    fig.subplots_adjust(left=0.16, right=0.97, bottom=0.17, top=0.76, hspace=1.05)
    fig.text(
        0.025,
        0.94,
        "Mining after an endorsed funding block",
        weight="bold",
        fontsize=18,
    )
    fig.text(
        0.025,
        0.88,
        f"Four-node regtest  ·  {sum(b['scheduled_payments'] for b in record['blocks']):,} confirmed payments  ·  {len(record['slots'])} of 300 opportunities recorded",
        color=MUTED,
    )
    for axis in (slots, ages):
        axis.spines[["top", "right", "left"]].set_visible(False)
        axis.spines["bottom"].set_color("#b6c2c6")
        axis.tick_params(axis="y", length=0, pad=9)
        axis.set_yticks([1, 0], labels)
        axis.set_ylim(-0.55, 1.55)
    slots.set_title(
        "Selected blocks and work availability by scheduled miner",
        loc="left",
        fontsize=11,
        weight="bold",
        pad=10,
    )
    slots.set_xlim(0, 30)
    slots.set_xticks(range(0, 31, 5))
    slots.set_xlabel("Minutes from first scheduled offer")
    for x in range(5, 30, 5):
        slots.axvline(x, color="#dce2e4", linewidth=0.8, zorder=0)
    cutoff = record["producer"][-1]["finished_ns"] / 60e9
    slots.axvspan(cutoff, 30, color="#e9edef", zorder=0)
    slots.text(
        (cutoff + 30) / 2,
        0.5,
        f"{300 - len(record['slots'])} slots\nunobserved",
        ha="center",
        va="center",
        color=MUTED,
    )
    styles = {
        "Selected": ("o", TEAL, 21),
        "Off active chain": ("o", "#83639a", 27),
        "Unready": ("|", "#74838a", 34),
        "Connection closed": ("x", "#af4d32", 34),
    }
    groups = {label: [] for label in styles}
    for row in record["slots"]:
        if row["outcome"] == "solved":
            outcome = "Selected" if row["hash"] in active else "Off active chain"
        else:
            outcome = {"unready": "Unready", "error": "Connection closed"}[
                row["outcome"]
            ]
        groups[outcome].append(
            (row["scheduled_ns"] / 60e9, int(row["miner"] == "scary"))
        )
    handles = []
    for label, (marker, color, size) in styles.items():
        x, y = zip(*groups[label], strict=True)
        fill = "none" if label == "Off active chain" else color
        args = (
            {"facecolors": fill, "edgecolors": color}
            if marker == "o"
            else {"color": color}
        )
        slots.scatter(x, y, s=size, marker=marker, linewidths=1.1, **args)
        handles.append(
            Line2D(
                [],
                [],
                ls="none",
                marker=marker,
                markersize=6,
                color=color,
                markerfacecolor=fill,
                label=label,
            )
        )
    slots.legend(
        handles=handles,
        ncols=4,
        loc="upper center",
        bbox_to_anchor=(0.5, -0.37),
        frameon=False,
        fontsize=9,
    )

    ages.set_title(
        "Age of the output spent by each payment on the selected chain",
        loc="left",
        fontsize=11,
        weight="bold",
        pad=9,
    )
    colors = ["#b87840", "#537b99", TEAL]
    bins = ["Same block", "Previous block", "Two or more blocks earlier"]
    for row, miner in zip((1, 0), miners, strict=True):
        counts = [0, 0, 0]
        for age, count in distances[miner].items():
            counts[min(int(age), 2)] += count
        total = sum(counts)
        start = 0
        for count, color in zip(counts, colors, strict=True):
            share = 100 * count / total
            ages.barh(row, share, left=start, height=0.5, color=color)
            if share >= 9:
                ages.text(
                    start + share / 2,
                    row,
                    f"{share:.1f}%",
                    ha="center",
                    va="center",
                    color="white",
                    fontsize=9,
                    weight="bold",
                )
            elif share > 0:
                ages.text(
                    start + share,
                    row + 0.28,
                    f"{share:.1f}%",
                    ha="right",
                    va="bottom",
                    color=TEXT,
                    fontsize=9,
                )
            start += share
    ages.set_yticks(
        [1, 0],
        [
            f"{label}\n{sum(distances[m].values()):,} payments"
            for label, m in zip(labels, miners, strict=True)
        ],
    )
    ages.set_xlim(0, 100)
    ages.set_xticks([0, 25, 50, 75, 100], ["0%", "25%", "50%", "75%", "100%"])
    ages.legend(
        handles=[Patch(color=c, label=b) for b, c in zip(bins, colors, strict=True)],
        ncols=3,
        loc="upper center",
        bbox_to_anchor=(0.5, -0.24),
        frameon=False,
        fontsize=9,
    )
    fig.text(
        0.025,
        0.025,
        "Each payment had one input. The denominator is each miner’s confirmed payments in the 66 audited blocks.",
        color=MUTED,
        fontsize=9,
    )
    fig.savefig(EVIDENCE / "chronicle-sustained-endorsed.png", dpi=150)
    plt.close(fig)


if __name__ == "__main__":
    render()
