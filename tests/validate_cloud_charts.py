"""Render pinned Helm charts and validate CMC custom resources against their CRDs.

Requires Helm, kubectl, PyYAML and jsonschema; reads chart repositories, never a cluster.
Run from repository root: python k8s/tests/validate_cloud_charts.py
"""
from pathlib import Path
import sys
import tempfile
import yaml
import jsonschema

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cmc


def main():
    config = cmc.load_config(cmc.ROOT / 'cmc/config.example.yaml')
    artifacts = cmc.artifacts(config)
    schemas = {}
    charts = [('apisix', 'https://apache.github.io/apisix-helm-chart', '2.10.0'), *cmc.CHARTS]
    with tempfile.TemporaryDirectory() as directory:
        values = Path(directory) / 'apisix.yaml'
        values.write_text(yaml.safe_dump(artifacts['apisix-values.yaml'][0]), encoding='utf-8')
        for name, repo, version in charts:
            args = ['helm', 'template', name, name, '--repo', repo, '--version', version,
                    '--namespace', name, '--include-crds', '--kube-version', config['kubernetesMinor'] + '.0']
            if name == 'apisix':
                args += ['-f', str(values)]
            rendered = [d for d in yaml.safe_load_all(cmc.run(args)) if d]
            for doc in rendered:
                if doc['kind'] == 'CustomResourceDefinition':
                    for ver in doc['spec']['versions']:
                        schemas[(doc['spec']['group'] + '/' + ver['name'], doc['spec']['names']['kind'])] = ver['schema']['openAPIV3Schema']
            if name == 'apisix':
                services = [d for d in rendered if d['kind'] == 'Service']
                gateway = next(d for d in services if d['metadata']['name'] == 'apisix-gateway')
                assert gateway['spec']['type'] == 'LoadBalancer'
                assert [p['port'] for p in gateway['spec']['ports']] == [443]
                assert gateway['metadata']['annotations'] == config['loadBalancerAnnotations']
                assert all(d is gateway or d['spec'].get('type', 'ClusterIP') == 'ClusterIP' for d in services)
                deployments = [d for d in rendered if d['kind'] == 'Deployment']
                app = next(d for d in deployments if d['metadata']['name'] == 'apisix')
                assert app['spec']['replicas'] == 2
            print(f'{name} {version}: Helm render passed')
        argocd = [d for d in yaml.safe_load_all(cmc.run([
            'helm', 'template', 'argocd', 'argo-cd',
            '--repo', 'https://argoproj.github.io/argo-helm', '--version', '10.10.3',
            '--namespace', 'argocd', '--include-crds',
            '--kube-version', config['kubernetesMinor'] + '.0'])) if d]
        for doc in argocd:
            if doc['kind'] == 'CustomResourceDefinition':
                for ver in doc['spec']['versions']:
                    schemas[(doc['spec']['group'] + '/' + ver['name'],
                             doc['spec']['names']['kind'])] = ver['schema']['openAPIV3Schema']
        gitops_values = yaml.safe_load((cmc.ROOT / 'gitops/applications/apisix.yaml').read_text(
            encoding='utf-8'))['spec']['source']['helm']['valuesObject']
        values.write_text(yaml.safe_dump(gitops_values), encoding='utf-8')
        cmc.run(['helm', 'template', 'apisix', 'apisix',
                 '--repo', 'https://apache.github.io/apisix-helm-chart', '--version', '2.10.0',
                 '--namespace', 'apisix', '--kube-version', config['kubernetesMinor'] + '.0',
                 '-f', str(values)])
        print('argo-cd 10.10.3 and committed APISIX GitOps values: Helm render passed')
    count = 0
    for doc in artifacts['app.yaml']:
        if doc['apiVersion'].split('/')[0] in ('apisix.apache.org', 'argoproj.io', 'keda.sh'):
            jsonschema.validate(doc, schemas[(doc['apiVersion'], doc['kind'])])
            count += 1
    gitops = cmc.render(cmc.ROOT / 'gitops/applications')
    gitops += [yaml.safe_load((cmc.ROOT / 'gitops/bootstrap/odoo-cmc-root.yaml').read_text(
        encoding='utf-8')), yaml.safe_load((cmc.ROOT / 'gitops/bootstrap/odoo-cmc-project.yaml').read_text(
        encoding='utf-8'))]
    for doc in gitops:
        jsonschema.validate(doc, schemas[(doc['apiVersion'], doc['kind'])])
        count += 1
    print(f'Validated {count} CMC custom resources against pinned chart schemas; HTTPS-only LoadBalancer verified.')


if __name__ == '__main__':
    main()
