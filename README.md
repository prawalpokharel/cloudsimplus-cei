# cloudsimplus-cei

Dependency-aware VM placement for [CloudSim Plus](https://github.com/cloudsimplus/cloudsimplus), and a study of how much a single host failure breaks under each placement policy.

CloudSim Plus places each VM on its own: `VmPlacementPolicyBestFit`, `FirstFit` and the others do not know that two VMs are replicas of the same service, or that one service calls another. The default (`VmPlacementPolicySimple`, worst fit) spreads VMs over every host; the consolidating policies can stack both replicas of a database on one host, and a single host failure then takes down the database and everything that calls it. None of them knows about racks. This project measures these effects and adds a policy that knows about replicas, dependencies and fault domains.

## What is here

- `VmDependencyGraph`, `VmPlacementPolicyDependencyCriticality` and `CriticalityIndex` live on the [`cei-placement` branch of the CloudSim Plus fork](https://github.com/prawalpokharel/cloudsimplus/tree/cei-placement), proposed for the core library. They use only public CloudSim Plus API, so they can also move into this project unchanged except for the package name.
- `src/main/java/io/github/prawalpokharel/cei/` has the experiment:
  - `AppGenerator` builds applications whose services form a chain, a fan-in onto a shared database, a fan-out tree, the DeathStarBench social network (Gan et al., ASPLOS 2019) or a preferential-attachment graph.
  - `BlastRadiusExperiment` places them with eight policies on the same 48-host datacenter, fails hosts with `HostFaultInjection`, and prints one CSV row per policy.
  - `ScheduledHostFaultInjection` turns off the random failure schedule so every policy sees the same failures.

## How the policy chooses a host

For each suitable host the policy computes

```
cost = lambda * added power + (1 - lambda) * added risk
```

- *added power* is the extra power the VM needs on that host as a fraction of its maximum power, including the static power when the host is empty;
- *added risk* is the criticality that a failure of that host would newly break, as a share of the criticality of the VM's application. A service is down when all of its working replicas are on the failed host, and broken when it, or anything it depends on, is down.

By default every service weighs 1, so the risk counts broken services. `graph.useCriticalityIndex()` weighs each service by its Centrality-Entropy Index (CEI) instead: `0.40 * centrality + 0.35 * demand entropy + 0.25 * declared criticality`, where centrality combines PageRank, betweenness, degree and closeness (P. Pokharel, IEMCON 2026).

```java
var graph = new VmDependencyGraph()
    .addVm("db", db1).addVm("db", db2)
    .addVm("api", api1)
    .addDependency("api", "db")
    .setDeclaredCriticality("db", 1.0);
var policy = new VmPlacementPolicyDependencyCriticality(graph, 0.1)
    .setFaultDomain(host -> host.getId() / 12);   // optional: 12 hosts per rack
var datacenter = DatacenterSimple.of(simulation, hosts, policy);
```

## Build and run

Requires JDK 25 and Maven 3.9.

```bash
git clone -b cei-placement https://github.com/prawalpokharel/cloudsimplus
(cd cloudsimplus && mvn install -DskipTests)
mvn package
java -jar target/cloudsimplus-cei-0.1.0-SNAPSHOT-jar-with-dependencies.jar --shape mixed --load 0.75 --seed 1
```

Options: `--shape mixed|chain|fanin|tree|dsb|scalefree`, `--load` (share of cores used), `--hosts`, `--hostCores`, `--replicas mixed|1|2`, `--order natural|shuffled`, `--failures`, `--policies FirstFit,BestFit,...,CEI@0.1`.

## Results

Measured on Modal with CloudSim Plus 9.0.0-SNAPSHOT (commit 4a160b4 plus the `cei-placement` branch). Paired runs. 95% intervals are clustered by seed, because cells with the same seed share applications. Power is simulated with `PowerModelHostSimple` (linear, hosts without VMs off).

**Host failures** (360 cells, seeds 11-20, critical and shared services replicated twice):

| Policy | Services broken per host failure | Replicas stacked on one host | Apps working after 8 host failures | Simulated power | Unplaced VMs |
|---|---|---|---|---|---|
| `VmPlacementPolicySimple` (worst fit, the default) | **6.51** | 0.7 | 32.3% | 8.47 kW | 0.51 |
| BestFit (consolidating) | 8.83 | 32.9 | 49.6% | 6.79 kW | 0 |
| FirstFit (consolidating) | 8.39 | 36.0 | 49.9% | 6.99 kW | 0 |
| BestFit + replica anti-affinity | 7.35 | 0 | 51.9% | 6.82 kW | 0 |
| Dependency-criticality, `lambda = 0.1` | 7.25 | 0.2 | **52.6%** | 6.82 kW | 0 |

- The default spreads VMs over every host. It breaks 11% fewer services per single host failure than the dependency-criticality policy, but uses 24% more simulated power, leaves VMs unplaced at 90% load and keeps fewer applications working over a failure sequence.
- At BestFit's power, the policy breaks 17.8% fewer services per host failure than BestFit (95% CI 17.5-18.1%). With no replicas the gain is 0.8%. Plain anti-affinity gets almost all of it: the policy is only 1.3% better (CI 1.1-1.5%).

**Rack failures** (300 cells, seeds 21-30, 2 to 12 racks):

| Policy | Services broken per rack failure | Declared-critical services broken |
|---|---|---|
| `VmPlacementPolicySimple` (default) | 90.6 | 32.3 |
| BestFit | 67.1 | 20.8 |
| Rack-aware anti-affinity | 60.4 | 16.8 |
| Dependency-criticality + `setFaultDomain`, count weights | 51.1 | 12.0 |
| Dependency-criticality + `setFaultDomain`, CEI weights | **49.1** | **11.0** |

- Host-level policies, including host anti-affinity, are no better than BestFit when a rack fails, and the default is the worst.
- With every service replicated, rack anti-affinity is enough. With some services unreplicated, the dependency-aware policy breaks 18.8% fewer services than rack anti-affinity (CI 16.7-20.9%). The gain is 6.5%, 8.0% and 12.9% at 25%, 50% and 75% of services replicated.
- The CEI weights (`useCriticalityIndex()`) made no measurable difference for host failures (identical in 906 of 960 cells, within 0.2% in the rest). Under rack failures they broke 3.9% fewer services and 7.6% fewer critical services than counting.

**Call graphs from the Alibaba 2021 microservice trace** (Luo et al., SoCC 2021; 119 online services, 240 cells per variant): the trace has no replica counts for its stateful DB and memcached callees, which are 62% of the services.
- With those callees at 2 replicas: 56% fewer services broken per host failure than BestFit, level with plain anti-affinity, and 7.9% fewer than rack anti-affinity per rack failure.
- With them at 1 replica: only 2.6% fewer than BestFit, and 3.0% fewer than rack anti-affinity per rack failure.

**Scale**: timed around `findHostForVm`, placing about 12,000 VMs on 2,000 hosts took 9.8 s, against 6.3 s for BestFit (medians of 5 seeds).

## Reproducing the study

The sweeps run on [Modal](https://modal.com) (CPU only; the whole study cost about $1). `experiments/` has the sweep definitions, the runner and the analysis, and `results/` has every CSV the tables come from.

```bash
git clone -b cei-placement https://github.com/prawalpokharel/cloudsimplus repo   # the policy, built on Modal
python experiments/make_sweeps.py                                                 # writes experiments/sweep_*.json
modal run experiments/modal_app.py::build --tests full --tag v5                   # JDK 25 + Maven image, all tests, experiment jar
modal run experiments/alibaba_extract.py                                          # Alibaba apps (stateful callees at 2 replicas)
modal run experiments/alibaba_extract.py --stateful-replicas 1 --suffix _stateful1
modal run --detach experiments/modal_app.py::sweep_detached --tag v5 \
    --names confirm_v5,confirm_r1_v5,confirm_r2_v5,tight_v5,racks_v5,replfrac_v5,alibaba_v5,alibaba_s1_v5,scale_v5
modal volume get cei-cloudsimplus-jars results/v5 /tmp/v5 && cp /tmp/v5/v5/sweep_*.csv results/
python experiments/analyze.py && python experiments/analyze.py --round2 && python experiments/check_v5.py
```

The exploration sweeps on seeds 1-10 (`sweep_main.csv`, `sweep_frontier.csv`, `sweep_replicas.csv`) and the earlier v2-v4 runs are kept in `results/`. `check_v5.py` shows that every policy whose weights did not change gives identical results in the v5 reruns. Intervals in `results/summary*.json` are 95% and clustered by seed. The Alibaba application files are not included: `experiments/alibaba_extract.py` rebuilds them from the public trace.

## Licence

GPL-3.0-or-later, the same as CloudSim Plus. If you use CloudSim Plus in research, cite M. C. Silva Filho, R. L. Oliveira, C. C. Monteiro, P. R. M. Inácio and M. M. Freire, "CloudSim Plus: a Cloud Computing Simulation Framework Pursuing Software Engineering Principles for Improved Modularity, Extensibility and Correctness", IFIP/IEEE IM 2017.
