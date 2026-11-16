package io.github.prawalpokharel.cei;

import io.github.prawalpokharel.cei.AppSpec.ServiceSpec;

import java.util.ArrayList;
import java.util.List;
import java.util.Random;

/// Generates applications whose services form dependency graphs of a few shapes,
/// until their VMs need a target share of the datacenter cores.
///
/// Shapes:
/// - `chain`: a 6-service pipeline;
/// - `fanin`: a gateway calling 5 services that all use one shared database (a hub callee);
/// - `tree`: a 13-service fan-out tree (1, 3, 9);
/// - `dsb`: the 12-service social network of DeathStarBench (Gan et al., ASPLOS 2019),
///   with the calls of its compose-post request;
/// - `scalefree`: 11 services where each new service calls 1 or 2 earlier ones,
///   chosen in proportion to how many callers they already have, plus a gateway;
/// - `mixed`: each application picks one of the shapes above at random.
///
/// Replicas: `1` gives every service one VM, `2` gives every service two,
/// and `mixed` gives two VMs to services declared critical or called by two or more
/// services, and one VM to the others (an operator replicating what matters).
public final class AppGenerator {
    static final List<String> SHAPES = List.of("chain", "fanin", "tree", "dsb", "scalefree");
    private static final int HISTORY_LENGTH = 288;

    private final Random rnd;
    private final String replicaMode;
    private final long seed;

    /// Reads applications in the text format written by experiments/alibaba_extract.py
    /// (`app`, `svc name replicas cores declared h1,h2,...`, `edge i j`, `end`) and draws them,
    /// in a seeded random order, until the next one that fits would exceed `load` of the cluster cores.
    /// Replica counts come from the file. The entering service is the first service of each application.
    public static List<AppSpec> fromFile(final java.nio.file.Path file, final double load, final int totalCores,
                                         final long seed) throws java.io.IOException {
        final var pool = new ArrayList<List<ServiceSpec>>();
        final var poolEdges = new ArrayList<List<int[]>>();
        List<ServiceSpec> services = new ArrayList<>();
        List<int[]> edges = new ArrayList<>();
        for (final String line : java.nio.file.Files.readAllLines(file)) {
            final String[] f = line.trim().split(" ");
            switch (f[0]) {
                case "app" -> {
                    services = new ArrayList<>();
                    edges = new ArrayList<>();
                }
                case "svc" -> {
                    final double[] history = f.length > 5 && !f[5].isEmpty()
                        ? java.util.Arrays.stream(f[5].split(",")).mapToDouble(Double::parseDouble).toArray()
                        : new double[0];
                    final double mean = java.util.Arrays.stream(history).average().orElse(0.5);
                    services.add(new ServiceSpec(f[1], Integer.parseInt(f[2]), Integer.parseInt(f[3]),
                        Double.parseDouble(f[4]), history, 0.2 + 0.6 * mean));
                }
                case "edge" -> edges.add(new int[]{Integer.parseInt(f[1]), Integer.parseInt(f[2])});
                case "end" -> {
                    pool.add(services);
                    poolEdges.add(edges);
                }
                default -> { }
            }
        }

        final var order = new ArrayList<Integer>();
        for (int i = 0; i < pool.size(); i++) {
            order.add(i);
        }

        java.util.Collections.shuffle(order, new Random(seed));
        final double capacity = load * totalCores;
        final var apps = new ArrayList<AppSpec>();
        int used = 0;
        for (final int i : order) {
            final var app = new AppSpec(apps.size(), "alibaba", pool.get(i), poolEdges.get(i), 0);
            if (used + app.totalCores() <= capacity) {
                apps.add(app);
                used += app.totalCores();
            }
        }

        return apps;
    }

    private AppGenerator(final long seed, final String replicaMode) {
        this.rnd = new Random(seed);
        this.replicaMode = replicaMode;
        this.seed = seed;
    }

    /// Generates applications until the next one would exceed `load` of the cluster cores.
    public static List<AppSpec> generate(final String shape, final double load, final int totalCores,
                                         final String replicaMode, final long seed) {
        final var generator = new AppGenerator(seed, replicaMode);
        final double capacity = load * totalCores;
        final var apps = new ArrayList<AppSpec>();
        int used = 0;
        while (true) {
            final String appShape = "mixed".equals(shape) ? SHAPES.get(generator.rnd.nextInt(SHAPES.size())) : shape;
            final AppSpec app = generator.app(apps.size(), appShape);
            if (used + app.totalCores() > capacity && !apps.isEmpty()) {
                return apps;
            }

            apps.add(app);
            used += app.totalCores();
        }
    }

    private AppSpec app(final int id, final String shape) {
        final List<String> names = new ArrayList<>();
        final List<int[]> edges = new ArrayList<>();
        final List<Integer> critical = new ArrayList<>();
        switch (shape) {
            case "chain" -> chain(names, edges);
            case "fanin" -> fanIn(names, edges);
            case "tree" -> tree(names, edges);
            case "dsb" -> deathStarBench(names, edges, critical);
            case "scalefree" -> scaleFree(names, edges);
            default -> throw new IllegalArgumentException("unknown shape " + shape);
        }

        final var callers = new int[names.size()];
        edges.forEach(e -> callers[e[1]]++);
        final var services = new ArrayList<ServiceSpec>(names.size());
        for (int s = 0; s < names.size(); s++) {
            final boolean declaredCritical = "dsb".equals(shape) ? critical.contains(s) : s == 0 || rnd.nextDouble() < 0.2;
            final double declared = declaredCritical ? 1.0 : 0.2;
            final int replicas = switch (replicaMode) {
                case "1" -> 1;
                case "2" -> 2;
                case "mixed" -> declaredCritical || callers[s] >= 2 ? 2 : 1;
                /* "p0.25": each service has two replicas with probability 0.25. The draw uses its own
                 * generator so the other draws (and so the other modes) are unchanged. */
                default -> {
                    if (!replicaMode.startsWith("p")) {
                        throw new IllegalArgumentException("unknown replica mode " + replicaMode);
                    }

                    final double share = Double.parseDouble(replicaMode.substring(1));
                    yield new Random(seed * 1_000_003L + id * 1_009L + s).nextDouble() < share ? 2 : 1;
                }
            };
            final double[] history = history();
            final double mean = java.util.Arrays.stream(history).average().orElse(0.5);
            services.add(new ServiceSpec(names.get(s), replicas, cores(), declared, history, mean));
        }

        return new AppSpec(id, shape, services, edges, 0);
    }

    private int cores() {
        final double u = rnd.nextDouble();
        return u < 0.4 ? 1 : u < 0.8 ? 2 : 4;
    }

    /// A day of 5-minute demand samples: a base level, a daily wave and noise, clipped to \[0, 1\].
    private double[] history() {
        final double base = 0.2 + 0.5 * rnd.nextDouble();
        final double amplitude = 0.3 * rnd.nextDouble();
        final double noise = 0.01 + 0.14 * rnd.nextDouble();
        final double phase = 2 * Math.PI * rnd.nextDouble();
        final var history = new double[HISTORY_LENGTH];
        for (int t = 0; t < HISTORY_LENGTH; t++) {
            final double value = base + amplitude * Math.sin(2 * Math.PI * t / HISTORY_LENGTH + phase) + noise * rnd.nextGaussian();
            history[t] = Math.clamp(value, 0.0, 1.0);
        }

        return history;
    }

    private static void chain(final List<String> names, final List<int[]> edges) {
        for (int i = 0; i < 6; i++) {
            names.add("stage" + i);
            if (i > 0) {
                edges.add(new int[]{i - 1, i});
            }
        }
    }

    private static void fanIn(final List<String> names, final List<int[]> edges) {
        names.add("gateway");
        for (int i = 1; i <= 5; i++) {
            names.add("svc" + i);
            edges.add(new int[]{0, i});
            edges.add(new int[]{i, 6});
        }

        names.add("db");
        names.add("cache");
        edges.add(new int[]{1, 7});
        edges.add(new int[]{3, 7});
    }

    private static void tree(final List<String> names, final List<int[]> edges) {
        names.add("root");
        for (int m = 1; m <= 3; m++) {
            names.add("mid" + m);
            edges.add(new int[]{0, m});
        }

        for (int m = 1; m <= 3; m++) {
            for (int l = 0; l < 3; l++) {
                names.add("leaf" + m + "_" + l);
                edges.add(new int[]{m, names.size() - 1});
            }
        }
    }

    /// The DeathStarBench social network services and the calls of its compose-post request.
    private static void deathStarBench(final List<String> names, final List<int[]> edges, final List<Integer> critical) {
        names.addAll(List.of(
            "nginx-web-server", "compose-post-service", "text-service", "media-service", "unique-id-service",
            "user-service", "user-timeline-service", "post-storage-service", "home-timeline-service",
            "url-shorten-service", "user-mention-service", "social-graph-service"));
        final int[][] calls = {
            {0, 1}, {0, 8}, {0, 6}, {0, 11}, {0, 5},
            {1, 2}, {1, 3}, {1, 4}, {1, 5}, {1, 6}, {1, 7}, {1, 8},
            {2, 9}, {2, 10}, {8, 11}, {6, 7}, {11, 5}};
        edges.addAll(List.of(calls));
        critical.addAll(List.of(0, 1, 5, 7, 11));
    }

    private void scaleFree(final List<String> names, final List<int[]> edges) {
        names.add("gateway");
        final int n = 11;
        final var callerCount = new int[n + 1];
        for (int i = 1; i <= n; i++) {
            names.add("svc" + i);
            if (i == 1) {
                continue;
            }

            final int calls = Math.min(i - 1, 1 + rnd.nextInt(2));
            final var chosen = new ArrayList<Integer>();
            while (chosen.size() < calls) {
                final int callee = pickByCallers(callerCount, i, chosen);
                chosen.add(callee);
                edges.add(new int[]{i, callee});
                callerCount[callee]++;
            }
        }

        for (int i = 1; i <= n; i++) {
            final int svc = i;
            if (edges.stream().noneMatch(e -> e[1] == svc)) {
                edges.add(new int[]{0, svc});
            }
        }
    }

    private int pickByCallers(final int[] callerCount, final int limit, final List<Integer> exclude) {
        double total = 0;
        for (int j = 1; j < limit; j++) {
            if (!exclude.contains(j)) {
                total += callerCount[j] + 1;
            }
        }

        double r = rnd.nextDouble() * total;
        for (int j = 1; j < limit; j++) {
            if (exclude.contains(j)) {
                continue;
            }

            r -= callerCount[j] + 1;
            if (r <= 0) {
                return j;
            }
        }

        for (int j = limit - 1; j >= 1; j--) {
            if (!exclude.contains(j)) {
                return j;
            }
        }

        throw new IllegalStateException("no callee to pick");
    }
}
