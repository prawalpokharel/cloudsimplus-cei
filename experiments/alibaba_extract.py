"""Extracts real application dependency graphs from the Alibaba 2021 microservices trace.

Source: github.com/alibaba/clusterdata, cluster-trace-microservices-v2021 (Luo et al.,
"Characterizing Microservice Dependency and Performance: Alibaba Trace Analysis", SoCC 2021).
The trace owners allow use for research or study and ask users to cite that paper.

Each online service (the entering microservice and the interface the proxy calls, as the
trace README defines it) becomes one application:
- services: the microservices in that service's call graphs (proxy names and missing names dropped);
- edges: caller -> callee pairs seen in those call graphs;
- replicas: from the number of containers of each microservice in MSResource_0
  (1 container -> 1 replica, 2-19 -> 2, 20 or more -> 3); stateful DB/MC callees, which the
  resource table does not cover, get 2; other microservices missing from it get 1;
- declared criticality: 1.0 for the entering microservice and for stateful DB/MC services, else 0.2;
- demand history: calls received per 30 s, divided by the maximum, over the downloaded window;
- cores: 1, 2 or 4 by the tercile of calls received inside the application.

Microservices shared by several online services are copied into each application.

Usage: modal run experiments/alibaba_extract.py
Writes results/alibaba_apps{suffix}.txt (read by the Java harness) and results/alibaba_apps{suffix}_stats.json,
(`--stateful-replicas 1 --suffix _stateful1` gives the sensitivity variant)
and puts a copy of the app file in the cei-cloudsimplus-jars volume under data/.
"""

from __future__ import annotations

import json
from pathlib import Path

import modal

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
URL = "https://aliopentrace.oss-cn-beijing.aliyuncs.com/v2021MicroservicesTraces"
CALLGRAPH_FILES = [0, 1, 2, 3]
RESOURCE_FILE = 0

app = modal.App("cei-cloudsimplus")
jars = modal.Volume.from_name("cei-cloudsimplus-jars", create_if_missing=True)
image = modal.Image.debian_slim(python_version="3.12").apt_install("curl").pip_install("pandas==2.2.3", "numpy==2.1.3", "requests==2.32.3")

BAD = {"", "(?)", "NAN", "nan", "UNKNOWN", "UNAVAILABLE"}


def _cached(url: str) -> Path:
    """Downloads a trace file once into the volume, so a retry doesn't fetch it again."""
    import subprocess

    raw = Path("/jars/data/raw")
    raw.mkdir(parents=True, exist_ok=True)
    dest = raw / url.rsplit("/", 1)[-1]
    if not dest.exists():
        tmp = dest.with_suffix(".part")
        subprocess.run(["curl", "-sSfL", "--retry", "5", "-o", str(tmp), url], check=True)
        tmp.rename(dest)
        jars.commit()
    print(f"have {dest.name} ({dest.stat().st_size / 1e6:.0f} MB)", flush=True)
    return dest


def _stream_csv(url: str, usecols=None, chunksize=None, dtype=str):
    """Streams the single CSV inside a cached .tar.gz into pandas without extracting it to disk."""
    import subprocess

    import pandas as pd

    path = _cached(url)
    proc = subprocess.Popen(["tar", "-xzOf", str(path)], stdout=subprocess.PIPE)
    return path.name, pd.read_csv(proc.stdout, usecols=usecols, chunksize=chunksize, dtype=dtype)


def _count_instances(log):
    import collections
    import time

    t0 = time.time()
    instances = collections.defaultdict(set)
    name, chunks = _stream_csv(f"{URL}/MSResource/MSResource_{RESOURCE_FILE}.tar.gz",
                               usecols=lambda c: c.lower() in {"msname", "msinstanceid"}, chunksize=2_000_000)
    for chunk in chunks:
        cols = {c.lower(): c for c in chunk.columns}
        for ms, inst in zip(chunk[cols["msname"]], chunk[cols["msinstanceid"]]):
            instances[ms].add(inst)
    log.append(f"MSResource {name}: {len(instances)} microservices, "
               f"{sum(len(v) for v in instances.values())} containers ({time.time() - t0:.0f}s)")
    return instances


@app.function(image=image, cpu=4, memory=32768, timeout=3 * 3600, volumes={"/jars": jars})
def extract(min_traces: int = 20, min_services: int = 6, max_services: int = 40,
            stateful_replicas: int = 2, suffix: str = "") -> dict:
    import collections
    import time

    import numpy as np
    import pandas as pd

    log = []
    t0 = time.time()

    # 1. containers per microservice (cached in the volume after the first run)
    counts_file = Path("/jars/data/ms_instances.json")
    instances = collections.defaultdict(set)
    if counts_file.exists():
        cached = json.loads(counts_file.read_text())
        instances = collections.defaultdict(set, {k: set(range(v)) for k, v in cached.items()})
        log.append(f"MSResource counts from cache: {len(instances)} microservices")
    else:
        instances = _count_instances(log)
        counts_file.write_text(json.dumps({k: len(v) for k, v in instances.items()}))
        jars.commit()
    print(log[-1], flush=True)

    # 2. call graphs
    edge_counts = collections.Counter()      # (key, um, dm) -> calls
    trace_count = collections.Counter()      # key -> traces
    entries = {}                             # key -> entering microservice
    stateful = set()
    calls_bin = collections.Counter()        # (dm, 30 s bin) -> calls
    proxies = set()
    rows_total = 0
    for i in CALLGRAPH_FILES:
        wanted = {"timestamp", "traceid", "rpcid", "um", "rpctype", "interface", "dm"}
        name, df = _stream_csv(f"{URL}/MSCallGraph/MSCallGraph_{i}.tar.gz", usecols=lambda c: c in wanted,
                               dtype={c: "category" for c in wanted - {"timestamp"}})
        log.append(f"{name}: columns {list(df.columns)}, {len(df)} rows")
        rows_total += len(df)
        df = df.dropna(subset=["traceid", "rpcid", "um", "dm"])
        df["depth"] = df["rpcid"].str.count(r"\.")
        # the root call of each trace: smallest depth, then smallest rpcid
        roots = df.sort_values(["traceid", "depth", "rpcid"]).drop_duplicates("traceid")
        roots = roots[~roots["dm"].isin(BAD)]
        root_dm = roots["dm"].astype(str)
        root_key = root_dm + "|" + roots["interface"].astype(str).replace("nan", "")
        key_of = dict(zip(roots["traceid"].astype(str), root_key))
        for k, entry in zip(root_key, root_dm):
            entries[k] = entry
        proxies.update(roots["um"].astype(str).tolist())
        trace_count.update(key_of.values())
        df["key"] = df["traceid"].astype(str).map(key_of)
        df = df.dropna(subset=["key"])
        st = df[df["rpctype"].astype(str).str.lower().isin(["db", "mc"])]["dm"]
        stateful.update(st.astype(str).tolist())
        shared = df["um"].cat.categories.union(df["dm"].cat.categories)
        df["um"] = df["um"].cat.set_categories(shared)
        df["dm"] = df["dm"].cat.set_categories(shared)
        df = df[~df["um"].isin(BAD) & ~df["dm"].isin(BAD) & (df["um"] != df["dm"])]
        df = df[~df["um"].astype(str).isin(proxies)]
        # RPC calls appear twice (once from each side); count unique (trace, rpcid, um, dm)
        df = df.drop_duplicates(["traceid", "rpcid", "um", "dm"])
        for (k, um, dm), n in df.groupby(["key", "um", "dm"], observed=True).size().items():
            edge_counts[(k, um, dm)] += int(n)
        ts = pd.to_numeric(df["timestamp"], errors="coerce").fillna(0).astype(np.int64) // 30000
        for (dm, b), n in df.assign(bin=ts).groupby(["dm", "bin"], observed=True).size().items():
            calls_bin[(dm, int(b))] += int(n)
        log.append(f"after file {i}: {len(trace_count)} services, {len(edge_counts)} edges ({time.time() - t0:.0f}s)")
        print(log[-1], flush=True)

    # 3. assemble applications
    by_key = collections.defaultdict(list)
    for (k, um, dm), n in edge_counts.items():
        by_key[k].append((um, dm, n))
    bins = sorted({b for (_, b) in calls_bin})
    calls_total = collections.Counter()
    for (dm, b), n in calls_bin.items():
        calls_total[dm] += n

    def replicas_of(ms):
        if ms in instances:
            c = len(instances[ms])
            return 1 if c <= 1 else (2 if c < 20 else 3)
        return stateful_replicas if ms in stateful else 1

    apps = []
    sizes = []
    for k, edges in sorted(by_key.items(), key=lambda kv: -trace_count[kv[0]]):
        if trace_count[k] < min_traces:
            continue
        entry = entries[k]
        nodes = sorted({u for u, _, _ in edges} | {d for _, d, _ in edges} | {entry})
        if not (min_services <= len(nodes) <= max_services):
            continue
        nodes.remove(entry)
        nodes = [entry] + nodes
        idx = {n: j for j, n in enumerate(nodes)}
        vol = [calls_total.get(n, 0) for n in nodes]
        q1, q2 = np.quantile(vol, [1 / 3, 2 / 3])
        services = []
        for j, n in enumerate(nodes):
            hist = np.array([calls_bin.get((n, b), 0) for b in bins], dtype=float)
            hist = hist / hist.max() if hist.max() > 0 else np.zeros_like(hist)
            cores = 1 if vol[j] <= q1 else (2 if vol[j] <= q2 else 4)
            declared = 1.0 if (j == 0 or n in stateful) else 0.2
            services.append({"name": n[:12], "replicas": replicas_of(n), "cores": cores,
                             "declared": declared, "history": [round(float(x), 4) for x in hist],
                             "assumed": n in stateful and n not in instances})
        app_edges = sorted({(idx[u], idx[d]) for u, d, _ in edges})
        apps.append({"key": k[:24], "traces": trace_count[k], "services": services, "edges": app_edges})
        sizes.append(len(nodes))

    lines = []
    for a_i, a in enumerate(apps):
        lines.append(f"app {a_i} {a['key'].replace(' ', '_')} {a['traces']}")
        for s in a["services"]:
            lines.append(f"svc {s['name']} {s['replicas']} {s['cores']} {s['declared']} "
                         + ",".join(str(x) for x in s["history"]))
        for u, d in a["edges"]:
            lines.append(f"edge {u} {d}")
        lines.append("end")
    text = "\n".join(lines) + "\n"
    out = Path("/jars/data")
    out.mkdir(parents=True, exist_ok=True)
    (out / f"alibaba_apps{suffix}.txt").write_text(text)
    jars.commit()

    rep = collections.Counter(s["replicas"] for a in apps for s in a["services"])
    indeg_max = []
    for a in apps:
        indeg = collections.Counter(d for _, d in a["edges"])
        indeg_max.append(max(indeg.values()) if indeg else 0)
    stats = {
        "callgraph_files": CALLGRAPH_FILES, "resource_file": RESOURCE_FILE, "rows": rows_total,
        "window_bins_30s": len(bins), "online_services_seen": len(trace_count),
        "apps_kept": len(apps), "app_size_mean": float(np.mean(sizes)) if sizes else 0,
        "app_size_quantiles": [float(x) for x in np.quantile(sizes, [0, 0.25, 0.5, 0.75, 1])] if sizes else [],
        "edges_mean": float(np.mean([len(a["edges"]) for a in apps])) if apps else 0,
        "replica_counts": dict(rep), "stateful_services": sum(1 for a in apps for s in a["services"] if s["declared"] == 1.0),
        "max_in_degree_median": float(np.median(indeg_max)) if indeg_max else 0,
        "microservices_with_containers": len(instances), "seconds": round(time.time() - t0),
        "stateful_replicas": stateful_replicas,
        "stateful_not_in_resource_table": sum(1 for a in apps for s in a["services"] if s.get("assumed")),
        "log": log,
    }
    return {"text": text, "stats": stats}


@app.local_entrypoint()
def main(stateful_replicas: int = 2, suffix: str = ""):
    res = extract.remote(stateful_replicas=stateful_replicas, suffix=suffix)
    (ROOT / "results" / f"alibaba_apps{suffix}.txt").write_text(res["text"])
    (ROOT / "results" / f"alibaba_apps{suffix}_stats.json").write_text(json.dumps(res["stats"], indent=1))
    print(json.dumps({k: v for k, v in res["stats"].items() if k != "log"}, indent=1))
    print("\n".join(res["stats"]["log"]))
