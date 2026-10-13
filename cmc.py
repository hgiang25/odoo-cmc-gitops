"""CMC manifest renderer and read-only preflight for the Argo CD deployment."""
import argparse
import copy
import json
from pathlib import Path
import re
import subprocess
import sys
import yaml

from validate import check, render

ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT / '.local/cmc-rendered'
CHARTS = (
    ('argo-rollouts', 'https://argoproj.github.io/argo-helm', '2.43.6'),
    ('keda', 'https://kedacore.github.io/charts', '2.18.0'),
)


def run(args, data=None):
    result = subprocess.run(args, input=data, text=True, encoding='utf-8',
                            capture_output=True, check=False)
    if result.returncode:
        # Never echo stdin: it may contain generated Secrets.
        raise RuntimeError(f"{args[0]} failed: {result.stderr.strip()}")
    return result.stdout


def load_config(path, live=False):
    config = yaml.safe_load(Path(path).read_text(encoding='utf-8'))
    required = {'context', 'domain', 'kubernetesMinor', 'images', 'imagePullSecrets',
                'storage', 'tlsSecret', 'loadBalancerAnnotations', 'backupScheduleEnabled'}
    if not isinstance(config, dict) or set(config) != required:
        raise ValueError(f'Config keys must be exactly: {sorted(required)}')
    if not isinstance(config['context'], str) or not config['context'].strip():
        raise ValueError('context is required')
    if config['context'] == 'docker-desktop':
        raise ValueError('CMC commands refuse docker-desktop; use deploy.ps1 for local')
    if config['kubernetesMinor'] not in ('1.31', '1.32', '1.33'):
        raise ValueError('Pinned KEDA 2.18 is tested on Kubernetes 1.31-1.33 only')
    domain = config['domain']
    if not isinstance(domain, str) or len(domain) > 253 or not re.fullmatch(
            r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]*', domain):
        raise ValueError('domain must be a lowercase DNS hostname without scheme/port/path')
    if domain.endswith('.local'):
        raise ValueError('Use a cloud DNS domain, not .local')
    if set(config['images']) != {'odoo', 'backup'}:
        raise ValueError('images must contain odoo and backup')
    for image in config['images'].values():
        if not isinstance(image, str) or not re.fullmatch(r'[a-z0-9][a-z0-9._:/@-]+', image):
            raise ValueError('Invalid image reference')
        if '/' not in image or (':' not in image.rsplit('/', 1)[-1]):
            raise ValueError('Use a registry repository and an explicit immutable tag or digest')
        if image.endswith((':latest', ':17.0-lab', ':15-lab')):
            raise ValueError('Cloud images need a release tag or digest, not latest/local lab tags')
    if set(config['storage']) != {'blockClass', 'sharedClass'}:
        raise ValueError('storage must specify blockClass and sharedClass')
    if not isinstance(config['imagePullSecrets'], list):
        raise ValueError('imagePullSecrets must be a list (empty for public Docker Hub)')
    for name in [config['tlsSecret'], *config['imagePullSecrets'], *config['storage'].values()]:
        if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]*', name):
            raise ValueError('Invalid Secret or StorageClass name')
    if type(config['backupScheduleEnabled']) is not bool:
        raise ValueError('backupScheduleEnabled must be boolean')
    annotations = config['loadBalancerAnnotations']
    if not isinstance(annotations, dict) or not all(isinstance(v, str) for v in annotations.values()):
        raise ValueError('LoadBalancer annotations must be a string mapping')
    if annotations.get('loadbalancer.openstack.org/class') not in (
            'cmc-loadbalancer-public', 'cmc-loadbalancer-private'):
        raise ValueError('Select a CMC public/private LoadBalancer class')
    if not annotations.get('loadbalancer.openstack.org/flavor-id'):
        raise ValueError('Select a region-specific CMC ELB flavor ID')
    if live and re.search(r'replace|example\.com|your_dockerhub', yaml.safe_dump(config), re.I):
        raise ValueError('Fill all example placeholders before contacting CMC')
    return config


def pod_spec(doc):
    if doc['kind'] == 'CronJob':
        return doc['spec']['jobTemplate']['spec']['template']['spec']
    if doc['kind'] in ('Deployment', 'StatefulSet', 'Rollout', 'Job'):
        return doc['spec']['template']['spec']
    return None


def transform(docs, config):
    docs = copy.deepcopy(docs)
    for doc in docs:
        pod = pod_spec(doc)
        if pod is not None:
            custom = False
            for container in pod.get('initContainers', []) + pod.get('containers', []):
                for key, old in (('odoo', 'odoo-queue:17.0-lab'), ('backup', 'odoo-backup:15-lab')):
                    if container['image'] == old:
                        container['image'] = config['images'][key]
                        container['imagePullPolicy'] = 'IfNotPresent'
                        custom = True
            if custom:
                pod['imagePullSecrets'] = [{'name': name} for name in config['imagePullSecrets']]
            if doc['kind'] == 'Rollout':
                pod['topologySpreadConstraints'] = [{
                    'maxSkew': 1, 'topologyKey': 'kubernetes.io/hostname',
                    'whenUnsatisfiable': 'ScheduleAnyway',
                    'labelSelector': {'matchLabels': {'app': 'odoo', 'component': 'web'}}}]
        if doc['kind'] == 'PersistentVolumeClaim':
            key = 'blockClass' if doc['metadata']['name'] == 'postgres-pvc' else 'sharedClass'
            doc['spec']['storageClassName'] = config['storage'][key]
        if doc['kind'] == 'CronJob':
            doc['spec']['suspend'] = not config['backupScheduleEnabled']
        if doc['kind'] == 'ApisixRoute':
            for rule in doc['spec']['http']:
                rule['match']['hosts'] = [config['domain']]
                rule['plugins'].append({'name': 'proxy-rewrite', 'enable': True, 'config': {
                    'headers': {'set': {'X-Forwarded-Host': config['domain'],
                                        'X-Forwarded-Proto': 'https'}}}})
    return docs


def resource(kind, name, spec, namespace='odoo-system', api='networking.k8s.io/v1'):
    return {'apiVersion': api, 'kind': kind,
            'metadata': {'name': name, 'namespace': namespace}, 'spec': spec}


def peer(namespace, labels=None):
    value = {'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': namespace}}}
    if labels is not None:
        value['podSelector'] = {'matchLabels': labels}
    return value


def policies():
    def rule(peers, ports):
        return {'from': peers, 'ports': [{'protocol': 'TCP', 'port': port} for port in ports]}
    def policy(name, labels, rules, namespace='odoo-system'):
        return resource('NetworkPolicy', name, {'podSelector': {'matchLabels': labels},
                        'policyTypes': ['Ingress'], 'ingress': rules}, namespace)
    return [
        policy('odoo-deny-ingress', {}, []),
        policy('odoo-web-from-gateway', {'app': 'odoo', 'component': 'web'},
               [rule([peer('apisix', {'app.kubernetes.io/name': 'apisix'})], [8069, 8072])]),
        policy('odoo-worker-from-runner', {'app': 'odoo', 'component': 'worker'},
               [rule([peer('odoo-system', {'app': 'odoo', 'component': 'queue-runner'})], [8069])]),
        policy('postgres-from-app-and-keda', {'app': 'postgres'},
               [rule([peer('odoo-system', {'app': 'odoo'}), peer('keda')], [5432])]),
        policy('apisix-etcd-private', {'app': 'apisix-etcd'},
               [rule([peer('apisix')], [2379, 2380])], 'apisix'),
        policy('apisix-gateway-boundary', {'app.kubernetes.io/name': 'apisix'},
               [{'ports': [{'protocol': 'TCP', 'port': 9443}]},
                rule([peer('apisix')], [9180, 9091])], 'apisix'),
    ]


def artifacts(config):
    app = transform(render(ROOT), config)
    app += [resource('ApisixTls', 'odoo-tls', {'hosts': [config['domain']],
            'secret': {'name': config['tlsSecret'], 'namespace': 'odoo-system'}},
            api='apisix.apache.org/v2'),
            resource('PodDisruptionBudget', 'odoo-web', {'minAvailable': 1,
            'selector': {'matchLabels': {'app': 'odoo', 'component': 'web'}}}, api='policy/v1')]
    network = policies()
    app += [d for d in network if d['metadata']['namespace'] == 'odoo-system']
    check(app, odoo_image=config['images']['odoo'])
    infrastructure = list(yaml.safe_load_all((ROOT / 'infrastructure/etcd.yaml').read_text()))
    infrastructure[0]['spec']['volumeClaimTemplates'][0]['spec']['storageClassName'] = config['storage']['blockClass']
    infrastructure += [d for d in network if d['metadata']['namespace'] == 'apisix']
    values = yaml.safe_load((ROOT / 'helm/apisix-values.yaml').read_text())
    values['replicaCount'] = 2
    values['service'] = {'type': 'LoadBalancer', 'http': {'enabled': False},
                         'tls': {'servicePort': 443},
                         'annotations': config['loadBalancerAnnotations']}
    values['apisix']['ssl'] = {'enabled': True}
    values['resources'] = {'requests': {'cpu': '250m', 'memory': '256Mi'},
                           'limits': {'cpu': '1000m', 'memory': '1Gi'}}
    values['topologySpreadConstraints'] = [{'maxSkew': 1, 'topologyKey': 'kubernetes.io/hostname',
        'whenUnsatisfiable': 'ScheduleAnyway', 'labelSelector': {
        'matchLabels': {'app.kubernetes.io/name': 'apisix', 'app.kubernetes.io/instance': 'apisix'}}}]
    # This chart's ingress subchart creates its own gateway Service by default.
    values['ingress-controller']['gateway'] = {'type': 'ClusterIP'}
    init = transform([yaml.safe_load((ROOT / 'base/12-init-db.yaml').read_text())], config)
    restore = transform([yaml.safe_load((ROOT / 'dr/restore-job.yaml').read_text())], config)
    return {'app.yaml': app, 'data.yaml': [d for d in app if d['kind'] in (
        'PersistentVolumeClaim', 'ConfigMap', 'StatefulSet') or
        (d['kind'] == 'Service' and d['metadata']['name'] == 'postgres-service')],
        'init-db.yaml': init, 'restore-job.yaml': restore, 'infrastructure.yaml': infrastructure,
        'apisix-values.yaml': [values]}


def write_artifacts(config, output):
    output.mkdir(parents=True, exist_ok=True)
    docs = artifacts(config)
    for name, values in docs.items():
        (output / name).write_text(yaml.safe_dump_all(values, sort_keys=False), encoding='utf-8')
    print(f'Rendered CMC staging artifacts: {output}')
    return docs


class Cluster:
    def __init__(self, config):
        self.config = config

    def kube(self, *args, data=None):
        # Watch commands have their own longer --timeout; do not cut them off at 30s.
        timeout = [] if 'wait' in args or 'rollout' in args else ['--request-timeout=30s']
        return run(['kubectl', '--context', self.config['context'], *timeout, *args], data)

    def get(self, kind, name=None, namespace=None):
        args = ['get', kind]
        if name:
            args += [name, '--ignore-not-found']
        if namespace:
            args += ['-n', namespace]
        result = self.kube(*args, '-o', 'json')
        return json.loads(result) if result.strip() else None

    def preflight(self, controllers=False):
        contexts = self.kube('config', 'get-contexts', '-o', 'name').splitlines()
        if self.config['context'] not in contexts:
            raise ValueError('Configured context does not exist')
        version = json.loads(self.kube('version', '-o', 'json'))['serverVersion']
        minor = version['major'] + '.' + re.match(r'\d+', version['minor'])[0]
        if minor != self.config['kubernetesMinor']:
            raise ValueError(f'Cluster is {minor}, config expects {self.config["kubernetesMinor"]}')
        print(f'API reachable: {version["gitVersion"]}. This pinned minor is upstream EOL; verify CMC patch support.')
        for name in set(self.config['storage'].values()):
            if not self.get('storageclass', name):
                raise ValueError(f'Missing StorageClass {name}; provision NFS/RWX before deploy')
        metric = self.get('apiservice', 'v1beta1.metrics.k8s.io')
        if not metric or not any(c['type'] == 'Available' and c['status'] == 'True'
                                 for c in metric.get('status', {}).get('conditions', [])):
            raise ValueError('CMC Metrics API must be Available; cloud deploy does not overwrite it')
        nodes = self.get('nodes')['items']
        ready = [n for n in nodes if not n['spec'].get('unschedulable') and
                 not any(t['effect'] in ('NoSchedule', 'NoExecute') for t in n['spec'].get('taints', [])) and
                 any(c['type'] == 'Ready' and c['status'] == 'True' for c in n['status']['conditions'])]
        if len(ready) < 2:
            raise ValueError('CMC profile requires at least two Ready schedulable untainted worker nodes')
        for name in self.config['imagePullSecrets']:
            secret = self.get('secret', name, 'odoo-system')
            if not secret or secret.get('type') != 'kubernetes.io/dockerconfigjson' or not secret.get('data', {}).get('.dockerconfigjson'):
                raise ValueError(f'Missing/invalid image pull Secret: {name}')
        tls = self.get('secret', self.config['tlsSecret'], 'odoo-system')
        if not tls or tls.get('type') != 'kubernetes.io/tls' or not all(
                tls.get('data', {}).get(key) for key in ('tls.crt', 'tls.key')):
            raise ValueError('Create the TLS Secret in odoo-system first')
        if self.get('configmap', 'odoo-backup-lock', 'odoo-system'):
            raise ValueError('Backup lock exists: finish/recover backup first')
        if self.get('deployment', 'odoo-web', 'odoo-system'):
            raise ValueError('Legacy odoo-web Deployment exists; migrate before deploying Rollout')
        if self.get('crd', 'rollouts.argoproj.io'):
            rollout = self.get('rollout', 'odoo-web-rollout', 'odoo-system')
            if rollout and rollout.get('status', {}).get('phase') != 'Healthy':
                raise ValueError('Existing Rollout must be Healthy before deploy')
        if self.get('pvc', 'postgres-pvc', 'odoo-system') and not self.get('secret', 'odoo-secrets', 'odoo-system'):
            raise ValueError('Database PVC exists without credentials: restore original Secret')
        for name in ('postgres-pvc', 'odoo-filestore-pvc', 'odoo-backup-pvc', 'odoo-restore-pvc'):
            claim = self.get('pvc', name, 'odoo-system')
            if claim:
                expected = self.config['storage']['blockClass' if name == 'postgres-pvc' else 'sharedClass']
                if claim['spec'].get('storageClassName') != expected:
                    raise ValueError(f'PVC {name} has a different immutable StorageClass; migrate data explicitly')
        if controllers:
            for name in ('rollouts.argoproj.io', 'scaledobjects.keda.sh',
                         'apisixroutes.apisix.apache.org', 'apisixtlses.apisix.apache.org'):
                if not self.get('crd', name):
                    raise ValueError(f'Missing controller CRD {name}; run bootstrap')
        print('Read-only preflight passed. Image pulls, RWX, certificate and data paths still require live smoke tests.')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('render', 'preflight'))
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    config = load_config(args.config, live=args.action != 'render')
    docs = write_artifacts(config, args.output)
    cluster = Cluster(config)
    if args.action == 'preflight':
        cluster.preflight()


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
