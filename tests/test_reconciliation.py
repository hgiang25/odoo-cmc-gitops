"""Regression cases for stale health, failed reruns, Argo races and Git publication."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.reconcile import Cluster, PLATFORM, application_ready, workload_ready
from scripts.runtime_secrets import acknowledge, digest, needs_restart
from scripts.check_health import healthy
from scripts.push_gitops import push

SHA = '1' * 40
IMAGE = 'docker.io/example/odoo:sha-' + SHA


def ready_app():
    return {'metadata': {'resourceVersion': '1'}, 'status': {
        'sync': {'status': 'Synced', 'revision': SHA}, 'health': {'status': 'Healthy'},
        'operationState': {'phase': 'Succeeded'}}}


class ReconciliationTests(unittest.TestCase):
    def test_old_revision_failed_or_running_operation_cannot_pass(self):
        app = ready_app()
        self.assertTrue(application_ready(app, SHA))
        self.assertFalse(application_ready(app, '2' * 40))
        for phase in ('Running', 'Terminating', 'Failed', 'Error'):
            app['status']['operationState']['phase'] = phase
            self.assertFalse(application_ready(app, SHA), phase)
        app = ready_app()
        app['operation'] = {'sync': {}}
        self.assertFalse(application_ready(app, SHA))

    def test_healthy_rollout_must_observe_generation_restart_and_actual_replicas(self):
        doc = {'kind': 'Rollout', 'metadata': {'generation': 5},
               'spec': {'replicas': 2, 'template': {'spec': {'containers': [{'image': IMAGE}]}}},
               'status': {'phase': 'Healthy', 'observedGeneration': '5',
                          'updatedReplicas': 2, 'readyReplicas': 2, 'availableReplicas': 2}}
        self.assertTrue(workload_ready(doc, IMAGE))
        for field, value in [('observedGeneration', '4'), ('observedGeneration', ''),
                             ('updatedReplicas', 1), ('readyReplicas', 1), ('phase', 'Paused')]:
            changed = copy.deepcopy(doc)
            changed['status'][field] = value
            self.assertFalse(workload_ready(changed, IMAGE), field)
        self.assertFalse(workload_ready(doc, IMAGE + '-other'))
        doc['spec']['restartAt'] = '2026-10-16T00:00:00Z'
        self.assertFalse(workload_ready(doc, IMAGE))
        doc['status']['restartedAt'] = doc['spec']['restartAt']
        self.assertTrue(workload_ready(doc, IMAGE))

    def test_secret_restart_is_durable_across_failed_runs(self):
        before = {'exists': True, 'hashes': {'db': 'old'}, 'accepted': digest({'db': 'old'})}
        self.assertFalse(needs_restart(before, before))
        after = dict(before, hashes={'db': 'new'})
        self.assertTrue(needs_restart(before, after))
        # Previous run updated the Secret but failed before acknowledging a healthy deployment.
        self.assertTrue(needs_restart(after, after))
        accepted = dict(after, accepted=digest(after['hashes']))
        self.assertFalse(needs_restart(accepted, accepted))
        self.assertFalse(needs_restart(dict(before, exists=False), after))

    def test_http_200_with_missing_or_failed_database_is_not_healthy(self):
        for body in ('<html>login</html>', '{}', '[]', 'null',
                     '{"status":"pass"}', '{"status":"pass","db_server_status":false}',
                     '{"status":"fail","db_server_status":true}'):
            self.assertFalse(healthy(body), body)
        self.assertTrue(healthy('{"status":"pass","db_server_status":true}'))

    def test_secret_changes_during_verify_are_not_acknowledged(self):
        cluster = Cluster('fixture')
        with patch.object(cluster, 'kube') as kube:
            with self.assertRaisesRegex(RuntimeError, 'changed during deployment'):
                acknowledge(cluster, 'prod', {'hashes': {'db': 'new'}}, {'hashes': {'db': 'old'}})
            kube.assert_not_called()
            acknowledge(cluster, 'prod', {'hashes': {'db': 'new'}}, {'hashes': {'db': 'new'}})
            self.assertEqual(kube.call_args.args[2], 'odoo-prod')

    def test_sync_order_and_environment_are_explicit(self):
        cluster = Cluster('fixture')
        with patch.object(cluster, 'legacy_guard') as guard, patch.object(cluster, 'sync') as sync:
            cluster.reconcile('prod', SHA)
        guard.assert_called_once()
        self.assertEqual([c.args[0] for c in sync.call_args_list],
                         ['odoo-cmc-root', *PLATFORM, 'odoo-prod'])
        self.assertEqual(sync.call_args_list[-1].args[1], SHA)
        self.assertEqual(next(c.args[1] for c in sync.call_args_list if c.args[0] == 'odoo-apisix'), '2.10.0')

    def test_noop_sync_does_not_request_another_operation(self):
        cluster = Cluster('fixture')
        with patch.object(cluster, 'get', return_value=ready_app()), patch.object(cluster, 'kube') as kube:
            cluster.sync('odoo-prod', SHA)
        kube.assert_not_called()

    def test_cas_conflict_retries_without_overwriting_another_operation(self):
        cluster = Cluster('fixture')
        app = ready_app()
        app['status']['sync']['revision'] = '0' * 40
        patches = []

        def kube(*args, **kwargs):
            operation = json.loads(args[-1])
            patches.append(operation)
            self.assertEqual(operation[0]['value'], app['metadata']['resourceVersion'])
            if len(patches) == 1:
                app['metadata']['resourceVersion'] = '2'
                raise RuntimeError('JSON patch conflict')
            app['status']['sync']['revision'] = SHA
            app['status']['operationState'] = {'phase': 'Succeeded', 'operation': operation[1]['value']}
            return '{}'

        with patch.object(cluster, 'get', side_effect=lambda *a, **k: copy.deepcopy(app)), \
                patch.object(cluster, 'kube', side_effect=kube):
            cluster.sync('odoo-prod', SHA)
        self.assertEqual(len(patches), 2)
        self.assertFalse(patches[-1][1]['value']['sync']['prune'])

    def test_failed_argo_operation_is_reported_instead_of_accepting_old_health(self):
        cluster = Cluster('fixture')
        app = ready_app()
        app['status']['sync']['revision'] = '0' * 40

        def kube(*args, **kwargs):
            operation = json.loads(args[-1])[1]['value']
            app['status']['operationState'] = {'phase': 'Failed', 'operation': operation, 'message': 'hook failed'}

        with patch.object(cluster, 'get', side_effect=lambda *a, **k: copy.deepcopy(app)), \
                patch.object(cluster, 'kube', side_effect=kube):
            with self.assertRaisesRegex(RuntimeError, 'hook failed'):
                cluster.sync('odoo-prod', SHA)


class GitPushTests(unittest.TestCase):
    def test_concurrent_non_overlapping_push_is_rebased_and_revalidated(self):
        def git(folder, *args):
            return subprocess.run(['git', '-C', str(folder), *args], check=True,
                                  capture_output=True, text=True).stdout.strip()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote, first, second = [root / name for name in ('remote.git', 'first', 'second')]
            remote.mkdir()
            git(remote, 'init', '--bare', '--initial-branch=main')
            git(root, 'clone', str(remote), str(first))
            for folder in (first,):
                git(folder, 'config', 'user.email', 'test@example.invalid')
                git(folder, 'config', 'user.name', 'Fixture')
            (first / 'validate.py').write_text("from pathlib import Path\nPath('validated').touch()\n")
            git(first, 'add', '.')
            git(first, 'commit', '-m', 'initial')
            git(first, 'push', 'origin', 'main')
            git(root, 'clone', str(remote), str(second))
            git(second, 'config', 'user.email', 'test@example.invalid')
            git(second, 'config', 'user.name', 'Fixture')
            (first / 'dev').write_text('dev image')
            git(first, 'add', 'dev')
            git(first, 'commit', '-m', 'dev')
            (second / 'platform').write_text('platform update')
            git(second, 'add', 'platform')
            git(second, 'commit', '-m', 'platform')
            git(second, 'push', 'origin', 'main')
            original = Path.cwd()
            try:
                os.chdir(first)
                push()
            finally:
                os.chdir(original)
            self.assertTrue((first / 'validated').exists())
            self.assertTrue((first / 'platform').exists())
            self.assertEqual(git(first, 'rev-parse', 'HEAD'), git(remote, 'rev-parse', 'main'))


if __name__ == '__main__':
    unittest.main()
