package io.github.prawalpokharel.cei;

import ch.qos.logback.classic.Level;
import org.cloudsimplus.brokers.DatacenterBrokerSimple;
import org.cloudsimplus.cloudlets.Cloudlet;
import org.cloudsimplus.cloudlets.CloudletSimple;
import org.cloudsimplus.core.CloudSimPlus;
import org.cloudsimplus.datacenters.DatacenterSimple;
import org.cloudsimplus.distributions.UniformDistr;
import org.cloudsimplus.hosts.Host;
import org.cloudsimplus.hosts.HostSimple;
import org.cloudsimplus.placementpolicies.VmDependencyGraph;
import org.cloudsimplus.placementpolicies.VmPlacementPolicy;
import org.cloudsimplus.placementpolicies.VmPlacementPolicyBestFit;
import org.cloudsimplus.placementpolicies.VmPlacementPolicyDependencyCriticality;
import org.cloudsimplus.placementpolicies.VmPlacementPolicyFirstFit;
import org.cloudsimplus.placementpolicies.VmPlacementPolicyRandom;
import org.cloudsimplus.placementpolicies.VmPlacementPolicyRoundRobin;
import org.cloudsimplus.placementpolicies.VmPlacementPolicySimple;
import org.cloudsimplus.power.models.PowerModelHostSimple;
import org.cloudsimplus.resources.CoreCapacity;
import org.cloudsimplus.resources.Processor;
import org.cloudsimplus.schedulers.vm.VmSchedulerTimeShared;
import org.cloudsimplus.util.Log;
import org.cloudsimplus.utilizationmodels.UtilizationModelDynamic;
import org.cloudsimplus.vms.Vm;
import org.cloudsimplus.vms.VmSimple;

import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.IdentityHashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Optional;
import java.util.Random;
import java.util.Set;
import java.util.stream.Collectors;

/// Places generated applications with several VM placement policies on the same
/// datacenter, then measures what host failures break.
///
/// One run per policy, all from the same applications, submission order and
/// failure sequence, so the comparison is paired:
///
/// 1. **Placement snapshot** (120 s): active hosts, power draw from the
///    `PowerModelHostSimple` of each host with VMs, and VMs that could not be placed.
/// 2. **Single-host failures (E1)**: for each host that has VMs, the VMs it holds,
///    the services that failing it would break (a service is down when all its replicas
///    are on the host, and broken when it or anything it depends on is down), the
///    applications hit, and the healthy VMs whose service breaks. Computed from the
///    placement the simulator produced.
/// 3. **Failure sequence (E2)**: `HostFaultInjection.generateHostFault(host)` fails whole
///    hosts every 600 s, in a random host order shared by all policies (empty hosts included,
///    so packing into fewer hosts also lowers exposure). After each failure the run records
///    the failed VMs and the applications still fully working.
///
/// Arguments: `--shape mixed --load 0.75 --seed 1 --hosts 48 --hostCores 16 --replicas mixed
/// --order natural --failures 8 --policies FirstFit,BestFit,...,CEI@0.35`.
/// Prints CSV with a header line.
public final class BlastRadiusExperiment {
    private static final double HOST_MIPS = 1000;
    private static final double MAX_POWER = 300;
    private static final double STATIC_POWER = 120;
    private static final double SNAPSHOT_TIME = 120;
    private static final double FAILURE_GAP = 600;
    private static final String DEFAULT_POLICIES =
        "FirstFit,BestFit,WorstFit,RoundRobin,Random,AntiAffinity,Uniform@0.35,CEI@0.35";

    private BlastRadiusExperiment() {/**/}

    public static void main(final String[] args) throws java.io.IOException {
        Log.setLevel(Level.OFF);
        final Map<String, String> opts = parse(args);
        final String shape = opts.getOrDefault("shape", "mixed");
        final double load = Double.parseDouble(opts.getOrDefault("load", "0.75"));
        final long seed = Long.parseLong(opts.getOrDefault("seed", "1"));
        final int hosts = Integer.parseInt(opts.getOrDefault("hosts", "48"));
        final int hostCores = Integer.parseInt(opts.getOrDefault("hostCores", "16"));
        final String replicas = opts.getOrDefault("replicas", "mixed");
        final String order = opts.getOrDefault("order", "natural");
        final int failures = Integer.parseInt(opts.getOrDefault("failures", "8"));
        final int rackSize = Integer.parseInt(opts.getOrDefault("rackSize", "1"));
        final List<String> policies = List.of(opts.getOrDefault("policies", DEFAULT_POLICIES).split(","));
        if (failures > hosts) {
            throw new IllegalArgumentException("--failures cannot be more than --hosts, since each host fails once");
        }

        final List<AppSpec> apps = "alibaba".equals(shape)
            ? AppGenerator.fromFile(java.nio.file.Path.of(opts.get("appsFile")), load, hosts * hostCores, seed)
            : AppGenerator.generate(shape, load, hosts * hostCores, replicas, seed);
        final Map<String, Double> criticality = referenceCriticality(apps);
        final var setup = new Setup(shape, load, seed, hosts, hostCores, "alibaba".equals(shape) ? "trace" : replicas,
                                    order, failures, rackSize);
        boolean header = true;
        for (final String policy : policies) {
            final long start = System.nanoTime();
            final var row = run(apps, policy, setup, criticality);
            row.put("wall_ms", (System.nanoTime() - start) / 1e6);
            if (header) {
                System.out.println(String.join(",", row.keySet()));
                header = false;
            }

            System.out.println(row.values().stream().map(BlastRadiusExperiment::format).collect(Collectors.joining(",")));
        }
    }

    record Setup(String shape, double load, long seed, int hosts, int hostCores,
                 String replicas, String order, int failures, int rackSize) {}

    /// CEI score of every service, from a graph with only services, used to weight losses
    /// the same way for every policy.
    static Map<String, Double> referenceCriticality(final List<AppSpec> apps) {
        final var graph = new VmDependencyGraph().useCriticalityIndex();
        describe(graph, apps);
        final var result = new HashMap<String, Double>();
        for (final AppSpec app : apps) {
            for (int s = 0; s < app.services().size(); s++) {
                result.put(app.qualifiedName(s), graph.getCriticality(app.qualifiedName(s)));
            }
        }

        return result;
    }

    private static void describe(final VmDependencyGraph graph, final List<AppSpec> apps) {
        for (final AppSpec app : apps) {
            for (int s = 0; s < app.services().size(); s++) {
                final var svc = app.services().get(s);
                graph.addService(app.qualifiedName(s))
                     .setDeclaredCriticality(app.qualifiedName(s), svc.declared())
                     .setDemandHistory(app.qualifiedName(s), svc.history());
            }

            for (final int[] e : app.edges()) {
                graph.addDependency(app.qualifiedName(e[0]), app.qualifiedName(e[1]));
            }
        }
    }

    static Map<String, Object> run(final List<AppSpec> apps, final String policyName, final Setup setup,
                                   final Map<String, Double> criticality) {
        final var sim = new CloudSimPlus();
        final var hostList = new ArrayList<Host>(setup.hosts());
        for (int i = 0; i < setup.hosts(); i++) {
            final Host host = HostSimple.of(setup.hostCores() * 4096L, 100_000, 1_000_000,
                new CoreCapacity(setup.hostCores(), HOST_MIPS), true);
            host.setVmScheduler(new VmSchedulerTimeShared());
            host.setPowerModel(new PowerModelHostSimple(MAX_POWER, STATIC_POWER));
            hostList.add(host);
        }

        final var graph = new VmDependencyGraph();
        describe(graph, apps);
        final String[] parts = policyName.split("@");
        final String base = parts[0];
        final double lambda = parts.length > 1 ? Double.parseDouble(parts[1]) : Double.NaN;
        /* CEI policies weigh services by the Centrality-Entropy Index. Every other policy, including
         * the Uniform ones, keeps the graph default of weight 1 per service (a count). */
        if (base.startsWith("CEI")) {
            graph.useCriticalityIndex();
        }

        final var vms = new ArrayList<Vm>();
        final var serviceApp = new HashMap<String, Integer>();
        final var declaredCritical = new HashSet<String>();
        final var cloudlets = new ArrayList<Cloudlet>();
        for (final AppSpec app : apps) {
            for (int s = 0; s < app.services().size(); s++) {
                final var svc = app.services().get(s);
                serviceApp.put(app.qualifiedName(s), app.id());
                if (svc.declared() >= 1.0) {
                    declaredCritical.add(app.qualifiedName(s));
                }
                for (int r = 0; r < svc.replicas(); r++) {
                    final Vm vm = VmSimple.of(vms.size(), new CoreCapacity(svc.cores(), HOST_MIPS));
                    vm.setRam(svc.cores() * 2048L).setBw(100).setSize(1000);
                    graph.addVm(app.qualifiedName(s), vm);
                    vms.add(vm);
                    final Cloudlet cloudlet = CloudletSimple.of(-10_000, svc.cores());
                    cloudlet.setUtilizationModelCpu(new UtilizationModelDynamic(svc.meanUtilization()))
                            .setUtilizationModelRam(new UtilizationModelDynamic(0.1))
                            .setUtilizationModelBw(new UtilizationModelDynamic(0.1));
                    cloudlet.setVm(vm);
                    cloudlets.add(cloudlet);
                }
            }
        }

        final var submitted = new ArrayList<>(vms);
        if ("shuffled".equals(setup.order())) {
            Collections.shuffle(submitted, new Random(setup.seed() * 31 + 7));
        }

        final Map<Host, Integer> rackOf = new IdentityHashMap<>();
        for (int i = 0; i < hostList.size(); i++) {
            rackOf.put(hostList.get(i), i / Math.max(1, setup.rackSize()));
        }

        final VmPlacementPolicy policy = policy(base, lambda, graph, setup.seed(), rackOf);
        PLACEMENT_NANOS.reset();

        final var datacenter = DatacenterSimple.of(sim, hostList, policy);
        datacenter.setSchedulingInterval(60);
        final var broker = new DatacenterBrokerSimple(sim);
        broker.submitVmList(submitted);
        broker.submitCloudletList(cloudlets);
        final var faults = new ScheduledHostFaultInjection(datacenter, setup.seed());

        final var failureOrder = new ArrayList<Integer>();
        for (int i = 0; i < setup.hosts(); i++) {
            failureOrder.add(i);
        }

        Collections.shuffle(failureOrder, new Random(setup.seed() * 131 + 17));
        final var row = new LinkedHashMap<String, Object>();
        row.put("shape", setup.shape());
        row.put("load", setup.load());
        row.put("seed", setup.seed());
        row.put("replicas", setup.replicas());
        row.put("order", setup.order());
        row.put("hosts", setup.hosts());
        row.put("rack_size", setup.rackSize());
        row.put("policy", base);
        row.put("lambda", Double.isNaN(lambda) ? "" : lambda);
        row.put("apps", apps.size());
        row.put("services", serviceApp.size());
        row.put("vms", vms.size());

        final var state = new RunState();
        sim.listeners().addOnClockTick(info -> {
            final double time = info.getTime();
            if (!state.snapshotDone && time >= SNAPSHOT_TIME) {
                snapshot(row, hostList, vms, graph, serviceApp, criticality, declaredCritical, failureOrder,
                         setup.rackSize());
                state.snapshotDone = true;
            }

            while (state.snapshotDone && state.injected < setup.failures()
                   && time >= SNAPSHOT_TIME + FAILURE_GAP * (state.injected + 1)) {
                final Host host = hostList.get(failureOrder.get(state.injected));
                faults.generateHostFault(host);
                state.injected++;
                recordAfterFailure(row, state.injected, vms, graph, serviceApp, apps.size());
            }
        });

        sim.terminateAt(SNAPSHOT_TIME + FAILURE_GAP * (setup.failures() + 1));
        sim.start();
        if (!state.snapshotDone || state.injected < setup.failures()) {
            throw new IllegalStateException("simulation ended before the snapshot or the failures: " + policyName);
        }

        row.put("fi_host_faults", faults.getHostFaultsNumber());
        row.put("placement_ms", PLACEMENT_NANOS.sum() / 1e6);
        return row;
    }

    private static final class RunState {
        boolean snapshotDone;
        int injected;
    }

    /// Total time spent inside the host search of the current run, in nanoseconds.
    static final java.util.concurrent.atomic.LongAdder PLACEMENT_NANOS = new java.util.concurrent.atomic.LongAdder();

    private static Optional<Host> timed(final java.util.function.Supplier<Optional<Host>> search) {
        final long start = System.nanoTime();
        try {
            return search.get();
        } finally {
            PLACEMENT_NANOS.add(System.nanoTime() - start);
        }
    }

    static VmPlacementPolicy policy(final String name, final double lambda, final VmDependencyGraph graph,
                                    final long seed, final Map<Host, Integer> rackOf) {
        return switch (name) {
            case "AntiAffinityRack" -> antiAffinityRack(graph, rackOf);
            case "Uniformrack", "CEIrack" -> new VmPlacementPolicyDependencyCriticality(graph, lambda) {
                @Override protected Optional<Host> defaultFindHostForVm(final Vm vm) {
                    return timed(() -> super.defaultFindHostForVm(vm));
                }
            }.setFaultDomain(rackOf::get);
            case "FirstFit" -> new VmPlacementPolicyFirstFit() {
                @Override protected Optional<Host> defaultFindHostForVm(final Vm vm) {
                    return timed(() -> super.defaultFindHostForVm(vm));
                }
            };
            case "BestFit" -> new VmPlacementPolicyBestFit() {
                @Override protected Optional<Host> defaultFindHostForVm(final Vm vm) {
                    return timed(() -> super.defaultFindHostForVm(vm));
                }
            };
            case "WorstFit" -> new VmPlacementPolicySimple() {
                @Override protected Optional<Host> defaultFindHostForVm(final Vm vm) {
                    return timed(() -> super.defaultFindHostForVm(vm));
                }
            };
            case "RoundRobin" -> new VmPlacementPolicyRoundRobin() {
                @Override protected Optional<Host> defaultFindHostForVm(final Vm vm) {
                    return timed(() -> super.defaultFindHostForVm(vm));
                }
            };
            case "Random" -> new VmPlacementPolicyRandom(new UniformDistr(seed + 99)) {
                @Override protected Optional<Host> defaultFindHostForVm(final Vm vm) {
                    return timed(() -> super.defaultFindHostForVm(vm));
                }
            };
            case "AntiAffinity" -> antiAffinity(graph);
            case "Uniform", "CEI" -> new VmPlacementPolicyDependencyCriticality(graph, lambda) {
                @Override protected Optional<Host> defaultFindHostForVm(final Vm vm) {
                    return timed(() -> super.defaultFindHostForVm(vm));
                }
            };
            default -> throw new IllegalArgumentException("unknown policy " + name);
        };
    }

    /// Best fit among the hosts that don't hold another replica of the same service,
    /// falling back to plain best fit. It ignores dependencies and criticality.
    /// It is passed as a function, the way a user adds a policy without subclassing.
    static VmPlacementPolicy antiAffinity(final VmDependencyGraph graph) {
        final Comparator<Host> bestFit = Comparator.comparing(Host::isActive).reversed()
            .thenComparingLong(Processor::freeCores)
            .thenComparingLong(Host::getId);
        return new VmPlacementPolicyBestFit().setFindHostForVmFunction((policy, vm) -> timed(() -> {
            final List<Vm> replicas = graph.getReplicas(graph.getService(vm));
            final List<Host> suitable = policy.getHostList().stream().filter(h -> h.isSuitableForVm(vm)).toList();
            final Optional<Host> spread = suitable.stream()
                .filter(h -> replicas.stream().noneMatch(other -> other != vm && other.getHost() == h))
                .min(bestFit);
            return spread.isPresent() ? spread : suitable.stream().min(bestFit);
        }));
    }

    /// Like [#antiAffinity(VmDependencyGraph)], but first looks for a host in a rack that holds
    /// no other replica of the service, then for a host without one, then any host (best fit each time).
    static VmPlacementPolicy antiAffinityRack(final VmDependencyGraph graph, final Map<Host, Integer> rackOf) {
        final Comparator<Host> bestFit = Comparator.comparing(Host::isActive).reversed()
            .thenComparingLong(Processor::freeCores)
            .thenComparingLong(Host::getId);
        return new VmPlacementPolicyBestFit().setFindHostForVmFunction((policy, vm) -> timed(() -> {
            final List<Vm> others = graph.getReplicas(graph.getService(vm)).stream()
                .filter(other -> other != vm && other.getHost() != Host.NULL).toList();
            final Set<Integer> usedRacks = others.stream().map(o -> rackOf.get(o.getHost())).collect(Collectors.toSet());
            final List<Host> suitable = policy.getHostList().stream().filter(h -> h.isSuitableForVm(vm)).toList();
            final Optional<Host> otherRack = suitable.stream().filter(h -> !usedRacks.contains(rackOf.get(h))).min(bestFit);
            if (otherRack.isPresent()) {
                return otherRack;
            }

            final Optional<Host> otherHost = suitable.stream()
                .filter(h -> others.stream().noneMatch(o -> o.getHost() == h)).min(bestFit);
            return otherHost.isPresent() ? otherHost : suitable.stream().min(bestFit);
        }));
    }

    private static void snapshot(final Map<String, Object> row, final List<Host> hosts, final List<Vm> vms,
                                 final VmDependencyGraph graph, final Map<String, Integer> serviceApp,
                                 final Map<String, Double> criticality, final Set<String> declaredCritical,
                                 final List<Integer> failureOrder, final int rackSize) {
        /* With a rack size of 1 each host is its own rack, so the r1_ columns repeat E1.
         * They are always written so every row has the same columns. */
        rackFailures(row, hosts, vms, graph, serviceApp, criticality, declaredCritical, Math.max(1, rackSize));

        final List<Vm> unplaced = vms.stream().filter(vm -> !vm.isCreated()).toList();
        final Set<String> brokenBefore = graph.brokenServices(unplaced);
        final double totalCriticality = criticality.values().stream().mapToDouble(Double::doubleValue).sum();
        double powerW = 0;
        int active = 0;
        final var vmsLost = new ArrayList<Double>();
        final var servicesBroken = new ArrayList<Double>();
        final var appsHit = new ArrayList<Double>();
        final var dependentsBroken = new ArrayList<Double>();
        final var criticalityLost = new ArrayList<Double>();
        final var replicaPairs = new ArrayList<Double>();
        final var criticalBroken = new ArrayList<Double>();
        for (final Host host : hosts) {
            final List<Vm> onHost = new ArrayList<>(host.getVmList());
            if (onHost.isEmpty()) {
                continue;
            }

            active++;
            powerW += host.getPowerModel().getPower(Math.clamp(host.getCpuPercentUtilization(), 0.0, 1.0));
            final var unavailable = new ArrayList<Vm>(unplaced);
            unavailable.addAll(onHost);
            final Set<String> broken = new HashSet<>(graph.brokenServices(unavailable));
            broken.removeAll(brokenBefore);
            final Set<Vm> onHostSet = Collections.newSetFromMap(new IdentityHashMap<>());
            onHostSet.addAll(onHost);
            final long dependents = vms.stream()
                .filter(vm -> vm.isCreated() && !onHostSet.contains(vm) && broken.contains(graph.getService(vm)))
                .count();
            vmsLost.add((double) onHost.size());
            servicesBroken.add((double) broken.size());
            appsHit.add((double) broken.stream().map(serviceApp::get).distinct().count());
            dependentsBroken.add((double) dependents);
            criticalityLost.add(broken.stream().mapToDouble(criticality::get).sum() / totalCriticality);
            criticalBroken.add((double) broken.stream().filter(declaredCritical::contains).count());
            final Map<String, Long> perService = onHost.stream()
                .collect(Collectors.groupingBy(graph::getService, Collectors.counting()));
            replicaPairs.add((double) perService.values().stream().filter(c -> c > 1).count());
        }

        row.put("unplaced_vms", unplaced.size());
        row.put("services_down_no_failure", brokenBefore.size());
        row.put("active_hosts", active);
        row.put("power_kw", powerW / 1000);
        putStats(row, "e1_vms_lost", vmsLost);
        putStats(row, "e1_services_broken", servicesBroken);
        putStats(row, "e1_apps_hit", appsHit);
        putStats(row, "e1_dependent_vms_broken", dependentsBroken);
        putStats(row, "e1_crit_lost", criticalityLost);
        putStats(row, "e1_declared_critical_broken", criticalBroken);
        row.put("e1_services_broken_exposure", servicesBroken.stream().mapToDouble(Double::doubleValue).sum());
        row.put("e1_colocated_replica_services", replicaPairs.stream().mapToDouble(Double::doubleValue).sum());
        row.put("e2_predicted_vms_failed_1", hosts.get(failureOrder.getFirst()).getVmList().size());
    }

    /// Rack failures: every host of a rack that holds VMs fails at once.
    private static void rackFailures(final Map<String, Object> row, final List<Host> hosts, final List<Vm> vms,
                                     final VmDependencyGraph graph, final Map<String, Integer> serviceApp,
                                     final Map<String, Double> criticality, final Set<String> declaredCritical,
                                     final int rackSize) {
        final List<Vm> unplaced = vms.stream().filter(vm -> !vm.isCreated()).toList();
        final Set<String> brokenBefore = graph.brokenServices(unplaced);
        final double totalCriticality = criticality.values().stream().mapToDouble(Double::doubleValue).sum();
        final var servicesBroken = new ArrayList<Double>();
        final var criticalBroken = new ArrayList<Double>();
        final var appsHit = new ArrayList<Double>();
        final var criticalityLost = new ArrayList<Double>();
        final var vmsLost = new ArrayList<Double>();
        for (int start = 0; start < hosts.size(); start += rackSize) {
            final List<Vm> inRack = new ArrayList<>();
            for (int i = start; i < Math.min(hosts.size(), start + rackSize); i++) {
                inRack.addAll(hosts.get(i).getVmList());
            }

            if (inRack.isEmpty()) {
                continue;
            }

            final var unavailable = new ArrayList<Vm>(unplaced);
            unavailable.addAll(inRack);
            final Set<String> broken = new HashSet<>(graph.brokenServices(unavailable));
            broken.removeAll(brokenBefore);
            vmsLost.add((double) inRack.size());
            servicesBroken.add((double) broken.size());
            criticalBroken.add((double) broken.stream().filter(declaredCritical::contains).count());
            appsHit.add((double) broken.stream().map(serviceApp::get).distinct().count());
            criticalityLost.add(broken.stream().mapToDouble(criticality::get).sum() / totalCriticality);
        }

        row.put("racks_used", vmsLost.size());
        putStats(row, "r1_vms_lost", vmsLost);
        putStats(row, "r1_services_broken", servicesBroken);
        putStats(row, "r1_declared_critical_broken", criticalBroken);
        putStats(row, "r1_apps_hit", appsHit);
        putStats(row, "r1_crit_lost", criticalityLost);
    }

    private static void recordAfterFailure(final Map<String, Object> row, final int k, final List<Vm> vms,
                                           final VmDependencyGraph graph, final Map<String, Integer> serviceApp,
                                           final int apps) {
        final List<Vm> unavailable = vms.stream().filter(vm -> vm.isFailed() || !vm.isCreated()).toList();
        final Set<String> broken = graph.brokenServices(unavailable);
        final long appsBroken = broken.stream().map(serviceApp::get).distinct().count();
        row.put("e2_vms_failed_" + k, vms.stream().filter(Vm::isFailed).count());
        row.put("e2_services_broken_" + k, broken.size());
        row.put("e2_apps_intact_" + k, apps - appsBroken);
    }

    private static void putStats(final Map<String, Object> row, final String name, final List<Double> values) {
        row.put(name + "_mean", values.stream().mapToDouble(Double::doubleValue).average().orElse(0));
        row.put(name + "_max", values.stream().mapToDouble(Double::doubleValue).max().orElse(0));
    }

    private static String format(final Object value) {
        if (value instanceof Double d) {
            return String.format(Locale.ROOT, "%.6g", d);
        }

        return String.valueOf(value);
    }

    private static Map<String, String> parse(final String[] args) {
        final var opts = new HashMap<String, String>();
        for (int i = 0; i + 1 < args.length; i += 2) {
            if (!args[i].startsWith("--")) {
                throw new IllegalArgumentException("expected --name value pairs, got " + args[i]);
            }

            opts.put(args[i].substring(2), args[i + 1]);
        }

        return opts;
    }
}
