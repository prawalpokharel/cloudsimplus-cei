"""Checks after the review fixes (jar v5).

1. Determinism: the v5 refactor (count weights by default, CEI opt-in, domain map built first,
   direct timing) must not change any policy whose weights didn't change. Rows of the earlier
   sweeps are compared with the v5 reruns on every shared metric column.
2. Uniformrack: v3/v4 left the rack policy named Uniformrack on CEI weights (only "Uniform" was
   switched to count weights), so those rows must now differ from the earlier runs.
3. CEI versus count weights at lambda 0.1, on every metric, across all v5 sweeps.

Usage: python experiments/check_v5.py
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd

RES = Path(__file__).resolve().parents[1] / "results"
KEY = ["shape", "load", "order", "replicas", "hosts", "rack_size", "seed"]
SKIP = {"wall_ms", "placement_ms", "fi_host_faults"}


def load(name):
    df = pd.read_csv(RES / f"sweep_{name}.csv")
    df["lambda"] = df["lambda"].fillna(-1)
    df["label"] = [p if lam < 0 else f"{p}@{lam:g}" for p, lam in zip(df.policy, df["lambda"])]
    if "rack_size" not in df:
        df["rack_size"] = 1
    for c in KEY:
        if c not in df:
            df[c] = "-"
    df["replicas"] = df["replicas"].astype(str)
    return df


def compare(old, new, label):
    a = old[old.label == label].set_index(KEY)
    b = new[new.label == label].set_index(KEY)
    cols = [c for c in a.columns if c in b.columns and c not in SKIP and c not in ("policy", "lambda", "label")
            and pd.api.types.is_numeric_dtype(a[c])]
    a, b = a[cols].align(b[cols], join="inner")
    same = ((a - b).abs() <= 1e-9) | (a.isna() & b.isna())
    return {"cells": int(len(a)), "identical_cells": int(same.all(axis=1).sum()), "columns": len(cols)}


out = {"determinism": {}, "uniformrack_changed": {}, "cei_vs_count": {}}
for old, new, labels in [
    ("confirm", "confirm_v5", ["FirstFit", "BestFit", "AntiAffinity", "Uniform@0.1", "CEI@0.1", "CEI@0.35"]),
    ("confirm_r1", "confirm_r1_v5", ["BestFit", "AntiAffinity", "CEI@0.1"]),
    ("confirm_r2", "confirm_r2_v5", ["BestFit", "AntiAffinity", "CEI@0.1"]),
    ("tight", "tight_v5", ["BestFit", "AntiAffinity", "Uniform@0.1", "CEI@0.1", "CEI@0.35"]),
    ("racks", "racks_v5", ["BestFit", "AntiAffinity", "AntiAffinityRack", "CEI@0.1", "CEIrack@0.1"]),
    ("replfrac", "replfrac_v5", ["BestFit", "AntiAffinity", "AntiAffinityRack", "CEIrack@0.1"]),
    ("alibaba", "alibaba_v5", ["FirstFit", "BestFit", "AntiAffinity", "AntiAffinityRack", "CEI@0.1", "CEIrack@0.1",
                               "Uniform@0.1", "CEI@0.35"]),
]:
    if not ((RES / f"sweep_{old}.csv").exists() and (RES / f"sweep_{new}.csv").exists()):
        continue
    o, n = load(old), load(new)
    out["determinism"][new] = {lab: compare(o, n, lab) for lab in labels}
    if "Uniformrack@0.1" in set(o.label):
        out["uniformrack_changed"][new] = compare(o, n, "Uniformrack@0.1")

total = same = 0
for name, a_lab, b_lab in [("confirm_v5", "CEI@0.1", "Uniform@0.1"), ("confirm_r1_v5", "CEI@0.1", "Uniform@0.1"),
                           ("confirm_r2_v5", "CEI@0.1", "Uniform@0.1"), ("tight_v5", "CEI@0.1", "Uniform@0.1"),
                           ("racks_v5", "CEIrack@0.1", "Uniformrack@0.1"), ("replfrac_v5", "CEIrack@0.1", "Uniformrack@0.1"),
                           ("alibaba_v5", "CEI@0.1", "Uniform@0.1"), ("alibaba_v5", "CEIrack@0.1", "Uniformrack@0.1"),
                           ("alibaba_s1_v5", "CEI@0.1", "Uniform@0.1"), ("alibaba_s1_v5", "CEIrack@0.1", "Uniformrack@0.1")]:
    if not (RES / f"sweep_{name}.csv").exists():
        continue
    df = load(name)
    if name.startswith("alibaba"):
        df = df[df.rack_size == (12 if a_lab.startswith("CEIrack") else 1)]
    a = df[df.label == a_lab].set_index(KEY)
    b = df[df.label == b_lab].set_index(KEY)
    cols = [c for c in a.columns if c.startswith(("e1_", "r1_", "e2_")) or c in ("active_hosts", "power_kw", "unplaced_vms")]
    a, b = a[cols].align(b[cols], join="inner")
    eq = (((a - b).abs() <= 1e-9) | (a.isna() & b.isna())).all(axis=1)
    out["cei_vs_count"][f"{name}:{a_lab}"] = {"cells": int(len(eq)), "identical": int(eq.sum())}
    total += len(eq)
    same += int(eq.sum())
out["cei_vs_count_total"] = {"cells": total, "identical": same}
(RES / "review_checks.json").write_text(json.dumps(out, indent=1))
print(json.dumps(out, indent=1))
