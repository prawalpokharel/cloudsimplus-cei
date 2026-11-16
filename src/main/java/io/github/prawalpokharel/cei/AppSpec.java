package io.github.prawalpokharel.cei;

import java.util.List;

/// A generated application: services, their dependencies and the entry service.
/// It is plain data, so each placement policy builds its own VMs from the same spec.
///
/// @param id the application number
/// @param shape the graph family the application was generated from
/// @param services the services of the application
/// @param edges dependencies as `{caller, callee}` service indexes
/// @param entry the index of the service that receives the requests
public record AppSpec(int id, String shape, List<ServiceSpec> services, List<int[]> edges, int entry) {
    /// A service of an application.
    ///
    /// @param name the service name, unique inside the application
    /// @param replicas how many VMs run the service
    /// @param cores the cores of each replica VM
    /// @param declared the criticality declared by the operator, in \[0, 1\]
    /// @param history the demand history used for the entropy term
    /// @param meanUtilization the CPU utilization of each replica during the run
    public record ServiceSpec(String name, int replicas, int cores, double declared,
                              double[] history, double meanUtilization) {}

    /// {@return the name used for a service in the dependency graph, unique across applications}
    public String qualifiedName(final int service) {
        return "app" + id + "/" + services.get(service).name();
    }

    /// {@return the total number of cores the application VMs need}
    public int totalCores() {
        return services.stream().mapToInt(s -> s.replicas() * s.cores()).sum();
    }
}
