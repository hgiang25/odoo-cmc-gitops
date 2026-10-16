"""Read-only check that the gateway advertises RFC1918 addresses and Dev has no external Services."""
import argparse
import ipaddress
import json
import subprocess

PRIVATE = [ipaddress.ip_network(cidr) for cidr in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')]


def verify(services):
    gateway = next(s for s in services if s['metadata']['namespace'] == 'apisix'
                   and s['metadata']['name'] == 'apisix-gateway')
    assert gateway['spec']['type'] == 'LoadBalancer'
    assert gateway['metadata']['annotations']['loadbalancer.openstack.org/class'] == 'cmc-loadbalancer-private'
    assert [p['port'] for p in gateway['spec']['ports']] == [443]
    endpoints = gateway.get('status', {}).get('loadBalancer', {}).get('ingress', [])
    assert endpoints, 'Private ELB has no assigned address yet'
    for entry in endpoints:
        assert 'ip' in entry, 'An ELB hostname requires explicit resolution and cloud-side verification'
        address = ipaddress.ip_address(entry['ip'])
        assert any(address in network for network in PRIVATE), 'Gateway advertises a non-RFC1918 IP'
    for service in services:
        assert not service['spec'].get('externalIPs'), 'Unexpected externalIPs; review before acceptance'
        if service['metadata']['namespace'] == 'odoo-dev':
            assert service['spec'].get('type', 'ClusterIP') == 'ClusterIP'
        if service['spec'].get('type') == 'LoadBalancer':
            assert service is gateway, 'Unexpected additional ELB; review cloud allocation'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context', required=True)
    args = parser.parse_args()
    result = subprocess.run(['kubectl', '--context', args.context, '--request-timeout=10s',
                             'get', 'services', '-A', '-o', 'json'], check=True,
                            text=True, capture_output=True, timeout=45)
    verify(json.loads(result.stdout)['items'])
    print('One private HTTPS gateway; no external Dev service. Confirm no floating IP in the CMC portal too.')
