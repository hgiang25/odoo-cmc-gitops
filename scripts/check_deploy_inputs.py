"""Validate deployment inputs against rendered desired state before mutating runtime Secrets."""
import os
from pathlib import Path
import sys
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from validate import ROOT, render


def check(environment, values=os.environ, root=ROOT):
    if environment not in ('dev', 'prod'):
        raise ValueError('Environment must be dev or prod')
    required = ['KUBE_CONTEXT', 'CMC_POSTGRES_HOST', 'CMC_POSTGRES_DATABASE',
                'CMC_POSTGRES_USER', 'CMC_POSTGRES_PASSWORD', 'DOCKERHUB_USERNAME', 'DOCKERHUB_TOKEN']
    if environment == 'prod':
        required += ['CMC_TLS_CERTIFICATE', 'CMC_TLS_PRIVATE_KEY', 'ODOO_DOMAIN']
    missing = [key for key in required if not values.get(key)]
    if missing:
        raise ValueError('Missing GitHub environment variables/secrets: ' + ', '.join(missing))
    if values['KUBE_CONTEXT'] == 'docker-desktop':
        raise ValueError('CMC deployment refuses docker-desktop')
    if values['CMC_POSTGRES_DATABASE'] != 'odoo17_' + environment:
        raise ValueError('Database does not match the selected environment')
    if values.get('CMC_POSTGRES_SSLMODE') not in ('disable', 'require', 'verify-ca', 'verify-full'):
        raise ValueError('Set an explicit PostgreSQL TLS policy')
    port = values.get('CMC_POSTGRES_PORT', '5432')
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError('Invalid PostgreSQL port')
    resources = render(root / 'overlays' / environment)
    platform = render(root / 'clusters/cmc')
    infra = render(root / 'overlays/cmc-infrastructure')
    if 'REPLACE_' in yaml.safe_dump(resources + platform + infra):
        raise ValueError('Unfilled desired-state placeholders; run Configure CMC desired state first')
    apisix = next(item for item in platform if item.get('kind') == 'Application'
                  and item['metadata']['name'] == 'odoo-apisix')
    service = apisix['spec']['source']['helm']['valuesObject']['service']
    if (service.get('type') != 'LoadBalancer'
            or service.get('annotations', {}).get('loadbalancer.openstack.org/class')
            != 'cmc-loadbalancer-private'
            or not service.get('annotations', {}).get('loadbalancer.openstack.org/flavor-id')):
        raise ValueError('Configure must commit a private APISIX LoadBalancer before deployment')
    for resource in resources:
        if resource['kind'] == 'PersistentVolumeClaim':
            if resource['spec']['storageClassName'] != values.get('CMC_RWX_STORAGE_CLASS', 'nfs-csi'):
                raise ValueError('Environment RWX StorageClass differs from committed desired state')
        if resource['kind'] == 'ApisixTls' and resource['spec']['hosts'] != [values['ODOO_DOMAIN']]:
            raise ValueError('Production ODOO_DOMAIN differs from configured APISIX TLS host')
    print(f'{environment}: required inputs and rendered configuration match; no credential values logged.')


if __name__ == '__main__':
    try:
        check(os.environ.get('ODOO_ENV'))
    except ValueError as exc:
        sys.exit(str(exc))
