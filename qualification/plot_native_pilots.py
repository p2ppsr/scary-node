"""Render the closed sizing pilots and the separate paired notification comparison."""

import json
from pathlib import Path
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import MultipleLocator

EVIDENCE = Path(__file__).resolve().parents[1] / "docs/evidence"
TIMER = "#925427"
IMMEDIATE = "#006e78"
TEXT = "#182a35"
MUTED = "#546674"


def load(name):
    return json.loads((EVIDENCE / name).read_text())


def render():
    pilots = [
        load("chronicle-native-sizing.json"),
        load("chronicle-immediate-sizing.json"),
    ]
    intervention = load("chronicle-relay-comparison.json")
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 10,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.spines.left": False,
            "text.color": TEXT,
            "axes.labelcolor": TEXT,
            "xtick.color": MUTED,
            "ytick.color": TEXT,
        }
    )
    fig, axes = plt.subplots(
        1, 2, figsize=(12, 4.35), gridspec_kw={"width_ratios": [1.15, 1]}
    )
    fig.subplots_adjust(left=0.16, right=0.96, top=0.72, bottom=0.20, wspace=0.54)
    fig.text(
        0.035,
        0.94,
        "Stale discoveries and native publication timing",
        fontsize=18,
        weight="bold",
    )
    fig.text(
        0.035,
        0.865,
        "Two independent sizing pilots and a separate four-seed paired comparison on regtest",
        fontsize=10.5,
        color=MUTED,
    )
    ax = axes[0]
    ax.set_title(
        "Stale-rate difference", fontsize=12, weight="bold", loc="left", pad=10
    )
    rows = [
        (0, "witnessed/full", 3.4),
        (1, "witnessed/full", 2.45),
        (0, "full/full", 1.1),
        (1, "full/full", 0.15),
    ]
    ax.axvline(0, color="#7b8790", lw=1)
    labels = []
    for index, arm, y in rows:
        a = pilots[index]["arms"][arm]
        mid = a["stale_difference"] * 100
        low, high = (n * 100 for n in a["confidence_interval_95"])
        color = (TIMER, IMMEDIATE)[index]
        ax.errorbar(
            mid,
            y,
            xerr=[[mid - low], [high - mid]],
            marker="o",
            capsize=4,
            color=color,
            lw=2.3,
            ms=7,
        )
        ax.text(
            mid,
            y + 0.24,
            f"{mid:+.1f}",
            ha="center",
            weight="bold",
            color=color,
            fontsize=9,
        )
        label = (
            ("Witnessed / full" if arm == "witnessed/full" else "Full / full")
            + " · "
            + ("timer", "immediate")[index]
        )
        labels.append(label)
    ax.set_yticks([r[2] for r in rows], labels, fontsize=8.8)
    ax.set_ylim(-0.45, 4.05)
    ax.set_xlim(-63, 63)
    ax.xaxis.set_major_locator(MultipleLocator(20))
    ax.set_xlabel("Role A minus role B (percentage points)", labelpad=6)
    ax.tick_params(axis="y", length=0, pad=7)
    ax.grid(axis="x", color="#e3e8eb", lw=0.7)
    ax.set_axisbelow(True)
    ax = axes[1]
    ax.set_title(
        "Solo submission to first active readback",
        fontsize=12,
        weight="bold",
        loc="left",
        pad=10,
    )
    for i, label in enumerate(("Witnessed", "Full")):
        y = 1 - i
        vals = [
            intervention[t]["arms"]["witnessed/full"][i][
                "submission_to_first_active_median_ms"
            ]["solo"]
            for t in ("timer", "immediate")
        ]
        ax.plot(vals, [y, y], color="#8f9ca4", lw=2, zorder=1)
        for k, (v, c) in enumerate(zip(vals, (TIMER, IMMEDIATE))):
            ax.scatter(v, y, s=53, color=c, zorder=2)
            ax.annotate(
                f"{v:.0f} ms",
                (v, y),
                xytext=(0, 10 if k == 0 else -16),
                textcoords="offset points",
                ha="center",
                color=c,
                fontsize=9,
                weight="bold",
            )
    ax.set_yticks([1, 0], ["Witnessed", "Full"], fontsize=9)
    ax.set_ylim(-0.55, 1.55)
    ax.set_xlim(0, 600)
    ax.set_xlabel("Median elapsed time (ms)", labelpad=6)
    ax.tick_params(axis="y", length=0, pad=7)
    ax.grid(axis="x", color="#e3e8eb", lw=0.7)
    ax.set_axisbelow(True)
    for t, c, x in (
        ("Timer", TIMER, 0.605),
        ("Immediate notification", IMMEDIATE, 0.700),
    ):
        fig.text(x, 0.865, "● " + t, color=c, fontsize=9, weight="bold")
    fig.text(
        0.035,
        0.035,
        "Paired 95% intervals use 16 sessions per pilot. Timing uses the same four seeds with both releases. Full/full compares two full-state miners.",
        fontsize=8.6,
        color=MUTED,
    )
    fig.savefig(EVIDENCE / "chronicle-native-pilots.png", dpi=160, facecolor="white")
    plt.close(fig)


if __name__ == "__main__":
    render()
