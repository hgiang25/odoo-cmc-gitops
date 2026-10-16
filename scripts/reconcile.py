"""Request Argo CD syncs and verify the exact Git revision; never applies Odoo workloads."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import yaml

ROOT = Path(__file__).resolve().parents[1]
PLATFORM = ('odoo-environments', 'odoo-cmc-infrastructure', 'odoo-argo-rollouts',
            'odoo-keda', 'odoo-apisix')


def application_ready(app, revision):
    status = app.get('status', {})
    return (status.get('sync', {}).get('status') == 'Synced'
            and status.get('sync', {}).get('revision') == revision
            and status.get('health', {}).get('status') == 'Healthy'
            and not app.get('operation')
            and status.get('operationState', {}).get('phase') not in ('Running', 'Terminating', 'Failed', 'Error'))


def workload_ready(doc, image):
    spec, status = doc['spec'], doc.get('status', {})
    desired = spec.get('replicas', 1)
    if desired < 1 or int(status.get('observedGeneration') or 0) != int(doc['metadata']['generation']):
        return False
    if doc['kind'] == 'Rollout' and status.get('phase') != 'Healthy':
        return False
    if doc['kind'] == 'Rollout' and spec.get('restartAt') and status.get('restartedAt') != spec['restartAt']:
        return False
    if any(status.get(field, 0) < desired for field in ('updatedReplicas', 'readyReplicas', 'availableReplicas')):
        return False
    return all(c['image'] == image for c in spec['template']['spec']['containers'])


class Cluster:
    def __init__(self, context, timeout=1800):
        if not context or context == 'docker-desktop':
            raise ValueError('An explicit CMC context is required')
        self.context, self.timeout = context, timeout

    def kube(self, *args, data=None):
        result = subprocess.run(['kubectl', '--context', self.context, '--request-timeout=15s', *args],
                                input=data, capture_output=True, text=True, timeout=60)
        if result.returncode:
            # Do not include stdin or stdout: kubectl responses may contain Secret data.
            raise RuntimeError(f'kubectl {args[0]} failed: {result.stderr.strip()}')
        return result.stdout

    def get(self, kind, name=None, namespace=None, optional=False):
        args = ['get', kind]
        if name:
            args.append(name)
        if namespace:
            args += ['-n', namespace]
        if optional:
            args += ['--ignore-not-found']
        text = self.kube(*args, '-o', 'json')
        return json.loads(text) if text.strip() else None

    def wait(self, description, predicate):
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if predicate():
                print(description + ': passed', flush=True)
                return
            print('Waiting: ' + description, flush=True)
            time.sleep(10)
        raise TimeoutError(description + ': timed out; inspect Argo operation and pod events')

    def legacy_guard(self):
        pods = self.get('pods', namespace='odoo-system')['items']
        if any(p.get('metadata', {}).get('labels', {}).get('app') == 'odoo'
               and p.get('status', {}).get('phase') not in ('Succeeded', 'Failed') for p in pods):
            raise ValueError('Legacy odoo-system writers still exist. Complete cmc/MULTI-ENV.md migration first.')
        # Pending legacy replicas also need to be stopped, otherwise they may start later.
        deployments = self.get('deployments', namespace='odoo-system')['items']
        if any(d['metadata']['name'] in ('odoo-worker', 'odoo-cron', 'odoo-queue-runner')
               and d['spec'].get('replicas', 1) for d in deployments):
            raise ValueError('Legacy Deployment desired replicas must be zero before Dev/Prod deployment')
        if self.get('crd', 'rollouts.argoproj.io', optional=True):
            old = self.get('rollout', 'odoo-web-rollout', 'odoo-system', optional=True)
            if old and old['spec'].get('replicas', 1):
                raise ValueError('Legacy Rollout desired replicas must be zero')
        if self.get('crd', 'scaledobjects.keda.sh', optional=True):
            for scaler in self.get('scaledobjects', namespace='odoo-system')['items']:
                if scaler['metadata'].get('annotations', {}).get('autoscaling.keda.sh/paused-replicas') != '0':
                    raise ValueError('Pause legacy KEDA scalers at zero before deployment')

    def sync(self, name, revision):
        self.wait(f'Application {name} registered',
                  lambda: bool(self.get('application', name, 'argocd', optional=True)))
        marker = 'github-actions-' + os.environ.get('GITHUB_RUN_ID', 'manual') + '-' + str(time.time_ns())
        for attempt in range(6):
            # Do not overwrite an operation started by another actor or auto-sync.
            self.wait(f'Application {name} previous operation finished', lambda: not self.get(
                'application', name, 'argocd').get('operation'))
            app = self.get('application', name, 'argocd')
            if app.get('operation'):
                continue
            if application_ready(app, revision):
                print(f'{name}: already Synced/Healthy at {revision}', flush=True)
                return
            patch = [
                {'op': 'test', 'path': '/metadata/resourceVersion', 'value': app['metadata']['resourceVersion']},
                {'op': 'add', 'path': '/operation', 'value': {
                    'initiatedBy': {'username': marker},
                    'sync': {'revision': revision, 'prune': False, 'syncStrategy': {'apply': {}}},
                    'retry': {'limit': 2, 'backoff': {'duration': '10s', 'factor': 2, 'maxDuration': '1m'}},
                }},
            ]
            try:
                self.kube('-n', 'argocd', 'patch', 'application', name, '--type=json', '-p', json.dumps(patch))
                break
            except RuntimeError:
                newer = self.get('application', name, 'argocd')
                if newer['metadata']['resourceVersion'] == app['metadata']['resourceVersion']:
                    raise
        else:
            raise RuntimeError(f'{name}: repeated concurrent changes; start a new deployment run')

        def finished():
            current = self.get('application', name, 'argocd')
            state = current.get('status', {}).get('operationState', {})
            ours = state.get('operation', {}).get('initiatedBy', {}).get('username') == marker
            if ours and state.get('phase') in ('Failed', 'Error') and not current.get('operation'):
                raise RuntimeError(f'{name} sync failed: {state.get("message", "inspect application status")}')
            return application_ready(current, revision)
        self.wait(f'{name} Synced/Healthy revision {revision}', finished)

    def reconcile(self, environment, revision):
        self.legacy_guard()
        self.sync('odoo-cmc-root', revision)
        apps = self.applications()
        for name in (*PLATFORM, 'odoo-' + environment):
            source = apps[name]['spec']['source']
            expected = source['targetRevision'] if 'chart' in source else revision
            self.sync(name, expected)

    @staticmethod
    def applications():
        apps = {}
        for path in (ROOT / 'clusters/cmc').rglob('*.yaml'):
            doc = yaml.safe_load(path.read_text(encoding='utf-8'))
            if doc.get('kind') == 'Application':
                apps[doc['metadata']['name']] = doc
        return apps

    def verify(self, environment, revision, expected_tag=None):
        ns = 'odoo-' + environment
        for name in (*PLATFORM, ns):
            desired = self.applications()[name]['spec']
            expected = desired['source']['targetRevision'] if 'chart' in desired['source'] else revision

            def accepted(n=name, d=desired, r=expected):
                app = self.get('application', n, 'argocd')
                if any(app['spec'][key] != d[key] for key in ('project', 'source', 'destination')):
                    raise ValueError(f'{n}: live Application wiring differs from committed environment')
                return application_ready(app, r)
            self.wait(f'{name} accepted source and revision {expected}', accepted)
        overlay = yaml.safe_load((ROOT/f'overlays/{environment}/kustomization.yaml').read_text(encoding='utf-8'))
        mapping = next(i for i in overlay['images'] if i['name'] == 'odoo-queue')
        if expected_tag and mapping['newTag'] != expected_tag:
            raise ValueError('Expected release tag differs from the checked-out desired state')
        image = mapping['newName'] + ':' + mapping['newTag']
        for kind, name in [('rollout', 'odoo-web-rollout'), ('deployment', 'odoo-worker'),
                           ('deployment', 'odoo-cron'), ('deployment', 'odoo-queue-runner')]:
            self.wait(f'{ns}/{name} current generation ready with {image}', lambda k=kind, n=name:
                      workload_ready(self.get(k, n, ns), image))
        def pods_ready():
            pods = self.get('pods', namespace=ns)['items']
            active = [p for p in pods if p['metadata'].get('labels', {}).get('app') == 'odoo'
                      and p['metadata'].get('labels', {}).get('component') in ('web', 'worker', 'cron', 'queue-runner')
                      and not p['metadata'].get('deletionTimestamp')]
            return bool(active) and all(
                all(c['image'] == image for c in p['spec']['containers'])
                and any(c['type'] == 'Ready' and c['status'] == 'True' for c in p.get('status', {}).get('conditions', []))
                for p in active)
        self.wait(f'{ns} actual running Odoo pods ready at desired image', pods_ready)
        for name in ('odoo-web-autoscaler', 'odoo-worker-autoscaler'):
            self.wait(f'{ns}/{name} Ready', lambda n=name: any(
                c['type'] == 'Ready' and c['status'] == 'True' for c in self.get(
                    'scaledobject', n, ns).get('status', {}).get('conditions', [])))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('sync', 'verify', 'legacy-guard'))
    parser.add_argument('--context', required=True)
    parser.add_argument('--environment', choices=('dev', 'prod'), required=True)
    parser.add_argument('--revision')
    parser.add_argument('--expected-tag')
    parser.add_argument('--timeout', type=int, default=1800)
    args = parser.parse_args()
    cluster = Cluster(args.context, args.timeout)
    if args.action == 'legacy-guard':
        cluster.legacy_guard()
        return
    if not args.revision or len(args.revision) != 40 or any(c not in '0123456789abcdef' for c in args.revision):
        raise ValueError('Expected full Git SHA is required')
    if args.action == 'sync':
        cluster.reconcile(args.environment, args.revision)
    else:
        cluster.verify(args.environment, args.revision, args.expected_tag)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, TimeoutError, subprocess.SubprocessError) as exc:
        sys.exit(str(exc))
