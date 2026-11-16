"""Summarises the sweeps into results/summary.json and the PDF figures.

Every comparison is paired: the same cell (shape, load, order, replicas, seed) runs every
policy on the same applications and the same failure order. Intervals are 95% paired
t-intervals over cells, and the policy means use t-intervals over cells too.

Usage: python experiments/analyze.py
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from scipy import stats  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "results"
FIG = ROOT / "report" / "figures"
FIG.mkdir(parents=True, exist_ok=True)

KEY = ["shape", "load", "order", "replicas", "hosts", "seed"]
POLICY_ORDER = ["FirstFit", "BestFit", "WorstFit", "RoundRobin", "Random", "AntiAffinity", "Uniform", "CEI"]
FAILURES = 8

# light palette from the dataviz reference instance (fixed slot order)
SURFACE, INK, INK2, MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#8a8984", "#e6e5e1"
S1, S2, S3, S4 = "#2a78d6", "#eb6834", "#1baf7a", "#eda100"

METRICS = {
    "e1_services_broken_mean": "Services broken per host failure (mean)",
    "e1_services_broken_max": "Services broken by the worst host failure",
    "e1_apps_hit_mean": "Applications hit per host failure (mean)",
    "e1_dependent_vms_broken_mean": "Healthy VMs that lose a dependency, per failure",
    "e1_vms_lost_mean": "VMs lost per host failure (mean)",
    "e1_crit_lost_mean": "CEI-weighted criticality lost per failure (share)",
    "e1_colocated_replica_services": "Replicated services with 2+ replicas on one host",
    "e1_declared_critical_broken_mean": "Declared-critical services broken per failure",
    "e2_apps_intact_frac_mean": "Share of apps fully working, mean over 8 failures",
    "e2_apps_intact_frac_8": "Share of apps fully working after 8 failures",
    "active_hosts": "Active hosts",
    "power_kw": "Power draw (kW)",
    "unplaced_vms": "VMs that could not be placed",
}


def load(name: str) -> pd.DataFrame:
    df = pd.read_csv(RES / f"sweep_{name}.csv")
    df["lambda"] = df["lambda"].fillna(-1.0)
    df["replicas"] = df["replicas"].astype(str)
    k_cols = [f"e2_apps_intact_{k}" for k in range(1, FAILURES + 1) if f"e2_apps_intact_{k}" in df]
    if k_cols:
        df["e2_apps_intact_frac_mean"] = df[k_cols].mean(axis=1) / df["apps"]
        df[f"e2_apps_intact_frac_{len(k_cols)}"] = df[k_cols[-1]] / df["apps"]
    return df


def ci_mean(x) -> dict:
    x = np.asarray(x, dtype=float)
    n = len(x)
    m = float(x.mean())
    if n < 2:
        return {"mean": m, "lo": m, "hi": m, "n": n}
    half = float(stats.t.ppf(0.975, n - 1) * x.std(ddof=1) / math.sqrt(n))
    return {"mean": m, "lo": m - half, "hi": m + half, "n": n}


def ci_cluster(values, clusters) -> dict:
    """Mean with a 95% t-interval over clusters: values are averaged within each cluster (seed) first,
    because cells that share a seed share applications and are not independent."""
    g = pd.DataFrame({"v": np.asarray(values, dtype=float), "c": np.asarray(clusters)}).groupby("c")["v"].mean()
    out = ci_mean(g.values)
    out["clusters"] = out.pop("n")
    out["cells"] = int(len(values))
    return out


def ci_by_seed(sub: pd.DataFrame, metric: str) -> dict:
    return ci_cluster(sub[metric], sub["seed"])


def paired(df: pd.DataFrame, a: str, b: str, metric: str, a_lambda=None, b_lambda=None) -> dict:
    """Mean of (a - b) with a 95% t-interval clustered by seed, plus the relative change and the
    share of cells where a is lower, equal or higher."""
    pa = df[df.policy == a]
    pb = df[df.policy == b]
    if a_lambda is not None:
        pa = pa[np.isclose(pa["lambda"], a_lambda)]
    if b_lambda is not None:
        pb = pb[np.isclose(pb["lambda"], b_lambda)]
    m = pa[KEY + [metric]].merge(pb[KEY + [metric]], on=KEY, suffixes=("_a", "_b"))
    d = m[f"{metric}_a"] - m[f"{metric}_b"]
    out = ci_cluster(d, m["seed"])
    base = float(m[f"{metric}_b"].mean())
    out["rel"] = out["mean"] / base if base else float("nan")
    out["rel_lo"] = out["lo"] / base if base else float("nan")
    out["rel_hi"] = out["hi"] / base if base else float("nan")
    out["a_lower"] = float((d < -1e-12).mean())
    out["tie"] = float((d.abs() <= 1e-12).mean())
    out["a_higher"] = float((d > 1e-12).mean())
    g = pd.DataFrame({"d": d, "c": m["seed"]}).groupby("c")["d"].mean()
    out["p_t"] = float(stats.ttest_1samp(g, 0).pvalue) if len(g) > 1 and g.std(ddof=1) > 0 else float("nan")
    return out


def policy_table(df: pd.DataFrame, metrics) -> dict:
    return {p: {mt: ci_by_seed(df[df.policy == p], mt) for mt in metrics} for p in POLICY_ORDER if p in set(df.policy)}


def confirm_summary(df: pd.DataFrame, metrics) -> dict:
    """Fresh-seed check of lambda = 0.1 (chosen on the frontier seeds) against the baselines."""
    df = df.copy()
    df["label"] = [p if lam < 0 else f"{p}@{lam:g}" for p, lam in zip(df.policy, df["lambda"])]
    labels = [lab for lab in ["FirstFit", "BestFit", "WorstFit", "AntiAffinity", "Uniform@0.1", "CEI@0.1", "CEI@0.35"]
              if lab in set(df.label)]
    out = {"cells": int(df[KEY].drop_duplicates().shape[0]),
           "seeds": sorted(int(x) for x in df.seed.unique()),
           "means": {lab: {mt: ci_by_seed(df[df.label == lab], mt) for mt in metrics} for lab in labels}}
    df2 = df.assign(policy=df.label)
    for a, b in [("CEI@0.1", "AntiAffinity"), ("CEI@0.1", "BestFit"), ("CEI@0.1", "Uniform@0.1"),
                 ("CEI@0.35", "AntiAffinity"), ("CEI@0.1", "CEI@0.35"), ("AntiAffinity", "BestFit"),
                 ("CEI@0.1", "WorstFit"), ("AntiAffinity", "WorstFit"), ("BestFit", "WorstFit")]:
        if a not in labels or b not in labels:
            continue
        out[f"{a} vs {b}"] = {mt: paired(df2, a, b, mt) for mt in metrics}
    return out


def style(ax):
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(MUTED)
    ax.tick_params(colors=INK2, labelsize=8)
    ax.yaxis.grid(True, color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)


def fig_bars(table: dict, metric: str, path: Path, title: str, order=None, highlight="CEI"):
    pols = [p for p in (order or POLICY_ORDER) if p in table]
    means = [table[p][metric]["mean"] for p in pols]
    err = [[table[p][metric]["mean"] - table[p][metric]["lo"] for p in pols],
           [table[p][metric]["hi"] - table[p][metric]["mean"] for p in pols]]
    fig, ax = plt.subplots(figsize=(6.4, 2.6), dpi=200)
    fig.patch.set_facecolor(SURFACE)
    style(ax)
    colors = [S1 if p == highlight else "#b9b8b2" for p in pols]
    ax.bar(range(len(pols)), means, width=0.62, color=colors, yerr=err, ecolor=INK2, capsize=2.5,
           error_kw={"linewidth": 0.8})
    for i, m in enumerate(means):
        ax.text(i, m + (err[1][i]) + max(means) * 0.02, f"{m:.2f}", ha="center", va="bottom", fontsize=7, color=INK)
    ax.set_xticks(range(len(pols)), pols, fontsize=8, color=INK)
    ax.set_title(title, fontsize=9, color=INK, loc="left")
    ax.set_ylim(0, max(means) * 1.22)
    fig.tight_layout()
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)


def fig_survival(df: pd.DataFrame, path: Path, labels):
    colors = [S1, S2, S3, S4]
    fig, ax = plt.subplots(figsize=(6.4, 2.8), dpi=200)
    fig.patch.set_facecolor(SURFACE)
    style(ax)
    ks = list(range(0, FAILURES + 1))
    finals = []
    for (name, shown), color in zip(labels, colors):
        sub = df[df.label == name]
        ys = [1.0] + [float((sub[f"e2_apps_intact_{k}"] / sub["apps"]).mean()) for k in range(1, FAILURES + 1)]
        ax.plot(ks, ys, color=color, linewidth=2, marker="o", markersize=3.5, label=shown)
        finals.append((ys[-1], shown))
    # direct labels at the right edge, nudged apart so they don't collide
    finals.sort()
    last = -1.0
    for y, shown in finals:
        y_text = max(y, last + 0.035)
        ax.text(FAILURES + 0.15, y_text, shown, color=INK, fontsize=7, va="center")
        last = y_text
    ax.set_xlim(0, FAILURES + 2.2)
    ax.set_xlabel("Hosts failed (random order shared by all policies, empty hosts included)", fontsize=8, color=INK2)
    ax.set_ylabel("Share of apps fully working", fontsize=8, color=INK2)
    ax.legend(fontsize=7, frameon=False, loc="lower left")
    fig.tight_layout()
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)


def fig_frontier(fr: pd.DataFrame, path: Path):
    fig, ax = plt.subplots(figsize=(6.4, 2.8), dpi=200)
    fig.patch.set_facecolor(SURFACE)
    style(ax)
    for name, color, shown in (("CEI", S1, "CEI weights"), ("Uniform", S2, "Uniform weights (count of services)")):
        sub = fr[fr.policy == name].groupby("lambda")["e1_services_broken_mean"].mean().reset_index()
        ax.plot(sub["lambda"], sub.e1_services_broken_mean, color=color, linewidth=2, marker="o", markersize=4,
                label=shown, zorder=3 if name == "CEI" else 2)
    for name in ("BestFit", "AntiAffinity"):
        y = fr[fr.policy == name].e1_services_broken_mean.mean()
        ax.hlines(y, -0.03, 1.03, color=MUTED, linewidth=1, linestyle=(0, (4, 3)), zorder=1)
        ax.text(1.05, y, name, fontsize=7, color=INK, va="center")
    ax.set_xlim(-0.03, 1.25)
    ax.set_xticks([0, 0.2, 0.4, 0.6, 0.8, 1.0])
    ax.set_xlabel("λ, the weight of the power term", fontsize=8, color=INK2)
    ax.set_ylabel("Services broken per host failure", fontsize=8, color=INK2)
    ax.legend(fontsize=7, frameon=False, loc="lower right", bbox_to_anchor=(0.97, 0.12))
    fig.tight_layout()
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)


def main():
    summary = {}
    main_df = load("main")
    metrics = [m for m in METRICS if m in main_df]
    summary["cells_main"] = int(main_df[KEY].drop_duplicates().shape[0])
    summary["policy_main_all"] = policy_table(main_df, metrics)
    mixed = main_df[main_df["shape"] == "mixed"]
    summary["policy_main_mixed"] = policy_table(mixed, metrics)
    comps = {}
    for b in ["BestFit", "FirstFit", "AntiAffinity", "Uniform", "WorstFit"]:
        comps[b] = {mt: paired(main_df, "CEI", b, mt) for mt in metrics}
    summary["cei_vs_main_all"] = comps
    summary["aa_vs_bestfit_main_all"] = {mt: paired(main_df, "AntiAffinity", "BestFit", mt) for mt in metrics}
    by_shape = {}
    for shape, sub in main_df.groupby("shape"):
        by_shape[shape] = {
            b: {mt: paired(sub, "CEI", b, mt) for mt in ["e1_services_broken_mean", "e1_services_broken_max",
                                                        "e1_apps_hit_mean", "active_hosts", "e2_apps_intact_frac_mean"]}
            for b in ["BestFit", "AntiAffinity", "Uniform"]
        }
        by_shape[shape]["means"] = policy_table(sub, ["e1_services_broken_mean", "active_hosts"])
    summary["by_shape"] = by_shape
    by_load = {}
    for (ld, order), sub in main_df.groupby(["load", "order"]):
        by_load[f"{ld}|{order}"] = {
            "means": policy_table(sub, ["e1_services_broken_mean", "unplaced_vms", "active_hosts", "power_kw"]),
            "cei_vs_aa": {mt: paired(sub, "CEI", "AntiAffinity", mt) for mt in ["e1_services_broken_mean", "unplaced_vms", "active_hosts"]},
            "cei_vs_bf": {mt: paired(sub, "CEI", "BestFit", mt) for mt in ["e1_services_broken_mean", "unplaced_vms", "active_hosts"]},
        }
    summary["by_load_order"] = by_load
    fig_bars(summary["policy_main_all"], "e1_services_broken_mean", FIG / "fig_explore_services_broken.png",
             "Exploration, seeds 1-10, CEI at lambda 0.35: services broken per host failure")

    rep = RES / "sweep_replicas.csv"
    if rep.exists():
        rdf = load("replicas")
        summary["replicas"] = {}
        for r, sub in rdf.groupby("replicas"):
            summary["replicas"][r] = {
                "means": policy_table(sub, metrics),
                "cei_vs_aa": {mt: paired(sub, "CEI", "AntiAffinity", mt) for mt in metrics},
                "cei_vs_bf": {mt: paired(sub, "CEI", "BestFit", mt) for mt in metrics},
                "cei_vs_uniform": {mt: paired(sub, "CEI", "Uniform", mt) for mt in metrics},
            }
    fr_path = RES / "sweep_frontier.csv"
    if fr_path.exists():
        fr = load("frontier")
        summary["frontier"] = {
            name: fr[fr.policy == name].groupby("lambda")[["power_kw", "active_hosts", "e1_services_broken_mean",
                                                          "e1_crit_lost_mean", "e2_apps_intact_frac_mean"]]
            .mean().reset_index().to_dict(orient="records")
            for name in ["CEI", "Uniform", "BestFit", "AntiAffinity", "WorstFit"]
        }
        fig_frontier(fr, FIG / "fig3_frontier.png")
    sc_path = RES / "sweep_scale.csv"
    if sc_path.exists():
        sc = pd.read_csv(sc_path)
        summary["scale"] = (sc.groupby(["hosts", "policy"])[["vms", "wall_ms"]].mean().reset_index()
                            .to_dict(orient="records"))
    for name in ("confirm", "confirm_r2", "confirm_r1", "tight"):
        if (RES / f"sweep_{name}_v5.csv").exists():
            cdf = load(f"{name}_v5")
            summary[name] = confirm_summary(cdf, [m for m in METRICS if m in cdf])
            if name == "tight":
                for rep, sub in cdf.groupby("replicas"):
                    summary[f"tight_r{rep}"] = confirm_summary(sub, [m for m in METRICS if m in cdf])
    conf = load("confirm_v5")
    conf["label"] = [p if lam < 0 else f"{p}@{lam:g}" for p, lam in zip(conf.policy, conf["lambda"])]
    labels = ["WorstFit", "FirstFit", "BestFit", "AntiAffinity", "Uniform@0.1", "CEI@0.1", "CEI@0.35"]
    fig_bars(summary["confirm"]["means"], "e1_services_broken_mean", FIG / "fig1_services_broken.png",
             "Services broken per single host failure, mean over powered hosts (fresh seeds 11-20, 360 cells)",
             order=labels, highlight="CEI@0.1")
    fig_survival(conf, FIG / "fig2_survival.png",
                 [("CEI@0.1", "CEI (λ=0.1)"), ("AntiAffinity", "AntiAffinity"), ("BestFit", "BestFit"),
                  ("WorstFit", "WorstFit (default)")])
    (RES / "summary.json").write_text(json.dumps(summary, indent=1, default=float))
    print("wrote", RES / "summary.json")


if __name__ == "__main__" and "--round2" not in __import__("sys").argv:
    main()


# ---------------- Round 2 ----------------
R2_METRICS = {
    "r1_services_broken_mean": "Services broken per rack failure",
    "r1_services_broken_max": "Worst rack failure",
    "r1_declared_critical_broken_mean": "Declared-critical services broken per rack failure",
    "r1_crit_lost_mean": "CEI-weighted criticality lost per rack failure",
    "e1_services_broken_mean": "Services broken per host failure",
    "e1_declared_critical_broken_mean": "Declared-critical services broken per host failure",
    "e2_apps_intact_frac_mean": "Share of apps working, mean over 8 host failures",
    "active_hosts": "Active hosts",
    "power_kw": "Power (kW)",
    "unplaced_vms": "VMs that could not be placed",
}


def labelled(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["label"] = [p if lam < 0 else f"{p}@{lam:g}" for p, lam in zip(df.policy, df["lambda"])]
    return df


def paired_labels(df: pd.DataFrame, a: str, b: str, metric: str, key) -> dict:
    pa = df[df.label == a][key + [metric]]
    pb = df[df.label == b][key + [metric]]
    m = pa.merge(pb, on=key, suffixes=("_a", "_b"))
    d = m[f"{metric}_a"] - m[f"{metric}_b"]
    out = ci_cluster(d, m["seed"])
    base = float(m[f"{metric}_b"].mean())
    for k in ("mean", "lo", "hi"):
        out[f"rel_{k}" if k != "mean" else "rel"] = out[k] / base if base else float("nan")
    out["a_lower"] = float((d < -1e-12).mean())
    out["tie"] = float((d.abs() <= 1e-12).mean())
    out["a_higher"] = float((d > 1e-12).mean())
    return out


def fig_racks(df: pd.DataFrame, path: Path):
    series = [("BestFit", "BestFit", S2), ("WorstFit", "WorstFit (default)", S3),
              ("AntiAffinityRack", "AntiAffinity (rack)", S4), ("CEIrack@0.1", "CEI with racks", S1)]
    fig, ax = plt.subplots(figsize=(6.4, 2.8), dpi=200)
    fig.patch.set_facecolor(SURFACE)
    style(ax)
    df = df[df.replicas == "mixed"]
    racks = sorted(df["racks"].unique())
    for lab, shown, color in series:
        ys = [df[(df.label == lab) & (df.racks == r)]["r1_services_broken_mean"].mean() for r in racks]
        ax.plot(racks, ys, color=color, linewidth=2, marker="o", markersize=4, label=shown,
                zorder=3 if lab.startswith("CEI") else 2)
    ax.set_xscale("log", base=2)
    ax.set_xticks(racks, [str(r) for r in racks])
    ax.set_xlabel("Racks in the 48-host datacenter (hosts per rack = 48 / racks)", fontsize=8, color=INK2)
    ax.set_ylabel("Services broken per rack failure", fontsize=8, color=INK2)
    ax.legend(fontsize=7, frameon=False, loc="upper right")
    fig.tight_layout()
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)


def fig_scale(sc: pd.DataFrame, path: Path):
    series = [("BestFit", S2), ("WorstFit", "#6250d6"), ("AntiAffinity", S3), ("CEI@0.1", S1), ("CEIrack@0.1", "#e87ba4")]
    fig, ax = plt.subplots(figsize=(6.4, 2.6), dpi=200)
    fig.patch.set_facecolor(SURFACE)
    style(ax)
    for lab, color in series:
        sub = sc[sc.label == lab].groupby("hosts")[["vms", "placement_ms"]].median().reset_index().sort_values("vms")
        shown = {"CEI@0.1": "CEI", "CEIrack@0.1": "CEI with racks (20 hosts each)"}.get(lab, lab)
        ax.plot(sub.vms, sub.placement_ms / 1000, color=color, linewidth=2, marker="o", markersize=4, label=shown)
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("VMs placed (100 to 2,000 hosts at 75% load, racks of 20, second run in a warm JVM)", fontsize=8, color=INK2)
    ax.set_ylabel("Host-search time (s)", fontsize=8, color=INK2)
    ax.legend(fontsize=7, frameon=False, loc="upper left")
    fig.tight_layout()
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)


def fig_replfrac(df: pd.DataFrame, path: Path):
    series = [("BestFit", "BestFit", S2), ("WorstFit", "WorstFit (default)", S3),
              ("AntiAffinityRack", "AntiAffinity (rack)", S4), ("CEIrack@0.1", "CEI with racks", S1)]
    fig, ax = plt.subplots(figsize=(6.4, 2.7), dpi=200)
    fig.patch.set_facecolor(SURFACE)
    style(ax)
    shares = sorted(df["share"].unique())
    for lab, shown, color in series:
        ys = [df[(df.label == lab) & (df.share == sh)]["r1_services_broken_mean"].mean() for sh in shares]
        ax.plot(shares, ys, color=color, linewidth=2, marker="o", markersize=4, label=shown,
                zorder=3 if lab.startswith("CEI") else 2)
    ax.set_xticks(shares, [f"{int(sh * 100)}%" for sh in shares])
    ax.set_xlabel("Share of services with two replicas (the rest have one)", fontsize=8, color=INK2)
    ax.set_ylabel("Services broken per rack failure", fontsize=8, color=INK2)
    ax.legend(fontsize=7, frameon=False, loc="upper right")
    fig.tight_layout()
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)


def round2():
    out = {}
    key_r = ["shape", "load", "order", "replicas", "hosts", "rack_size", "seed"]
    if (RES / "sweep_racks_v5.csv").exists():
        df = labelled(load("racks_v5"))
        df["racks"] = (df["hosts"] // df["rack_size"]).astype(int)
        labels = ["BestFit", "WorstFit", "AntiAffinity", "AntiAffinityRack", "CEI@0.1", "Uniformrack@0.1", "CEIrack@0.1"]
        mets = [m for m in R2_METRICS if m in df]
        out["racks_cells"] = int(df[key_r].drop_duplicates().shape[0])
        out["racks_means"] = {lab: {mt: ci_by_seed(df[df.label == lab], mt) for mt in mets} for lab in labels}
        out["racks_by_count"] = {}
        for r, sub in df.groupby("racks"):
            out["racks_by_count"][int(r)] = {
                "means": {lab: {mt: float(sub[sub.label == lab][mt].mean()) for mt in mets} for lab in labels},
                "ceirack_vs_aarack": {mt: paired_labels(sub, "CEIrack@0.1", "AntiAffinityRack", mt, key_r) for mt in mets},
                "ceirack_vs_uniformrack": {mt: paired_labels(sub, "CEIrack@0.1", "Uniformrack@0.1", mt, key_r) for mt in mets},
            }
        out["racks_by_replicas"] = {}
        for rep, sub in df.groupby("replicas"):
            out["racks_by_replicas"][rep] = {
                "means": {lab: {mt: float(sub[sub.label == lab][mt].mean()) for mt in mets} for lab in labels},
                "pairs": {f"{a} vs {b}": {mt: paired_labels(sub, a, b, mt, key_r) for mt in mets}
                          for a, b in [("CEIrack@0.1", "AntiAffinityRack"), ("AntiAffinityRack", "AntiAffinity"),
                                       ("CEIrack@0.1", "BestFit"), ("CEIrack@0.1", "Uniformrack@0.1"),
                                       ("CEI@0.1", "AntiAffinity"), ("CEIrack@0.1", "WorstFit"),
                                       ("AntiAffinityRack", "WorstFit")]},
            }
        out["racks_by_load"] = {}
        for ld, sub in df.groupby("load"):
            out["racks_by_load"][float(ld)] = {
                "means": {lab: {mt: float(sub[sub.label == lab][mt].mean()) for mt in mets} for lab in labels},
                "ceirack_vs_aarack": {mt: paired_labels(sub, "CEIrack@0.1", "AntiAffinityRack", mt, key_r) for mt in mets},
                "ceirack_vs_uniformrack": {mt: paired_labels(sub, "CEIrack@0.1", "Uniformrack@0.1", mt, key_r) for mt in mets},
            }
        out["racks_pairs"] = {
            f"{a} vs {b}": {mt: paired_labels(df, a, b, mt, key_r) for mt in mets}
            for a, b in [("CEIrack@0.1", "AntiAffinityRack"), ("CEIrack@0.1", "Uniformrack@0.1"),
                         ("CEIrack@0.1", "CEI@0.1"), ("CEIrack@0.1", "BestFit"),
                         ("AntiAffinityRack", "AntiAffinity"), ("CEI@0.1", "AntiAffinity")]
        }
        fig_racks(df, FIG / "fig4_racks.png")
    if (RES / "sweep_replfrac_v5.csv").exists():
        rf = labelled(load("replfrac_v5"))
        rf["share"] = rf["replicas"].str[1:].astype(float)
        out["replfrac"] = {}
        for sh, sub in rf.groupby("share"):
            out["replfrac"][f"{sh:g}"] = {
                "means": {lab: {mt: float(sub[sub.label == lab][mt].mean())
                                for mt in ["r1_services_broken_mean", "r1_declared_critical_broken_mean",
                                           "e1_services_broken_mean", "active_hosts"]}
                          for lab in ["BestFit", "WorstFit", "AntiAffinity", "AntiAffinityRack", "Uniformrack@0.1", "CEIrack@0.1"]},
                "pairs": {f"{a} vs {b}": {mt: paired_labels(sub, a, b, mt, key_r)
                                          for mt in ["r1_services_broken_mean", "r1_declared_critical_broken_mean"]}
                          for a, b in [("CEIrack@0.1", "AntiAffinityRack"), ("CEIrack@0.1", "Uniformrack@0.1"),
                                       ("AntiAffinityRack", "BestFit")]},
                "cells": int(sub[key_r].drop_duplicates().shape[0]),
            }
        fig_replfrac(rf, FIG / "fig6_replfrac.png")
    if (RES / "sweep_scale_v5.csv").exists():
        sc = labelled(pd.read_csv(RES / "sweep_scale_v5.csv").assign(**{"lambda": lambda d: d["lambda"].fillna(-1)}))
        sc["run"] = sc.groupby(["hosts", "rack_size", "seed", "label"]).cumcount()
        warm = sc[sc.run == 1]
        rows = []
        for (h, lab), sub in warm.groupby(["hosts", "label"]):
            rows.append({"hosts": int(h), "label": lab, "vms": float(sub.vms.mean()), "seeds": int(len(sub)),
                         "placement_ms_median": float(sub.placement_ms.median()),
                         "placement_ms_min": float(sub.placement_ms.min()), "placement_ms_max": float(sub.placement_ms.max()),
                         "wall_ms_median": float(sub.wall_ms.median()),
                         "wall_ms_min": float(sub.wall_ms.min()), "wall_ms_max": float(sub.wall_ms.max())})
        out["scale_v5"] = rows
        fig_scale(warm, FIG / "fig5_scale.png")
    for tag, fname in (("alibaba", "alibaba_v5"), ("alibaba_s1", "alibaba_s1_v5")):
        if not (RES / f"sweep_{fname}.csv").exists():
            continue
        al = labelled(load(fname))
        key_a = ["load", "order", "rack_size", "seed"]
        labels = ["FirstFit", "BestFit", "WorstFit", "AntiAffinity", "AntiAffinityRack", "Uniform@0.1", "CEI@0.1",
                  "Uniformrack@0.1", "CEIrack@0.1", "CEI@0.35"]
        mets = [m for m in list(R2_METRICS) + ["e1_services_broken_max", "e1_dependent_vms_broken_mean",
                                               "e1_colocated_replica_services", "e2_apps_intact_frac_8"] if m in al]
        out[tag] = {}
        for rs, sub in al.groupby("rack_size"):
            out[tag][int(rs)] = {
                "cells": int(sub[key_a].drop_duplicates().shape[0]),
                "apps_mean": float(sub.apps.mean()), "services_mean": float(sub.services.mean()),
                "vms_mean": float(sub.vms.mean()),
                "means": {lab: {mt: ci_by_seed(sub[sub.label == lab], mt) for mt in mets} for lab in labels},
                "pairs": {f"{a} vs {b}": {mt: paired_labels(sub, a, b, mt, key_a) for mt in mets}
                          for a, b in [("CEI@0.1", "BestFit"), ("CEI@0.1", "AntiAffinity"), ("CEI@0.1", "Uniform@0.1"),
                                       ("CEIrack@0.1", "AntiAffinityRack"), ("CEIrack@0.1", "Uniformrack@0.1"),
                                       ("AntiAffinity", "BestFit"), ("CEI@0.35", "AntiAffinity"),
                                       ("CEIrack@0.1", "BestFit"), ("CEI@0.1", "WorstFit"),
                                       ("CEIrack@0.1", "WorstFit"), ("AntiAffinity", "WorstFit")]},
            }
    (RES / "summary_round2.json").write_text(json.dumps(out, indent=1, default=float))
    print("wrote", RES / "summary_round2.json")


if __name__ == "__main__" and "--round2" in __import__("sys").argv:
    round2()
