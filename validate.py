"""Offline validation: render all overlays, check cross-resource/runtime constraints."""
import argparse
import ast
import configparser
import math
from pathlib import Path
import subprocess
import sys
import yaml

ROOT = Path(__file__).resolve().parent

def render(path):
    output = subprocess.run(["kubectl", "kustomize", str(path)], check=True,
                            text=True, capture_output=True)
    return [d for d in yaml.safe_load_all(output.stdout) if d]

def memory(value):
    for suffix, multiplier in (("Gi", 2**30), ("Mi", 2**20), ("Ki", 2**10)):
        if str(value).endswith(suffix):
            return float(str(value)[:-len(suffix)]) * multiplier
    return float(value)

def check(resources, local=False, odoo_image="odoo-queue:17.0-lab"):
    index = {(d["kind"], d["metadata"]["name"]): d for d in resources}
    assert len(index) == len(resources), "Duplicate resource identity"
    assert ("Deployment", "odoo-web") not in index, "Duplicate web owner"
    assert not any(d["kind"] == "Ingress" or d["metadata"]["name"] == "redis" for d in resources)
    config = configparser.ConfigParser(interpolation=None)
    config.read_string(index["ConfigMap", "odoo-config"]["data"]["odoo.conf"])
    options = config["options"]
    assert all(key not in options for key in ("db_password", "admin_passwd"))
    assert options.getboolean("list_db") is False
    web = index["Rollout", "odoo-web-rollout"]
    canary = web["spec"]["strategy"]["canary"]
    analysis_steps = [step["analysis"] for step in canary["steps"] if "analysis" in step]
    assert analysis_steps
    for analysis in analysis_steps:
        for template in analysis["templates"]:
            assert ("AnalysisTemplate", template["templateName"]) in index
    for service in (canary["stableService"], canary["canaryService"]):
        assert index["Service", service]["spec"]["selector"] == web["spec"]["selector"]["matchLabels"]
    route = index["ApisixRoute", canary["trafficRouting"]["apisix"]["route"]["name"]]
    route_rules = {r["name"]: r for r in route["spec"]["http"]}
    for rule_name in canary["trafficRouting"]["apisix"]["route"]["rules"]:
        rule = route_rules[rule_name]
        assert {b["serviceName"] for b in rule["backends"]} == {canary["stableService"], canary["canaryService"]}
        assert rule["timeout"]["read"] == "600s"
    assert route_rules["websocket"]["websocket"] is True
    assert {b["servicePort"] for b in route_rules["websocket"]["backends"]} == {8072}
    for doc in resources:
        if doc["kind"] not in ("Deployment", "Rollout") or doc["metadata"]["name"] == "postgres":
            continue
        pod = doc["spec"]["template"]["spec"]
        for key in ("runAsUser", "runAsGroup", "fsGroup"):
            assert pod["securityContext"][key] == 101
        for container in pod["containers"]:
            assert "command" not in container, "Preserve Odoo entrypoint"
            assert container["image"] == odoo_image
            args = container["args"]
            processes = 0
            if "--no-http" not in args:
                workers = int(next(v.split("=")[1] for v in args if v.startswith("--workers=")))
                processes += workers
            processes += int(next(v.split("=")[1] for v in args if v.startswith("--max-cron-threads=")))
            if doc["metadata"]["name"] == "odoo-queue-runner":
                processes += 1
            minimum = processes * options.getint("limit_memory_hard") + 512 * 2**20
            assert memory(container["resources"]["limits"]["memory"]) >= minimum, doc["metadata"]["name"]
            for mount in container.get("volumeMounts", []):
                assert any(v["name"] == mount["name"] for v in pod["volumes"])
            env = {item["name"]: item for item in container.get("env", [])}
            for name in ("HOST", "PORT", "USER", "PASSWORD"):
                if name in env:
                    assert env[name]["valueFrom"]["secretKeyRef"]["name"] == "odoo-db-connection"
    for scaler in [d for d in resources if d["kind"] == "ScaledObject"]:
        target = scaler["spec"]["scaleTargetRef"]
        assert (target.get("kind", "Deployment"), target["name"]) in index
    max_web = index["ScaledObject", "odoo-web-autoscaler"]["spec"]["maxReplicaCount"]
    max_queue = index["ScaledObject", "odoo-worker-autoscaler"]["spec"]["maxReplicaCount"]
    surge = math.ceil(max_web * int(canary["maxSurge"].rstrip("%")) / 100)
    # Master clears its inherited connection pool before worker processes fork.
    connection_budget = ((max_web + surge) * 3 + max_queue * 3 + 2 + 1) * options.getint("db_maxconn") + 32
    assert connection_budget < 300, f"Connection budget exceeds PostgreSQL: {connection_budget}"
    assert index["CronJob", "odoo-disaster-recovery-backup"]["spec"]["timeZone"] == "Asia/Ho_Chi_Minh"
    backup_volumes = index["CronJob", "odoo-disaster-recovery-backup"]["spec"]["jobTemplate"]["spec"]["template"]["spec"]["volumes"]
    assert next(v for v in backup_volumes if v["name"] == "backup")["persistentVolumeClaim"]["claimName"] == "odoo-backup-pvc"
    for name in ("odoo-filestore-pvc", "odoo-backup-pvc", "odoo-restore-pvc"):
        assert index["PersistentVolumeClaim", name]["spec"]["accessModes"] == (["ReadWriteOnce"] if local else ["ReadWriteMany"])
    print(f"Validated {len(resources)} resources; connection budget including reserve: {connection_budget}/300.")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--crd-schemas", type=Path, help="Directory containing official rendered CRDs")
    args = parser.parse_args()
    sets = [render(ROOT), render(ROOT / "overlays/local"), render(ROOT / "overlays/http-scaling")]
    for docs, local in zip(sets, (False, True, False)):
        check(docs, local)
    cmc_docs = render(ROOT / "overlays/cmc")
    cmc_rollout = next(d for d in cmc_docs if d["kind"] == "Rollout"
                       and d["metadata"]["name"] == "odoo-web-rollout")
    cmc_odoo_image = next(c["image"] for c in cmc_rollout["spec"]["template"]["spec"]["containers"]
                          if c["name"] == "odoo-web")
    assert cmc_odoo_image.startswith("docker.io/") and not cmc_odoo_image.endswith(":latest")
    check(cmc_docs, odoo_image=cmc_odoo_image)
    cmc_index = {(d["kind"], d["metadata"]["name"]): d for d in cmc_docs}
    assert ("StatefulSet", "postgres") not in cmc_index
    assert ("PersistentVolumeClaim", "postgres-pvc") not in cmc_index
    assert not any(kind == "EndpointSlice" for kind, _ in cmc_index)
    assert ("Service", "postgres-service") not in cmc_index
    for resource in cmc_docs:
        pod = None
        if resource["kind"] in ("Deployment", "Rollout", "Job"):
            pod = resource["spec"]["template"]["spec"]
        elif resource["kind"] == "CronJob":
            pod = resource["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        if pod and any("odoo-" in container["image"] for container in
                       pod.get("initContainers", []) + pod.get("containers", [])):
            assert pod["imagePullSecrets"] == [{"name": "odoo-registry"}]
    auth = cmc_index["TriggerAuthentication", "odoo-postgresql-auth"]
    assert {item["parameter"] for item in auth["spec"]["secretTargetRef"]} == {
        "host", "port", "dbName", "sslmode", "userName", "password"}
    runtime = cmc_index["ConfigMap", "odoo-runtime-settings"]["data"]
    assert runtime["s3_required"] == "false"
    backup = cmc_index["CronJob", "odoo-disaster-recovery-backup"]
    backup_env = {item["name"]: item for item in
                  backup["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert backup_env["S3_BUCKET"]["valueFrom"]["configMapKeyRef"]["name"] == "odoo-runtime-settings"
    assert backup_env["AWS_ACCESS_KEY_ID"]["valueFrom"]["secretKeyRef"]["name"] == "odoo-s3-credentials"
    sets.append(cmc_docs)
    infrastructure = render(ROOT / "overlays/cmc-infrastructure")
    etcd = next(d for d in infrastructure if d["kind"] == "StatefulSet"
                and d["metadata"]["name"] == "apisix-etcd")
    assert etcd["spec"]["replicas"] == 3
    etcd_container = etcd["spec"]["template"]["spec"]["containers"][0]
    assert etcd_container["command"] == ["/usr/local/bin/etcd"]
    assert etcd_container["env"] == [{"name": "POD_NAME", "valueFrom": {
        "fieldRef": {"fieldPath": "metadata.name"}}}]
    assert "--name=$(POD_NAME)" in etcd_container["args"]
    assert etcd_container["readinessProbe"]["httpGet"] == {
        "path": "/readyz?exclude=linearizable_read", "port": "client"}
    assert etcd_container["livenessProbe"]["httpGet"] == {
        "path": "/livez", "port": "client"}
    assert any(d["kind"] == "PodDisruptionBudget" and d["metadata"]["name"] == "apisix-etcd"
               for d in infrastructure)
    applications = render(ROOT / "clusters/cmc")
    assert {d["metadata"]["name"] for d in applications} == {
        "odoo-cmc-infrastructure", "odoo-argo-rollouts", "odoo-keda",
        "odoo-apisix", "odoo-cmc"}
    assert all(d["spec"]["syncPolicy"]["automated"]["selfHeal"] for d in applications)
    preflight = yaml.safe_load((ROOT / "bootstrap/odoo-dbaas-preflight.yaml").read_text(encoding="utf-8"))
    assert preflight["spec"]["template"]["spec"]["containers"][0]["image"] == "postgres:16-alpine"
    nfs_preflight = list(yaml.safe_load_all(
        (ROOT / "bootstrap/odoo-nfs-preflight.yaml").read_text(encoding="utf-8")))
    nfs_pvc = next(item for item in nfs_preflight if item["kind"] == "PersistentVolumeClaim")
    nfs_job = next(item for item in nfs_preflight if item["kind"] == "Job")
    assert nfs_pvc["metadata"]["name"] == "odoo-filestore-pvc"
    assert nfs_pvc["spec"]["accessModes"] == ["ReadWriteMany"]
    pod_spec = nfs_job["spec"]["template"]["spec"]
    assert pod_spec["securityContext"]["runAsUser"] == 101
    assert pod_spec["securityContext"]["runAsGroup"] == 101
    assert pod_spec["securityContext"]["fsGroup"] == 101
    from cmc import artifacts, load_config
    cloud = artifacts(load_config(ROOT / 'cmc/config.example.yaml'))
    sets.extend([cloud['app.yaml'], cloud['infrastructure.yaml']])
    for path in ROOT.rglob("*.py"):
        if ".local" not in path.parts:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    if args.crd_schemas:
        import jsonschema
        schemas = {}
        for path in args.crd_schemas.glob("*.yaml"):
            for crd in yaml.safe_load_all(path.read_text(encoding="utf-8")):
                if crd and crd.get("kind") == "CustomResourceDefinition":
                    for version in crd["spec"]["versions"]:
                        schemas[(crd["spec"]["group"] + "/" + version["name"], crd["spec"]["names"]["kind"])] = version["schema"]["openAPIV3Schema"]
        for docs in sets:
            for doc in docs:
                if doc["apiVersion"].split("/")[0] in ("apisix.apache.org", "argoproj.io", "keda.sh"):
                    key = (doc["apiVersion"], doc["kind"])
                    assert key in schemas, f"Missing official schema: {key}"
                    jsonschema.validate(doc, schemas[key])
        print("All custom resources passed official CRD schema validation.")
    print("Python syntax and cross-resource checks passed.")

if __name__ == "__main__":
    main()
