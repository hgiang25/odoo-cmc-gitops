"""Remember only Secret hashes, then report whether an existing workload needs a restart."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.reconcile import Cluster

ANNOTATION = 'odoo.cmc/runtime-secret-hash'


def digest(values):
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def snapshot(cluster, environment):
    ns = 'odoo-' + environment
    values = {}
    for name in ('odoo-db-connection', 'odoo-secrets', 'odoo-registry', 'odoo-tls', 'odoo-s3-credentials'):
        secret = cluster.get('secret', name, ns, optional=True)
        values[name] = hashlib.sha256(json.dumps(secret.get('data', {}) if secret else {},
                                                sort_keys=True).encode()).hexdigest()
    exists = False
    if cluster.get('crd', 'rollouts.argoproj.io', optional=True):
        exists = bool(cluster.get('rollout', 'odoo-web-rollout', ns, optional=True))
    namespace = cluster.get('namespace', ns, optional=True) or {}
    accepted = namespace.get('metadata', {}).get('annotations', {}).get(ANNOTATION)
    return {'exists': exists, 'hashes': values, 'accepted': accepted}


def needs_restart(before, after):
    # A failed previous run may already have written Secrets without restarting pods.
    return before['exists'] and (before['hashes'] != after['hashes']
                                 or before.get('accepted') != digest(after['hashes']))


def acknowledge(cluster, environment, current, prepared):
    if current['hashes'] != prepared['hashes']:
        raise RuntimeError('Runtime Secrets changed during deployment; refusing to acknowledge stale pods')
    cluster.kube('annotate', 'namespace', 'odoo-' + environment,
                 ANNOTATION + '=' + digest(current['hashes']), '--overwrite')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('snapshot', 'changed', 'accept'))
    parser.add_argument('--context', required=True)
    parser.add_argument('--environment', choices=('dev', 'prod'), required=True)
    parser.add_argument('--state', type=Path, required=True)
    args = parser.parse_args()
    current = snapshot(Cluster(args.context), args.environment)
    if args.action == 'snapshot':
        args.state.write_text(json.dumps(current), encoding='utf-8')
        args.state.chmod(0o600)
    elif args.action == 'changed':
        before = json.loads(args.state.read_text(encoding='utf-8'))
        print('restart=' + str(needs_restart(before, current)).lower())
        args.state.write_text(json.dumps(current), encoding='utf-8')
    else:
        prepared = json.loads(args.state.read_text(encoding='utf-8'))
        acknowledge(Cluster(args.context), args.environment, current, prepared)
        args.state.unlink()
