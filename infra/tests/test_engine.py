import copy
import base64
import datetime
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from infra.engine.core import ROOT, PREFIX, Rejected, bounded_path, environment, job, limits, read_yaml, validate, validate_target, wrapper
from infra.engine.policies import platform, safety_rules
from infra.engine.runtime import Executor, SafetyMonitor, actor, fetch_service, html_from_logs, prepare, reap
from infra.engine.webhook import abort


class PolicyTests(unittest.TestCase):
    def setUp(self):
        clean_environment = patch.dict('os.environ', {}, clear=True)
        clean_environment.start()
        self.addCleanup(clean_environment.stop)
        self.policy = limits()
        self.service = read_yaml(ROOT / 'infra/services.yaml')['services']['payment-service']
        self.source = ROOT / self.service['local_path']
        self.config = read_yaml(self.source / self.service['config_path'])

    def validate(self, config=None):
        return validate(config or self.config, 'payment-service', self.service, self.policy, self.source, self.service['local_image'], True)

    def test_valid_plan_and_platform_owned_ramp(self):
        plan = self.validate()
        self.assertEqual(plan['vus'], 10)
        self.assertEqual(plan['duration'], 30)
        plan.update(run_id='a' * 12, namespace='loadtest-payment-service-' + 'a' * 12)
        text = wrapper(plan)
        self.assertIn('ramping-vus', text)
        self.assertIn('"duration": "5s"', text)
        self.assertIn('business response is successful', text)

    def test_rejects_workload_and_target_bypasses(self):
        for field, value in [('vus', 5001), ('duration', '46m'), ('rampUp', '0s'), ('parallelism', 3),
                             ('targetService', 'production.example.com'), ('profile', 'unlimited'),
                             ('env', {'K6_VUS': '9999'}), ('scriptPath', '../infra/engine/core.py')]:
            with self.subTest(field=field, value=value):
                config = copy.deepcopy(self.config)
                config[field] = value
                with self.assertRaises(Rejected):
                    self.validate(config)

    def test_small_profile_ceiling_is_stricter_than_platform(self):
        self.policy['profiles']['small']['vus'] = 51
        with self.assertRaises(Rejected):
            self.validate()

    def test_resources_and_smoke_duration_are_configurable(self):
        profile = self.policy['profiles']['small']
        profile.update(generator_memory='512Mi', generator_request_memory='256Mi', vus=20)
        self.policy['smoke_seconds'] = 7
        plan = self.validate()
        plan.update(run_id='a' * 12, namespace='loadtest-payment-service-' + 'a' * 12, expires_at=2000000000)
        pod = job(plan, self.policy, smoke=True, local=True)[1]['spec']['template']['spec']
        self.assertEqual(pod['containers'][0]['resources']['limits']['memory'], '512Mi')
        self.assertIn('"duration": "7s"', wrapper(plan, smoke=True))
        self.assertEqual(plan['vus'], 20)

    def test_resource_request_over_limit_and_quota_that_cannot_fit_are_rejected(self):
        profile = self.policy['profiles']['small']
        profile['generator_request_memory'] = '512Mi'
        with self.assertRaises(Rejected):
            self.validate()
        profile['generator_request_memory'] = '128Mi'
        profile['namespace_cpu'] = '1'
        with self.assertRaises(Rejected):
            self.validate()

    def test_verified_non_owner_is_denied_before_repository_fetch(self):
        with patch('infra.engine.runtime.actor', return_value='99999'), patch('infra.engine.runtime.fetch_service') as fetch:
            with self.assertRaisesRegex(Rejected, 'does not own'):
                prepare('payment-service', 'a' * 40, self.service['image_repository'] + '@sha256:' + 'a' * 64)
            fetch.assert_not_called()

    def test_safety_rule_uses_configured_thresholds_and_hold_time(self):
        self.policy.update(cpu_abort_ratio=0.8, error_abort_ratio=0.02, abort_for_seconds=45)
        rules = safety_rules(self.policy)['spec']['groups'][0]['rules']
        self.assertIn('> 0.8', rules[0]['expr'])
        self.assertIn('> 0.02', rules[1]['expr'])
        self.assertTrue(all(rule['for'] == '45s' for rule in rules))

    def test_unknown_owner_metadata_cannot_change_authorization(self):
        config = copy.deepcopy(self.config)
        config['team'] = 'another-team'
        with self.assertRaises(Rejected):
            self.validate(config)

    def test_rejects_image_from_unapproved_registry_and_tags(self):
        for image in ['nginx:latest', self.service['image_repository'] + ':main', 'evil.test/service@sha256:' + 'a' * 64]:
            with self.assertRaises(Rejected):
                validate(self.config, 'payment-service', self.service, self.policy, self.source, image)

    def test_endpoint_must_be_allowlisted_get_path(self):
        for endpoint in [{'path': 'http://production', 'method': 'GET'}, {'path': '/checkout', 'method': 'POST'},
                         {'path': '/checkout', 'method': 'GET', 'password': 'secret'}, {'path': '/health', 'method': 'GET'}]:
            config = copy.deepcopy(self.config)
            config['endpoints'] = [endpoint]
            with self.assertRaises(Rejected):
                self.validate(config)

    def test_rejects_duplicate_yaml_and_unsafe_constructor(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.yaml'
            for text in ['spec: 1\nspec: 2', '!!python/object/apply:os.system [echo unsafe]']:
                path.write_text(text)
                with self.assertRaises(Rejected):
                    read_yaml(path)

    def test_symlink_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'load-tests').mkdir()
            (root / 'private').write_text('secret')
            (root / 'load-tests/escape.js').symlink_to(root / 'private')
            with self.assertRaises(Rejected):
                bounded_path(root, 'load-tests/escape.js')

    def test_nested_product_config_is_allowed_but_parent_traversal_is_not(self):
        config = 'developer/examples/payment-service/load-tests/load-test-config.yaml'
        self.assertEqual(bounded_path(ROOT, config), ROOT / config)
        for name in ('developer/examples/payment-service/../payment-service/load-tests/load-test-config.yaml',
                     str(ROOT / config), 'infra/platform.yaml'):
            with self.subTest(name=name), self.assertRaises(Rejected):
                bounded_path(ROOT, name)

    def test_target_requires_exact_run_owned_host(self):
        ns = 'loadtest-payment-service-' + 'a' * 12
        validate_target(f'http://payment-service.{ns}.svc.cluster.local:8080', ns, 'payment-service')
        for target in ['http://production:8080', f'http://payment-service.{ns}.svc.cluster.local.evil:8080',
                       f'http://user@payment-service.{ns}.svc.cluster.local:8080', f'https://payment-service.{ns}.svc.cluster.local:8080']:
            with self.assertRaises(Rejected):
                validate_target(target, ns, 'payment-service')

    def test_render_has_no_tokens_and_cloud_is_spot_only(self):
        plan, policy = prepare('payment-service', '', '', True)
        docs = environment(plan, policy)
        self.assertEqual(docs[0]['metadata']['labels']['istio.io/dataplane-mode'], 'ambient')
        pod = job(plan, policy)[1]['spec']['template']['spec']
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertEqual(pod['nodeSelector']['eks.amazonaws.com/capacityType'], 'SPOT')
        self.assertIn('K6_OUT', [e['name'] for e in pod['containers'][0]['env']])
        self.assertTrue(any(d['kind'] == 'AuthorizationPolicy' for d in docs))

    def test_policies_enforce_spot_and_bounded_reaper(self):
        docs = platform(self.policy, 'engine:local')
        rules = next(d for d in docs if d['metadata']['name'] == 'engine-job-caps' and d['kind'] == 'ValidatingAdmissionPolicy')
        self.assertIn('SPOT', json.dumps(rules))
        cron = next(d for d in docs if d['kind'] == 'CronJob')
        self.assertEqual(cron['spec']['concurrencyPolicy'], 'Forbid')
        self.assertEqual(cron['spec']['jobTemplate']['spec']['activeDeadlineSeconds'], 150)

    def test_missing_or_corrupt_html_report_is_rejected(self):
        for logs in ('', '\nLOADTEST_HTML_BEGIN\ninvalid base64!\nLOADTEST_HTML_END\n',
                     '\nLOADTEST_HTML_BEGIN\n' + base64.b64encode(b'not HTML').decode() + '\nLOADTEST_HTML_END\n'):
            with self.subTest(logs=logs), self.assertRaises((RuntimeError, ValueError)):
                html_from_logs(logs)


class GitHubIdentityTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict('os.environ', {'GITHUB_TOKEN': 'test-token', 'GITHUB_RUN_ID': '42',
                                      'GITHUB_RUN_ATTEMPT': '2', 'GITHUB_SHA': 'a' * 40}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.settings = {'github_repository': 'acme/platform', 'github_branch': 'main'}
        self.run = {'repository': {'full_name': 'acme/platform'}, 'event': 'workflow_dispatch',
                    'head_branch': 'main', 'head_sha': 'a' * 40, 'run_attempt': 2,
                    'path': '.github/workflows/loadtest.yml@main',
                    'actor': {'id': 100}, 'triggering_actor': {'id': 200}}

    def test_rerun_uses_verified_triggering_actor(self):
        with patch('infra.engine.runtime.request_json', return_value=self.run) as request:
            self.assertEqual(actor(False, self.settings), '200')
        self.assertEqual(request.call_args.args[0],
                         'https://api.github.com/repos/acme/platform/actions/runs/42/attempts/2')

    def test_unapproved_workflow_context_is_denied(self):
        for key, value in [('repository', {'full_name': 'attacker/platform'}), ('event', 'pull_request'),
                           ('head_branch', 'feature'), ('head_sha', 'b' * 40), ('run_attempt', 1),
                           ('path', '.github/workflows/unapproved.yml@main'), ('triggering_actor', {})]:
            with self.subTest(key=key), patch('infra.engine.runtime.request_json', return_value={**self.run, key: value}):
                with self.assertRaises(Rejected):
                    actor(False, self.settings)

    def test_push_pipeline_uses_environment_approver_not_pusher(self):
        run = {**self.run, 'event': 'push', 'path': '.github/workflows/pipeline.yml@main'}
        approvals = [{'state': 'approved', 'environments': [{'name': 'loadtest'}], 'user': {'id': 300}}]
        with patch('infra.engine.runtime.request_json', side_effect=[run, approvals]) as request:
            self.assertEqual(actor(False, self.settings), '300')
        self.assertEqual(request.call_args.args[0], 'https://api.github.com/repos/acme/platform/actions/runs/42/approvals')

    def test_push_pipeline_without_single_approval_is_denied(self):
        run = {**self.run, 'event': 'push', 'path': '.github/workflows/pipeline.yml@main'}
        approved = {'state': 'approved', 'environments': [{'name': 'loadtest'}], 'user': {'id': 300}}
        for approvals in ([], [{**approved, 'state': 'rejected'}], [{**approved, 'environments': [{'name': 'other'}]}],
                          [approved, {**approved, 'user': {'id': 301}}]):
            with self.subTest(approvals=approvals), patch('infra.engine.runtime.request_json', side_effect=[run, approvals]):
                with self.assertRaises(Rejected):
                    actor(False, self.settings)

    def test_event_must_match_its_workflow(self):
        for event, path in [('push', '.github/workflows/loadtest.yml@main'),
                            ('workflow_dispatch', '.github/workflows/pipeline.yml@main')]:
            with self.subTest(event=event), patch('infra.engine.runtime.request_json',
                                                  return_value={**self.run, 'event': event, 'path': path}):
                with self.assertRaises(Rejected):
                    actor(False, self.settings)

    def test_missing_credentials_or_run_identity_is_denied_before_api_request(self):
        for key in ('GITHUB_TOKEN', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT'):
            with self.subTest(key=key), patch.dict('os.environ', {key: ''}), patch('infra.engine.runtime.request_json') as request:
                with self.assertRaises(Rejected):
                    actor(False, self.settings)
                request.assert_not_called()

    def test_repository_path_reads_config_from_service_subdirectory(self):
        service = {**read_yaml(ROOT / 'infra/services.yaml')['services']['payment-service'],
                   'owner_ids': ['200']}
        catalog = {'services': {'payment-service': service}}
        image = service['image_repository'] + '@sha256:' + 'b' * 64
        with tempfile.TemporaryDirectory() as checkout, \
                patch('infra.engine.runtime.read_yaml', side_effect=lambda path: catalog if Path(path).name == 'services.yaml' else read_yaml(path)), \
                patch('infra.engine.runtime.actor', return_value='200'), \
                patch('infra.engine.runtime.fetch_service', return_value=(Path(checkout), None)):
            config = Path(checkout, 'svc', 'load-tests')
            config.mkdir(parents=True)
            (config / 'load-test-config.yaml').write_text((ROOT / service['local_path'] / service['config_path']).read_text())
            for path, ok in [('svc', True), ('../svc', False), ('/svc', False)]:
                service['repository_path'] = path
                with self.subTest(path=path):
                    if ok:
                        self.assertEqual(prepare('payment-service', 'a' * 40, image)[0]['service'], 'payment-service')
                    else:
                        with self.assertRaises(Rejected):
                            prepare('payment-service', 'a' * 40, image)

    def test_unapproved_repository_url_is_denied_before_git(self):
        for url in ('https://github.com.evil.test/acme/service.git', 'https://user@github.com/acme/service.git',
                    'https://github.com/acme/service.git?token=secret', 'http://github.com/acme/service.git'):
            with self.subTest(url=url), patch('infra.engine.runtime.command') as git:
                with self.assertRaises(Rejected):
                    fetch_service({'repository': url}, 'a' * 40, False)
                git.assert_not_called()


class FakeKube:
    def __init__(self, failed=False, cleanup_failure=False, interrupt=False, measured_ms=30000, missing_html=False, oom=False):
        self.objects = {}
        self.calls = []
        self.failed, self.cleanup_failure, self.interrupt = failed, cleanup_failure, interrupt
        self.measured_ms = measured_ms
        self.missing_html = missing_html
        self.oom = oom
        self.objects[('configmap', 'loadtest-system', 'limits')] = {'data': {'sha256': hashlib.sha256(json.dumps(limits(), sort_keys=True).encode()).hexdigest()}}

    def call(self, *args, **kwargs):
        self.calls.append(args)
        if args[0] == 'rollout' and self.interrupt:
            raise KeyboardInterrupt
        if args[0] == 'logs':
            passed = not (self.failed and args[1] == 'job/load')
            data = {'state': {'testRunDurationMs': self.measured_ms}, 'metrics': {'http_reqs': {'values': {'count': 100}},
                'http_req_duration': {'thresholds': {'p(95)<250': {'ok': passed}}}}}
            logs = 'LOADTEST_SUMMARY=' + json.dumps(data) + '\n'
            if not self.missing_html:
                html = base64.b64encode(b'<!doctype html><html><body>k6 report fixture</body></html>').decode()
                logs += '\nLOADTEST_HTML_BEGIN\n' + html + '\nLOADTEST_HTML_END\n'
            return logs
        if args[:2] == ('delete', 'configmap'):
            self.objects.pop(('configmap', 'loadtest-system', args[2]), None)
        return ''

    def create(self, value):
        key = (value['kind'].lower(), value['metadata'].get('namespace'), value['metadata']['name'])
        if key in self.objects:
            raise RuntimeError('AlreadyExists')
        self.objects[key] = value

    def apply(self, values):
        for value in values:
            if value['kind'] == 'Job':
                value['status'] = {'conditions': [{'type': 'Complete', 'status': 'True'}]}
            self.objects[(value['kind'].lower(), value['metadata'].get('namespace'), value['metadata']['name'])] = value

    def get(self, resource, name=None, namespace=None, labels=None):
        if resource == 'pods':
            statuses = [{'name': 'service', 'restartCount': 1, 'state': {'running': {}},
                         'lastState': {'terminated': {'reason': 'OOMKilled', 'exitCode': 137}}}]
            return {'items': [{'metadata': {'name': 'target'}, 'status': {'containerStatuses': statuses}}] if self.oom else []}
        if resource in ('namespaces', 'jobs'):
            kind = 'namespace' if resource == 'namespaces' else 'job'
            return {'items': [v for (k, n, _), v in self.objects.items() if k == kind and (namespace is None or n == namespace)]}
        return self.objects.get((resource, namespace, name))

    def cleanup(self, namespace, seconds=120):
        if self.cleanup_failure:
            raise RuntimeError('cleanup timed out')
        self.objects = {k: v for k, v in self.objects.items() if k != ('namespace', None, namespace) and k[1] != namespace}


class LifecycleTests(unittest.TestCase):
    def execute(self, kube):
        with patch.dict('os.environ', {}, clear=True):
            plan, policy = prepare('payment-service', '', '', True)
        with tempfile.TemporaryDirectory() as output:
            executor = Executor(kube, plan, policy, output, True)
            code = executor.execute()
            report = json.loads((Path(output) / 'report.json').read_text())
            for filename in report.get('html_reports', []):
                self.assertIn(b'<html>', (Path(output) / filename).read_bytes())
        return code, report, kube

    def test_pass_and_cleanup(self):
        code, report, kube = self.execute(FakeKube())
        self.assertEqual((code, report['verdict'], report['cleanup']), (0, 'PASS', 'COMPLETE'))
        self.assertIsNone(kube.get('configmap', 'execution-slot', 'loadtest-system'))
        self.assertEqual(report['html_reports'], ['smoke.html', 'load.html'])

    def test_failed_threshold_still_cleans_up(self):
        code, report, _ = self.execute(FakeKube(failed=True))
        self.assertEqual((code, report['verdict'], report['cleanup']), (1, 'FAIL', 'COMPLETE'))
        self.assertIn('load.html', report['html_reports'])

    def test_target_oom_fails_even_when_k6_thresholds_pass_and_survives_cleanup(self):
        code, report, kube = self.execute(FakeKube(oom=True))
        self.assertEqual((code, report['verdict'], report['cleanup']), (1, 'FAIL', 'COMPLETE'))
        self.assertEqual(report['reason'], 'target container OOMKilled')
        self.assertTrue(report['target_pods'][0]['oom_killed'])
        self.assertEqual(report['target_pods'][0]['last_termination']['exitCode'], 137)
        self.assertIsNone(kube.get('configmap', 'execution-slot', 'loadtest-system'))

    def test_missing_html_fails_run_and_still_cleans_up(self):
        code, report, _ = self.execute(FakeKube(missing_html=True))
        self.assertEqual((code, report['verdict'], report['cleanup']), (1, 'INFRA_ERROR', 'COMPLETE'))

    def test_cleanup_failure_does_not_report_success_and_keeps_slot(self):
        code, report, kube = self.execute(FakeKube(cleanup_failure=True))
        self.assertEqual((code, report['verdict'], report['cleanup']), (1, 'PASS', 'FAILED'))
        self.assertIsNotNone(kube.get('configmap', 'execution-slot', 'loadtest-system'))

    def test_cancel_still_cleans_up(self):
        _, report, _ = self.execute(FakeKube(interrupt=True))
        self.assertEqual((report['verdict'], report['cleanup']), ('ABORTED', 'COMPLETE'))

    def test_shortened_run_is_invalid_even_with_passing_thresholds(self):
        code, report, _ = self.execute(FakeKube(measured_ms=15000))
        self.assertEqual((code, report['verdict'], report['cleanup']), (1, 'INVALID', 'COMPLETE'))

    def test_second_run_cannot_take_an_existing_slot(self):
        kube = FakeKube()
        kube.objects[('configmap', 'loadtest-system', 'execution-slot')] = {'data': {'run_id': 'someone-else'}}
        _, report, kube = self.execute(kube)
        self.assertEqual(report['cleanup'], 'NOT_REQUIRED')
        self.assertEqual(kube.get('configmap', 'execution-slot', 'loadtest-system')['data']['run_id'], 'someone-else')

    def test_policy_drift_rejected_before_provisioning(self):
        kube = FakeKube()
        kube.objects[('configmap', 'loadtest-system', 'limits')]['data']['sha256'] = 'wrong'
        _, report, kube = self.execute(kube)
        self.assertIn('installed platform policy differs', report['reason'])
        self.assertEqual(kube.get('namespaces')['items'], [])

    def test_reaper_bounds_forged_expiry_by_creation_time(self):
        kube = FakeKube()
        now = 1700000000
        kube.objects[('namespace', None, 'old')] = {'metadata': {'name': 'old', 'labels': {PREFIX + 'expires-at': str(now + 10000)},
            'creationTimestamp': datetime.datetime.fromtimestamp(now - 3601, datetime.timezone.utc).isoformat()}}
        reap(kube, limits(), now)
        self.assertIsNone(kube.get('namespace', 'old'))


class MonitorTests(unittest.TestCase):
    def test_sustained_breach_then_reset(self):
        clock = [0]
        ratio = [0.95]
        def query(expr):
            return 0 if 'timestamp' in expr or 'k6_' in expr else ratio[0]
        monitor = SafetyMonitor('', 'test', limits(), now=lambda: clock[0], query=query)
        self.assertIsNone(monitor.check())
        clock[0] = 29
        self.assertIsNone(monitor.check())
        clock[0] = 30
        self.assertIn('cpu', monitor.check())
        ratio[0] = 0.1
        self.assertIsNone(monitor.check())
        ratio[0], clock[0] = 0.95, 40
        self.assertIsNone(monitor.check())

    def test_missing_metrics_abort_fail_closed(self):
        clock = [0]
        def missing(_):
            raise RuntimeError('missing')
        monitor = SafetyMonitor('', 'test', limits(), now=lambda: clock[0], query=missing)
        self.assertIsNone(monitor.check())
        clock[0] = 31
        self.assertIn('telemetry', monitor.check())

    def test_alert_cannot_stop_unrelated_namespace(self):
        kube = FakeKube()
        with self.assertRaises(Rejected):
            abort(kube, {'status': 'firing', 'labels': {'alertname': 'LoadTestSafetyAbort', 'namespace': 'production', 'run_id': 'a' * 12}})
        self.assertFalse(kube.calls)


if __name__ == '__main__':
    unittest.main()
