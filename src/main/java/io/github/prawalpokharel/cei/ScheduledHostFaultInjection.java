package io.github.prawalpokharel.cei;

import org.cloudsimplus.datacenters.Datacenter;
import org.cloudsimplus.distributions.UniformDistr;
import org.cloudsimplus.faultinjection.HostFaultInjection;

/// A [HostFaultInjection] that injects no random failures by itself.
/// The experiment calls [#generateHostFault(org.cloudsimplus.hosts.Host)] at fixed
/// times on a host order shared by every policy, so the runs stay paired.
/// Everything else (failing the host cores, failing and destroying its VMs,
/// counting faults) is the unchanged CloudSim Plus behaviour.
final class ScheduledHostFaultInjection extends HostFaultInjection {
    ScheduledHostFaultInjection(final Datacenter datacenter, final long seed) {
        super(datacenter, new UniformDistr(seed));
    }

    @Override
    protected void startInternal() {
        // No random failure schedule: failures are injected by the experiment.
    }
}
