"""CMC manifest renderer and read-only preflight for the Argo CD deployment."""
import argparse
import copy
import ipaddress
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
                'storage', 'database', 's3', 'tlsSecret', 'loadBalancerAnnotations',
                'backupScheduleEnabled'}
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
    if set(config['database']) != {'privateIp'}:
        raise ValueError('database must specify the CMC DBaaS privateIp')
    try:
        ipaddress.ip_address(config['database']['privateIp'])
    except ValueError as error:
        raise ValueError('database.privateIp must be an IPv4 or IPv6 address') from error
    if set(config['s3']) != {'endpointUrl', 'bucket', 'prefix', 'region'}:
        raise ValueError('s3 must specify endpointUrl, bucket, prefix and region')
    if not re.fullmatch(r'https://[^/]+', config['s3']['endpointUrl']):
        raise ValueError('s3.endpointUrl must be an HTTPS origin without a path')
    if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', config['s3']['bucket']):
        raise ValueError('s3.bucket is invalid')
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
                for key, marker in (('odoo', 'odoo-queue'), ('backup', 'odoo-backup')):
                    if marker in container['image']:
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
            doc['spec']['storageClassName'] = config['storage']['sharedClass']
        if doc['kind'] == 'EndpointSlice' and doc['metadata']['name'] == 'postgres-service-dbaas':
            doc['addressType'] = 'IPv6' if ':' in config['database']['privateIp'] else 'IPv4'
            doc['endpoints'][0]['addresses'] = [config['database']['privateIp']]
        if doc['kind'] == 'ConfigMap' and doc['metadata']['name'] == 'odoo-runtime-settings':
            doc['data'].update({'s3_required': 'true', 's3_endpoint_url': config['s3']['endpointUrl'],
                's3_bucket': config['s3']['bucket'], 's3_prefix': config['s3']['prefix'],
                'aws_region': config['s3']['region']})
        if doc['kind'] == 'CronJob':
            doc['spec']['suspend'] = not config['backupScheduleEnabled']
        if doc['kind'] == 'ApisixRoute':
            for rule in doc['spec']['http']:
                rule['match']['hosts'] = [config['domain']]
                proxy = next((p for p in rule['plugins'] if p['name'] == 'proxy-rewrite'), None)
                if proxy is None:
                    proxy = {'name': 'proxy-rewrite', 'enable': True, 'config': {'headers': {'set': {}}}}
                    rule['plugins'].append(proxy)
                proxy['config']['headers']['set'].update(
                    {'X-Forwarded-Host': config['domain'], 'X-Forwarded-Proto': 'https'})
        if doc['kind'] == 'ApisixTls':
            doc['spec']['hosts'] = [config['domain']]
    return docs


def artifacts(config):
    app = transform(render(ROOT / 'overlays/cmc'), config)
    check(app, odoo_image=config['images']['odoo'])
    infrastructure = render(ROOT / 'overlays/cmc-infrastructure')
    etcd = next(d for d in infrastructure if d['kind'] == 'StatefulSet' and
                d['metadata']['name'] == 'apisix-etcd')
    etcd['spec']['volumeClaimTemplates'][0]['spec']['storageClassName'] = config['storage']['blockClass']
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
        'PersistentVolumeClaim', 'ConfigMap', 'EndpointSlice') or
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
        required_secrets = {
            'odoo-secrets': {'POSTGRES_USER', 'POSTGRES_PASSWORD', 'ADMIN_PASSWORD',
                             'ADMIN_LOGIN_PASSWORD'},
            'odoo-s3-credentials': {'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY'},
        }
        for name, keys in required_secrets.items():
            secret = self.get('secret', name, 'odoo-system')
            if not secret or not keys.issubset(secret.get('data', {})):
                raise ValueError(f'Missing runtime Secret: {name}')
        if self.get('pvc', 'postgres-pvc', 'odoo-system'):
            raise ValueError('Legacy postgres-pvc still exists; migrate and verify DBaaS before pruning it')
        for name in ('odoo-filestore-pvc', 'odoo-backup-pvc', 'odoo-restore-pvc'):
            claim = self.get('pvc', name, 'odoo-system')
            if claim:
                expected = self.config['storage']['sharedClass']
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
