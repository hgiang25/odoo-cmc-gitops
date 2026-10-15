# ELB provisioning paused

The desired topology is clients -> HA firewalls -> private ELB -> APISIX -> Odoo.
Wait for the firewall interfaces and ELB subnet to be ready before provisioning
the private ELB. The committed APISIX gateway is currently `ClusterIP` with no
ELB annotations; neither a public nor a private ELB is requested.

Sync `odoo-cmc-root`, then `odoo-apisix`. Changing the existing gateway Service
from LoadBalancer to ClusterIP lets the cloud controller clean up its old ELB.
External access through the old public IP will stop. Odoo and its data remain.
Do not delete the namespace, PVCs or Argo CD Application to remove an ELB.

Check on the bastion after sync:

```bash
kubectl -n apisix get svc apisix-gateway -o yaml
kubectl -n apisix get events --field-selector involvedObject.kind=Service,involvedObject.name=apisix-gateway --sort-by=.lastTimestamp
kubectl get svc -A --field-selector spec.type=LoadBalancer
```

The gateway should be ClusterIP, with no external ingress, nodePort or cloud
load-balancer cleanup finalizer after cleanup completes. Also verify in CMC
that the old ELB associated with public IP 113.20.122.53 and its listeners,
pools and members are removed; check whether a separately allocated public IP
remains billable. A ClusterIP alone does not prove cloud cleanup completed.
If cleanup fails, inspect controller events/logs and the exact ELB ID before
removing any orphan. Never strip the cleanup finalizer to bypass an error.

`configure-cmc.yml` preserves the paused Service type. The example config's
private class is for future planning; `cmc.py render` only produces local
artifacts and may include a LoadBalancer. Do not apply those artifacts while
ELB creation is paused. Do not rerun a legacy direct Helm deployment.

Resume only after an explicit request: confirm the private subnet/network,
CMC private class and flavor, firewall routing/NAT, return path and security
rules. Then change the GitOps gateway to LoadBalancer with the validated
private annotations and verify that no public IP is allocated.
