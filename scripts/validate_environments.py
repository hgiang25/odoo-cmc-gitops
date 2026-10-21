"""Validate rendered environment boundaries, scheduling, references and shared DB budget."""
import configparser
import math
import re
from pathlib import Path
import sys
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from validate import ROOT, check, render
from scripts.environment import KEY, bootstrap_resources


def validate_gateway_service(service):
    """Accept only the unpublished gateway or the configured private CMC ELB."""
    assert service['http']['enabled'] is False
    if service['type'] == 'ClusterIP':
        assert service['externalTrafficPolicy'] == ''
        assert service['annotations'] == {}
        return 'ClusterIP'

    assert service['type'] == 'LoadBalancer'
    assert service['externalTrafficPolicy'] == 'Cluster'
    annotations = service.get('annotations', {})
    assert set(annotations) == {
        'loadbalancer.openstack.org/class',
        'loadbalancer.openstack.org/flavor-id',
    }
    assert annotations['loadbalancer.openstack.org/class'] == 'cmc-loadbalancer-private'
    assert annotations['loadbalancer.openstack.org/flavor-id']
    return 'LoadBalancer'


def pod_spec(doc):
    if doc['kind'] in ('Deployment', 'Rollout', 'Job', 'StatefulSet'):
        return doc['spec']['template']['spec']
    if doc['kind'] == 'CronJob':
        return doc['spec']['jobTemplate']['spec']['template']['spec']


def validate_environment(resources, environment):
    ns = 'odoo-' + environment
    index = {(d['kind'], d['metadata']['name']): d for d in resources}
    assert all(d['metadata'].get('namespace') == ns for d in resources), 'Foreign namespace'
    assert 'odoo-system' not in yaml.safe_dump(resources), 'Unrewritten legacy namespace reference'
    assert not any(d['kind'] in ('Secret', 'Namespace', 'ClusterRole', 'ClusterRoleBinding', 'StatefulSet')
                   for d in resources), 'Workload overlay must not own cluster resources or DB credentials'
    rollout = index['Rollout', 'odoo-web-rollout']
    assert index['Deployment', 'odoo-worker']['spec']['strategy']['rollingUpdate']['maxSurge'] == 0
    for name in ('odoo-cron', 'odoo-queue-runner'):
        assert index['Deployment', name]['spec']['strategy']['type'] == 'Recreate'
    image = rollout['spec']['template']['spec']['containers'][0]['image']
    tag = image.rsplit(':', 1)[-1]
    pattern = r'sha-[0-9a-f]{12,40}'
    if environment == 'prod':
        pattern += r'|v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)'
    assert re.fullmatch(pattern, tag), 'Invalid environment image tag'
    check(resources, odoo_image=image)
    for doc in resources:
        pod = pod_spec(doc)
        if not pod:
            continue
        assert pod['tolerations'] == [{'key': KEY, 'operator': 'Equal', 'value': environment, 'effect': 'NoSchedule'}]
        terms = pod['affinity']['nodeAffinity']['requiredDuringSchedulingIgnoredDuringExecution']['nodeSelectorTerms']
        assert terms == [{'matchExpressions': [{'key': KEY, 'operator': 'In', 'values': [environment]}]}]
        for volume in pod.get('volumes', []):
            if 'persistentVolumeClaim' in volume:
                assert ('PersistentVolumeClaim', volume['persistentVolumeClaim']['claimName']) in index
        assert not pod.get('hostNetwork') and not pod.get('nodeName')
    for doc in resources:
        if doc['kind'] == 'RoleBinding':
            for subject in doc.get('subjects', []):
                if subject['kind'] == 'ServiceAccount':
                    assert subject['namespace'] == ns
        if doc['kind'] == 'Service':
            assert doc['spec'].get('type', 'ClusterIP') == 'ClusterIP'
            assert not doc['spec'].get('externalIPs')
        if doc['kind'] == 'PersistentVolumeClaim':
            assert 'Prune=false' in doc['metadata']['annotations']['argocd.argoproj.io/sync-options']
    ingress = index['NetworkPolicy', 'odoo-deny-ingress']['spec']
    assert ingress['podSelector'] == {} and ingress['ingress'] == []
    runner = index['NetworkPolicy', 'odoo-worker-from-runner']['spec']['ingress'][0]['from'][0]
    assert runner['namespaceSelector']['matchLabels']['kubernetes.io/metadata.name'] == ns
    config = configparser.ConfigParser(interpolation=None)
    config.read_string(index['ConfigMap', 'odoo-config']['data']['odoo.conf'])
    assert config['options']['db_name'] == 'odoo17_' + environment
    assert config['options']['dbfilter'] == '^odoo17_' + environment + '$'
    assert index['ConfigMap', 'odoo-runtime-settings']['data']['s3_prefix'] == f'odoo/{environment}/backups'
    if environment == 'dev':
        assert not any(d['kind'] in ('ApisixRoute', 'ApisixTls', 'Ingress') for d in resources)
        assert 'trafficRouting' not in rollout['spec']['strategy']['canary']
        assert ('NetworkPolicy', 'odoo-web-from-gateway') not in index
        nginx = index['ConfigMap', 'odoo-dev-access']['data']['nginx.conf']
        assert 'odoo-web-stable:8072' in nginx and 'odoo-web-stable:8069' in nginx
        assert 'Upgrade $http_upgrade' in nginx
        for rule in index['Role', 'odoo-developer']['rules']:
            assert not {'secrets', 'pods/exec', 'nodes', '*'}.intersection(rule['resources'])
    else:
        assert index['ApisixTls', 'odoo-tls']['spec']['secret']['namespace'] == ns
        assert rollout['spec']['replicas'] >= 2
    for kind in ('nfs', 'db', 'restore'):
        for doc in bootstrap_resources(environment, kind):
            assert doc['metadata']['namespace'] == ns
            pod = pod_spec(doc)
            if pod:
                assert pod['affinity']['nodeAffinity']['requiredDuringSchedulingIgnoredDuringExecution']['nodeSelectorTerms'] == terms
    max_web = index['ScaledObject', 'odoo-web-autoscaler']['spec']['maxReplicaCount']
    max_worker = index['ScaledObject', 'odoo-worker-autoscaler']['spec']['maxReplicaCount']
    surge = math.ceil(max_web * .2)
    return ((max_web + surge) * 3 + max_worker * 3 + 3) * config['options'].getint('db_maxconn')


def validate_environments(root=ROOT):
    sets = [render(root / 'overlays' / env) for env in ('dev', 'prod')]
    total = sum(validate_environment(docs, env) for docs, env in zip(sets, ('dev', 'prod'))) + 32
    assert total <= 300, f'Shared DB connection budget {total} exceeds 300'
    apps = {d['metadata']['name']: d for d in render(root / 'clusters/cmc')}
    for env in ('dev', 'prod'):
        spec = apps['odoo-'+env]['spec']
        assert spec['source']['path'] == 'overlays/'+env
        assert spec['source']['targetRevision'] == env
        assert spec['destination'] == {'server': 'https://kubernetes.default.svc', 'namespace': 'odoo-'+env}
        project = yaml.safe_load((root/f'bootstrap/odoo-{env}-project.yaml').read_text(encoding='utf-8'))
        assert project['spec']['destinations'] == [spec['destination']]
        assert project['spec']['clusterResourceWhitelist'] == []
    assert apps['odoo-dev']['spec']['syncPolicy']['automated'] == {
        'prune': True, 'selfHeal': True, 'allowEmpty': False}
    for name in ('odoo-cmc', 'odoo-prod'):
        assert 'automated' not in apps[name]['spec']['syncPolicy']
    for name in ('odoo-cmc', 'odoo-dev', 'odoo-prod'):
        assert 'finalizers' not in apps[name]['metadata']
    service = apps['odoo-apisix']['spec']['source']['helm']['valuesObject']['service']
    validate_gateway_service(service)
    retired = render(root / 'overlays/cmc-retired')
    for doc in retired:
        assert doc['kind'] not in ('ApisixRoute', 'ApisixTls', 'Job')
        if doc['kind'] in ('Deployment', 'Rollout'):
            assert doc['spec']['replicas'] == 0
        if doc['kind'] == 'ScaledObject':
            assert doc['metadata']['annotations']['autoscaling.keda.sh/paused-replicas'] == '0'
        if doc['kind'] == 'CronJob':
            assert doc['spec']['suspend'] is True
        if doc['kind'] in ('Namespace', 'PersistentVolumeClaim'):
            assert 'Prune=false' in doc['metadata']['annotations']['argocd.argoproj.io/sync-options']
    print(f'Dev/Prod isolation and scheduling passed; shared DB budget {total}/300 including reserve.')
    return sets


if __name__ == '__main__':
    validate_environments()
