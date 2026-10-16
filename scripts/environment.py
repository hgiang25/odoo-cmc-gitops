"""Read-only environment preflight and bootstrap Job renderer; never applies workloads."""
import argparse
import copy
import json
from pathlib import Path
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
KEY = 'odoo.cmc/environment'


def placement(environment):
    return {
        'tolerations': [{'key': KEY, 'operator': 'Equal', 'value': environment, 'effect': 'NoSchedule'}],
        'affinity': {'nodeAffinity': {'requiredDuringSchedulingIgnoredDuringExecution': {
            'nodeSelectorTerms': [{'matchExpressions': [
                {'key': KEY, 'operator': 'In', 'values': [environment]}]}]}}},
    }


def eligible_nodes(nodes, environment):
    """Respect every blocking taint, Ready and cordon, not just the environment label."""
    result = []
    for node in nodes:
        spec = node.get('spec', {})
        if node['metadata'].get('labels', {}).get(KEY) != environment or spec.get('unschedulable'):
            continue
        if not any(c['type'] == 'Ready' and c['status'] == 'True'
                   for c in node.get('status', {}).get('conditions', [])):
            continue
        expected = {'key': KEY, 'value': environment, 'effect': 'NoSchedule'}
        taints = spec.get('taints', [])
        if expected not in taints:
            continue
        if any(t.get('effect') in ('NoSchedule', 'NoExecute') and t != expected for t in taints):
            continue
        result.append(node)
    return result


def bootstrap_resources(environment, kind, storage_class='nfs-csi'):
    filename = {'nfs': 'bootstrap/odoo-nfs-preflight.yaml',
                'db': 'bootstrap/odoo-dbaas-preflight.yaml',
                'restore': 'dr/restore-job.yaml'}[kind]
    resources = list(yaml.safe_load_all((ROOT / filename).read_text(encoding='utf-8')))
    for doc in resources:
        doc['metadata']['namespace'] = 'odoo-' + environment
        if doc['kind'] == 'PersistentVolumeClaim':
            doc['spec']['storageClassName'] = storage_class
        if doc['kind'] != 'Job':
            continue
        pod = doc['spec']['template']['spec']
        pod.update(copy.deepcopy(placement(environment)))
        doc['spec']['template'].setdefault('metadata', {}).setdefault('labels', {})[KEY] = environment
        if kind == 'restore':
            overlay = yaml.safe_load((ROOT / f'overlays/{environment}/kustomization.yaml').read_text(encoding='utf-8'))
            image = next(i for i in overlay['images'] if i['name'] == 'odoo-backup')
            pod['containers'][0]['image'] = image['newName'] + ':' + image['newTag']
            pod['imagePullSecrets'] = [{'name': 'odoo-registry'}]
            for variable in pod['containers'][0]['env']:
                if variable['name'] == 'PGDATABASE':
                    variable.pop('value', None)
                    variable['valueFrom'] = {'secretKeyRef': {'name': 'odoo-db-connection', 'key': 'DATABASE'}}
        if kind == 'db':
            container = pod['containers'][0]
            container['env'] += [
                {'name': 'PGDATABASE', 'valueFrom': {'secretKeyRef': {
                    'name': 'odoo-db-connection', 'key': 'DATABASE'}}},
                {'name': 'PGCONNECT_TIMEOUT', 'value': '10'},
                {'name': 'EXPECTED_DB', 'value': 'odoo17_' + environment},
                {'name': 'OTHER_DB', 'value': 'odoo17_' + ('prod' if environment == 'dev' else 'dev')},
            ]
            container['command'] = ['/bin/sh', '-ec', '''
test "$PGDATABASE" = "$EXPECTED_DB"
version="$(psql -X -At -v ON_ERROR_STOP=1 -c 'SHOW server_version_num')"
case "$version" in 16*) ;; *) echo 'Expected PostgreSQL 16'; exit 1;; esac
connections="$(psql -X -At -v ON_ERROR_STOP=1 -c 'SHOW max_connections')"
test "$connections" -ge 300
unsafe="$(psql -X -At -v ON_ERROR_STOP=1 -c "SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls FROM pg_roles WHERE rolname = current_user")"
test "$unsafe" = f
# Fail if peer DB is absent too: both databases must be provisioned before bootstrap.
cross_access="$(psql -X -At -v ON_ERROR_STOP=1 -c "SELECT has_database_privilege(current_user, '$OTHER_DB', 'CONNECT')")"
test "$cross_access" = f
# Check the management DB used by the pinned init helper and transactional schema DDL.
psql -X -At -v ON_ERROR_STOP=1 -d postgres -c 'SELECT 1' >/dev/null
psql -X -v ON_ERROR_STOP=1 <<'SQL'
BEGIN;
CREATE TABLE public.odoo_bootstrap_privilege_probe (id integer);
DROP TABLE public.odoo_bootstrap_privilege_probe;
ROLLBACK;
SQL
echo 'PostgreSQL 16, database identity, connection capacity and cross-environment CONNECT denial verified.'
'''.strip()]
    return resources


def check_cluster(context, environment):
    if not context or context == 'docker-desktop':
        raise ValueError('Specify an explicit CMC context')
    def get(*args):
        result = subprocess.run(['kubectl', '--context', context, '--request-timeout=10s',
                                 *args, '-o', 'json'], capture_output=True, text=True,
                                check=True, timeout=45)
        return json.loads(result.stdout)
    nodes = get('get', 'nodes')['items']
    required = 2 if environment == 'prod' else 1
    eligible = eligible_nodes(nodes, environment)
    if len(eligible) < required:
        raise ValueError(f'{environment}: need {required} Ready nodes with matching label and NoSchedule taint')
    print(f'{environment}: {len(eligible)} eligible nodes')
    metrics = get('get', 'apiservice', 'v1beta1.metrics.k8s.io')
    if not any(c['type'] == 'Available' and c['status'] == 'True'
               for c in metrics.get('status', {}).get('conditions', [])):
        raise ValueError('Metrics API must be Available before enabling CPU/memory autoscaling')
    ns = 'odoo-' + environment
    # Existing pods on the wrong node are not moved by NoSchedule or IgnoredDuringExecution.
    pods = get('get', 'pods', '-n', ns)['items']
    by_name = {n['metadata']['name']: n for n in nodes}
    for pod in pods:
        node_name = pod.get('spec', {}).get('nodeName')
        if node_name and pod.get('status', {}).get('phase') not in ('Succeeded', 'Failed'):
            if by_name[node_name]['metadata'].get('labels', {}).get(KEY) != environment:
                raise ValueError(f'{ns}/{pod["metadata"]["name"]} runs outside its node group')
    print('Existing running pods are on the correct environment nodes.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['render-bootstrap', 'preflight'])
    parser.add_argument('--environment', required=True, choices=['dev', 'prod'])
    parser.add_argument('--kind', choices=['db', 'nfs', 'restore'], default='db')
    parser.add_argument('--storage-class', default='nfs-csi')
    parser.add_argument('--context')
    args = parser.parse_args()
    if args.action == 'preflight':
        check_cluster(args.context, args.environment)
    else:
        print(yaml.safe_dump_all(bootstrap_resources(args.environment, args.kind, args.storage_class),
                                 sort_keys=False))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, subprocess.SubprocessError) as exc:
        sys.exit(str(exc))
