"""Offline integration tests for cloud manifest boundaries and deployment guards."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cmc
from gitops.update_images import update


class CloudTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = cmc.load_config(cmc.ROOT / 'cmc/config.example.yaml')
        cls.docs = cmc.artifacts(cls.config)

    def test_all_execution_paths_use_registry_images(self):
        for key in ('app.yaml', 'init-db.yaml', 'restore-job.yaml'):
            for doc in self.docs[key]:
                pod = cmc.pod_spec(doc)
                if pod is None:
                    continue
                for c in pod.get('initContainers', []) + pod['containers']:
                    if 'odoo' in c['image']:
                        self.assertIn(c['image'], self.config['images'].values())
                        self.assertNotIn('-lab', c['image'])
                        self.assertIn('imagePullSecrets', pod)

    def test_private_registry_credentials_propagate_to_jobs(self):
        config = copy.deepcopy(self.config)
        config['imagePullSecrets'] = ['private-hub']
        docs = cmc.artifacts(config)
        for key in ('init-db.yaml', 'restore-job.yaml'):
            self.assertEqual(cmc.pod_spec(docs[key][0])['imagePullSecrets'], [{'name': 'private-hub'}])
        cron = next(d for d in docs['app.yaml'] if d['kind'] == 'CronJob')
        self.assertEqual(cmc.pod_spec(cron)['imagePullSecrets'], [{'name': 'private-hub'}])

    def test_storage_tls_network_and_backup_boundaries(self):
        for doc in self.docs['app.yaml']:
            if doc['kind'] == 'PersistentVolumeClaim':
                block = doc['metadata']['name'] == 'postgres-pvc'
                self.assertEqual(doc['spec']['accessModes'], ['ReadWriteOnce' if block else 'ReadWriteMany'])
                self.assertEqual(doc['spec']['storageClassName'], self.config['storage']['blockClass' if block else 'sharedClass'])
            if doc['kind'] == 'ApisixRoute':
                for rule in doc['spec']['http']:
                    self.assertEqual(rule['match']['hosts'], [self.config['domain']])
                    proxy = next(p for p in rule['plugins'] if p['name'] == 'proxy-rewrite')
                    self.assertEqual(proxy['config']['headers']['set']['X-Forwarded-Proto'], 'https')
            if doc['kind'] == 'CronJob':
                self.assertTrue(doc['spec']['suspend'])
        self.assertTrue(any(d['kind'] == 'ApisixTls' for d in self.docs['app.yaml']))
        deny = next(d for d in self.docs['app.yaml'] if d['metadata']['name'] == 'odoo-deny-ingress')
        self.assertEqual(deny['spec']['ingress'], [])

    def test_placeholders_rejected_before_cluster_access(self):
        with self.assertRaisesRegex(ValueError, 'placeholders'):
            cmc.load_config(cmc.ROOT / 'cmc/config.example.yaml', live=True)

    def test_local_context_and_unqualified_images_rejected(self):
        for key, value in [('context', 'docker-desktop'), ('images', {'odoo': 'odoo:17.0', 'backup': 'backup:15'})]:
            config = copy.deepcopy(self.config)
            config[key] = value
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'config.yaml'
                path.write_text(yaml.safe_dump(config))
                with self.assertRaises(ValueError):
                    cmc.load_config(path)

    def test_version_mismatch_stops_preflight_without_mutation(self):
        cluster = cmc.Cluster(self.config)
        with patch.object(cluster, 'kube', side_effect=[self.config['context'],
                json.dumps({'serverVersion': {'major': '1', 'minor': '36'}})]):
            with self.assertRaisesRegex(ValueError, 'expects'):
                cluster.preflight()

    def test_restore_same_backup_different_database_gets_distinct_job(self):
        names = []
        for db in ('odoo17_restore_drill', 'odoo17_restore_drill_second'):
            result = subprocess.run([sys.executable, str(cmc.ROOT / 'manifest.py'), 'restore',
                '--template', str(cmc.ROOT / 'dr/restore-job.yaml'), '--backup-set', '20261013T190000Z',
                '--database', db], check=True, capture_output=True, text=True)
            names.append(yaml.safe_load(result.stdout)['metadata']['name'])
        self.assertNotEqual(*names)

    def preflight_get(self, kind, name=None, namespace=None):
        if kind == 'storageclass':
            return {'metadata': {'name': name}}
        if kind == 'apiservice':
            return {'status': {'conditions': [{'type': 'Available', 'status': 'True'}]}}
        if kind == 'nodes':
            return {'items': [{'spec': {}, 'status': {'conditions': [
                {'type': 'Ready', 'status': 'True'}]}} for _ in range(2)]}
        if kind == 'secret' and name == self.config['tlsSecret']:
            return {'type': 'kubernetes.io/tls', 'data': {'tls.crt': 'fake', 'tls.key': 'fake'}}
        return None

    def test_preflight_missing_storage_and_lost_db_secret(self):
        for failure in ('storage', 'secret'):
            cluster = cmc.Cluster(self.config)
            def get(kind, name=None, namespace=None):
                if failure == 'storage' and kind == 'storageclass':
                    return None
                if failure == 'secret' and kind == 'pvc' and name == 'postgres-pvc':
                    return {'spec': {'storageClassName': self.config['storage']['blockClass']}}
                return self.preflight_get(kind, name, namespace)
            with patch.object(cluster, 'kube', side_effect=[self.config['context'], json.dumps({
                'serverVersion': {'major': '1', 'minor': '32', 'gitVersion': 'v1.32.7'}})]), \
                patch.object(cluster, 'get', side_effect=get):
                with self.assertRaisesRegex(ValueError, 'StorageClass|original Secret'):
                    cluster.preflight()

    def test_cmc_cli_has_no_direct_cluster_deploy_action(self):
        source = (cmc.ROOT / 'cmc.py').read_text(encoding='utf-8')
        self.assertIn("choices=('render', 'preflight')", source)
        self.assertNotIn("def deploy(self", source)

    def test_gitops_image_promotion_updates_both_images(self):
        source = cmc.ROOT / 'overlays/cmc/kustomization.yaml'
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'kustomization.yaml'
            target.write_text(source.read_text(encoding='utf-8'), encoding='utf-8')
            update(target, 'hgiang25', 'sha-0123456789ab')
            images = yaml.safe_load(target.read_text(encoding='utf-8'))['images']
            self.assertEqual({item['newName'] for item in images}, {
                'docker.io/hgiang25/odoo-queue', 'docker.io/hgiang25/odoo-backup'})
            self.assertEqual({item['newTag'] for item in images}, {'sha-0123456789ab'})


if __name__ == '__main__':
    unittest.main()
