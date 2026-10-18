"""Regression tests for actual rendered isolation and fail-closed runtime checks."""
import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from validate import ROOT, render
from scripts.environment import eligible_nodes, bootstrap_resources, KEY, placement, audit_pods
from scripts.validate_environments import (validate_environment, validate_environments,
                                           validate_gateway_service)
from scripts.promote_environment import promote
from scripts.update_images import update
from scripts.verify_private_elb import verify
import cmc


class EnvironmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dev, cls.prod = [render(ROOT/'overlays'/env) for env in ('dev', 'prod')]

    def test_full_rendered_boundaries_and_budget(self):
        validate_environments()

    def test_rollout_ports_explicitly_declare_tcp_to_avoid_defaulting_drift(self):
        for docs in (self.dev, self.prod):
            rollout = next(d for d in docs if d['kind'] == 'Rollout')
            web = next(c for c in rollout['spec']['template']['spec']['containers']
                       if c['name'] == 'odoo-web')
            self.assertEqual(web['ports'], [
                {'name': 'http', 'containerPort': 8069, 'protocol': 'TCP'},
                {'name': 'websocket', 'containerPort': 8072, 'protocol': 'TCP'},
            ])

    def test_demo_profiles_are_distinct_without_changing_selectors(self):
        for env, docs, limits in [('dev', self.dev, (1, 2)), ('prod', self.prod, (2, 8))]:
            for doc in docs:
                self.assertEqual(doc['metadata']['labels'][KEY], env)
            index = {(d['kind'], d['metadata']['name']): d for d in docs}
            scaler = index['ScaledObject', 'odoo-web-autoscaler']['spec']
            self.assertEqual((scaler['minReplicaCount'], scaler['maxReplicaCount']), limits)
            rollout = index['Rollout', 'odoo-web-rollout']['spec']
            self.assertNotIn(KEY, rollout['selector']['matchLabels'])
            self.assertEqual('trafficRouting' in rollout['strategy']['canary'], env == 'prod')

    def test_runtime_audit_checks_policy_and_assigned_node_taint(self):
        node = {'metadata': {'name': 'dev-1', 'labels': {KEY: 'dev'}},
                'spec': {'taints': [{'key': KEY, 'value': 'dev', 'effect': 'NoSchedule'}]}}
        pod = {'metadata': {'name': 'web'}, 'spec': dict(placement('dev'), nodeName='dev-1'),
               'status': {'phase': 'Running'}}
        pod['spec']['tolerations'].append({'key': 'node.kubernetes.io/not-ready',
                                          'operator': 'Exists', 'effect': 'NoExecute', 'tolerationSeconds': 300})
        audit_pods([node], [pod], 'dev')
        for mode in ('affinity', 'wildcard', 'peer', 'taint', 'node'):
            bad_pod, bad_node = copy.deepcopy(pod), copy.deepcopy(node)
            if mode == 'affinity': bad_pod['spec'].pop('affinity')
            if mode == 'wildcard': bad_pod['spec']['tolerations'].append({'operator': 'Exists'})
            if mode == 'peer': bad_node['metadata']['labels'][KEY] = 'prod'
            if mode == 'taint': bad_node['spec']['taints'] = []
            if mode == 'node': bad_pod['spec']['nodeName'] = 'unknown'
            with self.assertRaises(ValueError, msg=mode):
                audit_pods([bad_node], [bad_pod], 'dev')

    def test_wrong_node_affinity_and_legacy_reference_are_rejected(self):
        for mutation in ('affinity', 'reference'):
            docs = copy.deepcopy(self.dev)
            rollout = next(d for d in docs if d['kind'] == 'Rollout')
            if mutation == 'affinity':
                rollout['spec']['template']['spec']['affinity']['nodeAffinity']['requiredDuringSchedulingIgnoredDuringExecution']['nodeSelectorTerms'].append({})
            else:
                next(d for d in docs if d['kind'] == 'AnalysisTemplate')['spec']['metrics'][0]['provider']['web']['url'] = 'http://odoo-system.svc'
            with self.assertRaises(AssertionError):
                validate_environment(docs, 'dev')

    def test_dev_external_service_rejected(self):
        docs = copy.deepcopy(self.dev)
        next(d for d in docs if d['kind'] == 'Service')['spec']['type'] = 'NodePort'
        with self.assertRaises(AssertionError):
            validate_environment(docs, 'dev')

    def test_actual_image_promotions_are_independent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('base', 'overlays'):
                shutil.copytree(ROOT/name, root/name)
            before = render(root/'overlays/prod')
            update(root/'overlays/dev/kustomization.yaml', 'hgyang25', 'sha-'+'1'*40)
            dev = render(root/'overlays/dev')
            self.assertEqual(render(root/'overlays/prod'), before)
            image = next(d for d in dev if d['kind']=='Rollout')['spec']['template']['spec']['containers'][0]['image']
            self.assertTrue(image.endswith(':sha-'+'1'*40), image)
            with self.assertRaisesRegex(ValueError, 'Dev changed'):
                promote('sha-'+'2'*40, root)
            promote('sha-'+'1'*40, root)
            prod = render(root/'overlays/prod')
            self.assertEqual(next(d for d in prod if d['kind']=='Rollout')['spec']['template']['spec']['containers'][0]['image'], image)

    def test_taint_ready_cordon_and_other_taint_are_checked(self):
        node = {'metadata': {'name':'dev-1', 'labels':{KEY:'dev'}},
                'spec': {'taints':[{'key':KEY,'value':'dev','effect':'NoSchedule'}]},
                'status': {'conditions':[{'type':'Ready','status':'True'}]}}
        self.assertEqual(eligible_nodes([node], 'dev'), [node])
        self.assertEqual(eligible_nodes([node], 'prod'), [])
        for mode in ('cordon', 'not-ready', 'other-taint', 'missing-taint'):
            changed = copy.deepcopy(node)
            if mode == 'cordon': changed['spec']['unschedulable'] = True
            if mode == 'not-ready': changed['status']['conditions'][0]['status'] = 'False'
            if mode == 'other-taint': changed['spec']['taints'].append({'key':'reserved','effect':'NoSchedule'})
            if mode == 'missing-taint': changed['spec']['taints'] = []
            self.assertEqual(eligible_nodes([changed], 'dev'), [], mode)

    def test_public_class_rejected(self):
        config = yaml.safe_load((ROOT/'cmc/config.example.yaml').read_text())
        config['loadBalancerAnnotations']['loadbalancer.openstack.org/class']='cmc-loadbalancer-public'
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'config.yaml'; path.write_text(yaml.safe_dump(config))
            with self.assertRaisesRegex(ValueError, 'private'):
                cmc.load_config(path)

    def test_gitops_gateway_allows_only_safe_or_private_configured_state(self):
        safe = {'type': 'ClusterIP', 'externalTrafficPolicy': '', 'annotations': {},
                'http': {'enabled': False}}
        self.assertEqual(validate_gateway_service(safe), 'ClusterIP')

        private = copy.deepcopy(safe)
        private.update({
            'type': 'LoadBalancer',
            'externalTrafficPolicy': 'Cluster',
            'annotations': {
                'loadbalancer.openstack.org/class': 'cmc-loadbalancer-private',
                'loadbalancer.openstack.org/flavor-id': 'flavor-2',
            },
        })
        self.assertEqual(validate_gateway_service(private), 'LoadBalancer')

        for mutation in ('public', 'missing-flavor', 'extra-annotation'):
            invalid = copy.deepcopy(private)
            if mutation == 'public':
                invalid['annotations']['loadbalancer.openstack.org/class'] = 'cmc-loadbalancer-public'
            elif mutation == 'missing-flavor':
                invalid['annotations']['loadbalancer.openstack.org/flavor-id'] = ''
            else:
                invalid['annotations']['service.beta.kubernetes.io/openstack-internal-load-balancer'] = 'false'
            with self.assertRaises(AssertionError, msg=mutation):
                validate_gateway_service(invalid)

    def test_runtime_elb_fails_on_public_pending_or_extra_service(self):
        gateway={'metadata':{'name':'apisix-gateway','namespace':'apisix','annotations':{'loadbalancer.openstack.org/class':'cmc-loadbalancer-private'}},'spec':{'type':'LoadBalancer','ports':[{'port':443}]},'status':{'loadBalancer':{'ingress':[{'ip':'10.0.1.20'}]}}}
        verify([gateway])
        for mode in ('public', 'pending', 'extra'):
            item=copy.deepcopy(gateway); services=[item]
            if mode=='public': item['status']['loadBalancer']['ingress'][0]['ip']='8.8.8.8'
            if mode=='pending': item['status']['loadBalancer']['ingress']=[]
            if mode=='extra':
                extra=copy.deepcopy(gateway); extra['metadata']['namespace']='odoo-dev'; services.append(extra)
            with self.assertRaises(AssertionError): verify(services)

    def test_bootstrap_and_restore_use_environment_nodes_secrets_and_images(self):
        for env in ('dev','prod'):
            for kind in ('nfs','db','restore'):
                resources=bootstrap_resources(env,kind)
                for doc in resources:
                    self.assertEqual(doc['metadata']['namespace'],'odoo-'+env)
                    if doc['kind']=='Job':
                        pod=doc['spec']['template']['spec']
                        self.assertEqual(pod['tolerations'][0]['value'],env)
                        if kind=='restore': self.assertIn(':sha-',pod['containers'][0]['image'])
                        if kind=='db':
                            command=pod['containers'][0]['command'][-1]
                            self.assertIn('has_database_privilege',command)
                            self.assertIn('rolsuper OR rolcreatedb',command)


if __name__ == '__main__':
    unittest.main()
