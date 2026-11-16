"""Writes the experiment cell lists run by `modal_app.py::sweep`.

Every cell runs all of its policies on the same generated applications, submission
order and failure order (paired by seed). Seeds 1-10 everywhere except the scale check.
"""

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEEDS = range(1, 11)
BASE = "FirstFit,BestFit,WorstFit,RoundRobin,Random,AntiAffinity,Uniform@0.35,CEI@0.35"
LAMBDAS = [0.0, 0.1, 0.2, 0.35, 0.5, 0.65, 0.8, 0.9, 1.0]


def write(name, cells):
    (HERE / f"sweep_{name}.json").write_text(json.dumps({"cells": cells}, indent=1))
    print(name, len(cells))


main = [
    {"shape": shape, "load": load, "order": order, "replicas": "mixed", "seed": seed, "policies": BASE}
    for shape in ["mixed", "chain", "fanin", "tree", "dsb", "scalefree"]
    for load in [0.5, 0.75, 0.9]
    for order in ["natural", "shuffled"]
    for seed in SEEDS
]
write("main", main)

replicas = [
    {"shape": "mixed", "load": load, "order": order, "replicas": rep, "seed": seed, "policies": BASE}
    for rep in ["1", "2"]
    for load in [0.5, 0.75, 0.9]
    for order in ["natural", "shuffled"]
    for seed in SEEDS
]
write("replicas", replicas)

frontier_policies = ",".join(["BestFit", "AntiAffinity", "WorstFit"]
                             + [f"CEI@{lam}" for lam in LAMBDAS] + [f"Uniform@{lam}" for lam in LAMBDAS])
frontier = [
    {"shape": "mixed", "load": 0.75, "order": order, "replicas": "mixed", "seed": seed, "policies": frontier_policies}
    for order in ["natural", "shuffled"]
    for seed in SEEDS
]
write("frontier", frontier)

scale = [
    {"shape": "mixed", "load": 0.75, "order": "natural", "replicas": "mixed", "seed": seed,
     "hosts": hosts, "failures": 2, "policies": "BestFit,AntiAffinity,CEI@0.35"}
    for hosts in [48, 96, 192, 384]
    for seed in [1, 2, 3]
]
write("scale", scale)

# Confirmation on fresh seeds. lambda = 0.1 was picked from the frontier sweep (seeds 1-10),
# so it is re-tested here on seeds 11-20 that played no part in choosing it.
CONFIRM = "FirstFit,BestFit,AntiAffinity,Uniform@0.1,CEI@0.1,CEI@0.35"
confirm = [
    {"shape": shape, "load": load, "order": order, "replicas": "mixed", "seed": seed, "policies": CONFIRM}
    for shape in ["mixed", "chain", "fanin", "tree", "dsb", "scalefree"]
    for load in [0.5, 0.75, 0.9]
    for order in ["natural", "shuffled"]
    for seed in range(11, 21)
]
write("confirm", confirm)
confirm_r2 = [
    {"shape": "mixed", "load": load, "order": order, "replicas": "2", "seed": seed, "policies": CONFIRM}
    for load in [0.5, 0.75, 0.9]
    for order in ["natural", "shuffled"]
    for seed in range(11, 21)
]
write("confirm_r2", confirm_r2)

# Tight clusters: few hosts, so some replicas must share a host and the policy has to choose
# which services to put at risk. This is where the CEI weights could matter.
# Run with jar tag v2, which adds the e1_declared_critical_broken column.
TIGHT = "BestFit,AntiAffinity,Uniform@0.1,CEI@0.1,CEI@0.35"
tight = [
    {"shape": shape, "load": load, "order": "natural", "replicas": rep, "seed": seed,
     "hosts": hosts, "failures": 4, "policies": TIGHT}
    for hosts in [6, 8, 12]
    for load in [0.85, 0.95]
    for rep in ["2", "mixed"]
    for shape in ["mixed", "dsb"]
    for seed in SEEDS
]
write("tight", tight)

# No replicas, on the fresh seeds, at lambda = 0.1 (the replicas sweep above used 0.35).
confirm_r1 = [
    {"shape": "mixed", "load": load, "order": order, "replicas": "1", "seed": seed, "policies": CONFIRM}
    for load in [0.5, 0.75, 0.9]
    for order in ["natural", "shuffled"]
    for seed in range(11, 21)
]
write("confirm_r1", confirm_r1)

# ---- Round 2 (jar v3: fault domains) ----
RACK = "BestFit,AntiAffinity,AntiAffinityRack,CEI@0.1,Uniformrack@0.1,CEIrack@0.1"
racks = [
    {"shape": "mixed", "load": load, "order": "natural", "replicas": rep, "seed": seed,
     "rackSize": rack_size, "policies": RACK}
    for rack_size in [4, 8, 12, 16, 24]
    for load in [0.75, 0.9, 0.95]
    for rep in ["mixed", "2"]
    for seed in range(21, 31)
]
write("racks", racks)

# Placement time at scale. Each policy runs twice in the same JVM; the second run is the warm one.
scale2 = [
    {"shape": "mixed", "load": 0.75, "order": "natural", "replicas": "mixed", "seed": seed,
     "hosts": hosts, "rackSize": rack_size, "failures": 2,
     "policies": "BestFit,AntiAffinity,CEI@0.1,CEIrack@0.1,BestFit,AntiAffinity,CEI@0.1,CEIrack@0.1"}
    for hosts in [100, 250, 500, 1000, 2000]
    for rack_size in [1, 20]
    for seed in [1, 2, 3]
]
write("scale2", scale2)

ALIBABA = ("FirstFit,BestFit,AntiAffinity,AntiAffinityRack,Uniform@0.1,CEI@0.1,"
           "Uniformrack@0.1,CEIrack@0.1,CEI@0.35")
alibaba = [
    {"shape": "alibaba", "appsFile": "/jars/data/alibaba_apps.txt", "load": load, "order": order,
     "seed": seed, "rackSize": rack_size, "policies": ALIBABA}
    for load in [0.5, 0.75, 0.9]
    for order in ["natural", "shuffled"]
    for rack_size in [1, 12]
    for seed in range(21, 41)
]
write("alibaba", alibaba)

# Share of services replicated, under rack failures (jar v4 adds the "p<share>" replica mode).
REPL = "BestFit,AntiAffinity,AntiAffinityRack,Uniformrack@0.1,CEIrack@0.1"
replfrac = [
    {"shape": "mixed", "load": 0.9, "order": "natural", "replicas": f"p{share}", "seed": seed,
     "rackSize": rack_size, "policies": REPL}
    for share in [0.0, 0.25, 0.5, 0.75, 1.0]
    for rack_size in [8, 16]
    for seed in range(41, 61)
]
write("replfrac", replfrac)

# ---- Review fixes (jar v5): WorstFit (VmPlacementPolicySimple, the DatacenterSimple default) everywhere,
# Uniform* policies truly count-weighted (v3/v4 left Uniformrack on CEI weights), direct placement timing.
V5 = "FirstFit,BestFit,WorstFit,AntiAffinity,Uniform@0.1,CEI@0.1,CEI@0.35"
write("confirm_v5", [dict(c, policies=V5) for c in confirm])
write("confirm_r1_v5", [dict(c, policies=V5) for c in confirm_r1])
write("confirm_r2_v5", [dict(c, policies=V5) for c in confirm_r2])
write("tight_v5", [dict(c, policies="BestFit,WorstFit,AntiAffinity,Uniform@0.1,CEI@0.1,CEI@0.35") for c in tight])
RACK5 = "BestFit,WorstFit,AntiAffinity,AntiAffinityRack,CEI@0.1,Uniformrack@0.1,CEIrack@0.1"
write("racks_v5", [dict(c, policies=RACK5) for c in racks])
write("replfrac_v5", [dict(c, policies="BestFit,WorstFit,AntiAffinity,AntiAffinityRack,Uniformrack@0.1,CEIrack@0.1")
                      for c in replfrac])
ALI5 = ("FirstFit,BestFit,WorstFit,AntiAffinity,AntiAffinityRack,Uniform@0.1,CEI@0.1,"
        "Uniformrack@0.1,CEIrack@0.1,CEI@0.35")
write("alibaba_v5", [dict(c, policies=ALI5) for c in alibaba])
write("alibaba_s1_v5", [dict(c, policies=ALI5, appsFile="/jars/data/alibaba_apps_stateful1.txt") for c in alibaba])
# Timing: one rack size (20 hosts per rack) for every column, 5 seeds, each policy run twice (second = warm).
T5 = ["BestFit", "WorstFit", "AntiAffinity", "CEI@0.1", "CEIrack@0.1"]
write("scale_v5", [
    {"shape": "mixed", "load": 0.75, "order": "natural", "replicas": "mixed", "seed": seed,
     "hosts": hosts, "rackSize": 20, "failures": 2, "policies": ",".join(T5 + T5)}
    for hosts in [100, 250, 500, 1000, 2000]
    for seed in range(1, 6)
])
