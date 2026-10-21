"""Branch separation must hold even when platform and workload Git SHAs differ."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.reconcile import Cluster, PLATFORM
from scripts.push_gitops import push
from scripts.promote_environment import promote, next_release, publish, digest
from scripts.validate_branch_pair import validate_pair

ROOT = Path(__file__).resolve().parents[1]


class BranchTests(unittest.TestCase):
    def test_platform_and_workload_revisions_are_separate(self):
        cluster = Cluster('fixture')
        with patch.object(cluster, 'legacy_guard'), patch.object(cluster, 'sync') as sync:
            cluster.reconcile('prod', '2'*40, '1'*40)
        calls = {c.args[0]: c.args[1] for c in sync.call_args_list}
        self.assertEqual(calls['odoo-cmc-root'], '1'*40)
        self.assertEqual(calls['odoo-environments'], '1'*40)
        self.assertEqual(calls['odoo-prod'], '2'*40)
        self.assertNotIn('odoo-dev', calls)

    def test_daily_sync_never_syncs_platform_or_peer(self):
        cluster = Cluster('fixture')
        app = cluster.applications()['odoo-dev']
        with patch.object(cluster, 'get', return_value=app), patch.object(cluster, 'legacy_guard'), patch.object(cluster, 'sync') as sync:
            cluster.reconcile('dev', '2'*40, '1'*40, workload_only=True)
        sync.assert_called_once_with('odoo-dev', '2'*40)
        wrong = copy.deepcopy(app)
        wrong['spec']['source']['targetRevision'] = 'prod'
        with patch.object(cluster, 'get', return_value=wrong), patch.object(cluster, 'legacy_guard'), patch.object(cluster, 'sync') as sync:
            with self.assertRaises(ValueError):
                cluster.reconcile('dev', '2'*40, workload_only=True)
            sync.assert_not_called()

    def test_branch_push_is_explicit(self):
        for branch in ('dev', 'prod'):
            with patch('scripts.push_gitops.run') as run:
                run.return_value.returncode = 0
                push(branch)
                run.assert_called_once_with('git', 'push', 'origin', 'HEAD:'+branch, check=False)

    def test_verify_uses_platform_sha_and_environment_sha_independently(self):
        cluster = Cluster('fixture')
        apps = cluster.applications()
        def get(kind, name, namespace):
            app = copy.deepcopy(apps[name])
            source = app['spec']['source']
            revision = source['targetRevision'] if 'chart' in source else (
                '2'*40 if name == 'odoo-prod' else '1'*40)
            app['status'] = {'sync': {'status': 'Synced', 'revision': revision},
                             'health': {'status': 'Healthy'}}
            return app
        checked = []
        def wait(description, predicate):
            if 'accepted source' in description:
                self.assertTrue(predicate(), description)
                checked.append(description)
        with patch.object(cluster, 'get', side_effect=get), patch.object(cluster, 'wait', side_effect=wait):
            cluster.verify('prod', '2'*40, platform_revision='1'*40)
        self.assertEqual(len(checked), len(PLATFORM)+1)

    def test_promotion_reads_dev_snapshot_not_stale_prod_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, target = root/'dev', root/'prod'
            for path in (source/'overlays/dev', target/'overlays/prod'):
                path.mkdir(parents=True)
            images = [{'name': n, 'newName': 'docker.io/example/'+n, 'newTag': 'sha-'+'a'*40}
                      for n in ('odoo-queue', 'odoo-backup')]
            (source/'overlays/dev/kustomization.yaml').write_text(yaml.safe_dump({'images': images}))
            output = target/'overlays/prod/kustomization.yaml'
            output.write_text(yaml.safe_dump({'namespace': 'odoo-prod', 'images': []}))
            promote('sha-'+'a'*40, target, source)
            document = yaml.safe_load(output.read_text())
            self.assertEqual(document['images'], images)
            self.assertEqual(document['namespace'], 'odoo-prod')
            with patch('scripts.promote_environment.publish') as publish_mock:
                promote('sha-'+'a'*40, target, source, 'v1.0.0')
                publish_mock.assert_called_once_with(images, 'v1.0.0')
            released = yaml.safe_load(output.read_text())
            self.assertEqual({i['newTag'] for i in released['images']}, {'v1.0.0'})
            self.assertEqual(released['namespace'], 'odoo-prod')
            self.assertEqual(yaml.safe_load((source/'overlays/dev/kustomization.yaml').read_text())['images'], images)
            before = output.read_text()
            with patch('scripts.promote_environment.publish', side_effect=ValueError('digest mismatch')):
                with self.assertRaises(ValueError):
                    promote('sha-'+'a'*40, target, source, 'v1.0.1')
            self.assertEqual(output.read_text(), before)

    def test_semver_is_allowed_only_for_prod_render(self):
        from validate import render
        from scripts.validate_environments import validate_environment
        for env in ('dev', 'prod'):
            docs = render(ROOT/'overlays'/env)
            # Substitute the rendered image tags, preserving all environment settings.
            text = yaml.safe_dump(docs)
            import re
            text = re.sub(r'sha-[0-9a-f]{12,40}', 'v1.0.0', text)
            docs = yaml.safe_load(text)
            if env == 'prod':
                validate_environment(docs, env)
            else:
                with self.assertRaises(AssertionError):
                    validate_environment(docs, env)

    def test_release_version_allocation(self):
        for current, expected in [('sha-'+'a'*40, 'v1.0.0'), ('v1.0.0', 'v1.0.1'),
                                  ('v2.3.99', 'v2.3.100')]:
            self.assertEqual(next_release([{'newTag': current}]*2), expected)
        for tags in (['latest'], ['v01.0.0'], ['v1.0.0', 'v1.0.1']):
            with self.assertRaises(ValueError):
                next_release([{'newTag': tag} for tag in tags])

    def test_release_copies_digest_and_resumes_partial_publication(self):
        images = [{'newName': 'docker.io/example/'+name, 'newTag': 'sha-'+'a'*40}
                  for name in ('odoo-queue', 'odoo-backup')]
        with patch('scripts.promote_environment.digest', side_effect=['a', 'b', 'a', None, 'a', 'b']), patch(
                'scripts.promote_environment.subprocess.run') as run:
            publish(images, 'v1.0.0')
        run.assert_called_once_with(['docker', 'buildx', 'imagetools', 'create', '--prefer-index=false',
                                    '--tag', 'docker.io/example/odoo-backup:v1.0.0',
                                    'docker.io/example/odoo-backup@b'], check=True)

    def test_release_collision_never_publishes(self):
        images = [{'newName': name, 'newTag': 'sha-'+'a'*40} for name in ('queue', 'backup')]
        with patch('scripts.promote_environment.digest', side_effect=['a', 'b', None, 'other']), patch(
                'scripts.promote_environment.subprocess.run') as run:
            with self.assertRaises(ValueError):
                publish(images, 'v1.0.0')
            run.assert_not_called()

    def test_registry_auth_failure_is_not_missing_tag(self):
        import subprocess
        result = subprocess.CompletedProcess([], 1, '', 'unauthorized')
        with patch('scripts.promote_environment.subprocess.run', return_value=result):
            with self.assertRaises(RuntimeError):
                digest('example:v1.0.0', missing_ok=True)

    def test_promotion_dispatch_has_no_manual_image_inputs(self):
        workflow = yaml.safe_load((ROOT/'.github/workflows/promote-prod.yml').read_text())
        self.assertIsNone(workflow.get('on', workflow.get(True))['workflow_dispatch'])
        self.assertIn('resolve-dev', workflow['jobs']['verify-dev']['needs'])
        self.assertEqual(workflow['jobs']['deploy']['with']['expected_tag'],
                         '${{ needs.promote.outputs.tag }}')

    def test_cross_branch_budget_is_checked(self):
        with patch('scripts.validate_branch_pair.render', return_value=[]), patch(
                'scripts.validate_branch_pair.validate_environment', side_effect=[100, 200]):
            with self.assertRaises(ValueError):
                validate_pair('dev-snapshot', 'prod-snapshot')

    def test_workflow_routing_and_protection_contract(self):
        def workflow(name):
            return yaml.safe_load((ROOT/'.github/workflows'/name).read_text())
        dev = workflow('deploy-dev.yml')
        self.assertEqual(dev.get('on', dev.get(True))['push']['branches'], ['dev'])
        self.assertEqual(dev['jobs']['dev']['with']['environment'], 'dev')
        prod = workflow('deploy-prod.yml')
        self.assertNotIn('push', prod.get('on', prod.get(True)))
        self.assertEqual(prod['jobs']['prod']['with']['environment'], 'prod')
        reusable = workflow('deploy-environment.yml')
        self.assertEqual(reusable['jobs']['reconcile']['environment'], 'odoo-${{ inputs.environment }}')
        for env in ('dev', 'prod'):
            app = yaml.safe_load((ROOT/f'clusters/cmc/{env}/application.yaml').read_text())
            self.assertEqual(app['spec']['source']['targetRevision'], env)
