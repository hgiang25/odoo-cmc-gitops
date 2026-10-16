"""Update non-secret CMC desired state from validated GitHub Variables."""
import argparse
from pathlib import Path
import re

import yaml


ROOT = Path(__file__).resolve().parents[1]
DNS = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]*")
K8S_NAME = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,251}[a-z0-9])?")
FLAVOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


def require(pattern, value, name):
    if not pattern.fullmatch(value):
        raise ValueError(f"Invalid {name}")


def literal_value(generator, key):
    prefix = key + "="
    return next(item[len(prefix):] for item in generator["literals"] if item.startswith(prefix))


def set_literal(generator, key, value):
    prefix = key + "="
    generator["literals"] = [prefix + value if item.startswith(prefix) else item
                             for item in generator["literals"]]


def configure(domain, rwx_class, block_class, elb_flavor, root=ROOT):
    require(DNS, domain, "domain")
    require(K8S_NAME, rwx_class, "RWX StorageClass")
    require(K8S_NAME, block_class, "block StorageClass")
    require(FLAVOR, elb_flavor, "ELB flavor ID")

    app_path = root / "overlays/cmc-common/kustomization.yaml"
    app = yaml.safe_load(app_path.read_text(encoding="utf-8"))
    deployment = next(item for item in app["configMapGenerator"]
                      if item["name"] == "cmc-deployment-settings")
    previous_domain = literal_value(deployment, "domain")
    set_literal(deployment, "domain", domain)
    for patch in app["patches"]:
        target = patch.get("target", {})
        if target.get("kind") == "PersistentVolumeClaim" and "storageClassName" in patch.get("patch", ""):
            operations = yaml.safe_load(patch["patch"])
            next(op for op in operations if op["path"] == "/spec/storageClassName")["value"] = rwx_class
            patch["patch"] = yaml.safe_dump(operations, sort_keys=False).rstrip()
        elif previous_domain and previous_domain in patch.get("patch", ""):
            patch["patch"] = patch["patch"].replace(previous_domain, domain)
    app_path.write_text(yaml.safe_dump(app, sort_keys=False), encoding="utf-8")

    resources_path = root / "overlays/cmc-common/cloud-resources.yaml"
    resources = list(yaml.safe_load_all(resources_path.read_text(encoding="utf-8")))
    tls = next(item for item in resources if item.get("kind") == "ApisixTls")
    tls["spec"]["hosts"] = [domain]
    resources_path.write_text(yaml.safe_dump_all(resources, sort_keys=False), encoding="utf-8")

    infra_path = root / "overlays/cmc-infrastructure/kustomization.yaml"
    infra = yaml.safe_load(infra_path.read_text(encoding="utf-8"))
    operations = yaml.safe_load(infra["patches"][0]["patch"])
    next(op for op in operations if op["path"].endswith("/storageClassName"))["value"] = block_class
    infra["patches"][0]["patch"] = yaml.safe_dump(operations, sort_keys=False).rstrip()
    infra_path.write_text(yaml.safe_dump(infra, sort_keys=False), encoding="utf-8")

    apisix_path = root / "clusters/cmc/platform/apisix.yaml"
    apisix = yaml.safe_load(apisix_path.read_text(encoding="utf-8"))
    service = apisix["spec"]["source"]["helm"]["valuesObject"]["service"]
    # This cluster has private-only ingress. Publishing the source repository leaves
    # the Service at ClusterIP; only a validated Configure run enables the ELB.
    service["type"] = "LoadBalancer"
    service["externalTrafficPolicy"] = "Cluster"
    service["annotations"] = {
        "loadbalancer.openstack.org/class": "cmc-loadbalancer-private",
        "loadbalancer.openstack.org/flavor-id": elb_flavor,
    }
    apisix_path.write_text(yaml.safe_dump(apisix, sort_keys=False), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--rwx-storage-class", required=True)
    parser.add_argument("--block-storage-class", required=True)
    parser.add_argument("--elb-flavor-id", required=True)
    args = parser.parse_args()
    configure(args.domain, args.rwx_storage_class, args.block_storage_class, args.elb_flavor_id)


if __name__ == "__main__":
    main()
