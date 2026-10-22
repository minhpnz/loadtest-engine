"""Bounded Kubernetes execution, safety monitoring and durable reports."""
import json
import base64
import hashlib
import os
import re
import signal
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .core import ROOT, PREFIX, Rejected, bounded_path, environment, job, limits, read_yaml, validate, validate_target

# Approved entry points and the only event each may be started by.
# loadtest.yml: someone presses Run workflow. pipeline.yml: a push, gated by an environment approval.
WORKFLOWS = {'.github/workflows/loadtest.yml': 'workflow_dispatch', '.github/workflows/pipeline.yml': 'push'}
APPROVAL_ENVIRONMENT = 'loadtest'


def command(args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=kwargs.pop('timeout', 60), **kwargs).stdout


def request_json(url, headers=None, body=None):
    request = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json', **(headers or {})})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def actor(local, settings):
    if local:
        if os.environ.get('CI'):
            raise Rejected('--local is forbidden in CI')
        return 'local'
    token = os.environ.get('GITHUB_TOKEN', '')
    if not token:
        raise Rejected('GITHUB_TOKEN required; caller identity is verified with GitHub')
    run_id, attempt = os.environ.get('GITHUB_RUN_ID', ''), os.environ.get('GITHUB_RUN_ATTEMPT', '')
    if not run_id.isdecimal() or not attempt.isdecimal():
        raise Rejected('GitHub workflow run ID and attempt are required')
    repository = settings['github_repository']
    headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json'}
    runs = f'https://api.github.com/repos/{repository}/actions/runs/{run_id}'
    data = request_json(f'{runs}/attempts/{attempt}', headers)
    # Verify server-side run metadata, including the person who requested this attempt.
    workflow = data.get('path', '').split('@', 1)[0]
    if (data.get('repository', {}).get('full_name', '').lower() != repository.lower()
            or workflow not in WORKFLOWS or data.get('event') != WORKFLOWS[workflow]
            or data.get('head_branch') != settings['github_branch']
            or data.get('head_sha') != os.environ.get('GITHUB_SHA')
            or data.get('run_attempt') != int(attempt)):
        raise Rejected('execution must use the approved platform workflow/repository/branch')
    if data['event'] == 'push':
        # A push only proposes the run; the reviewer who approved the protected environment is
        # the person authorised against the catalog.
        approvers = {a.get('user', {}).get('id') for a in request_json(f'{runs}/approvals', headers)
                     if a.get('state') == 'approved'
                     and any(e.get('name') == APPROVAL_ENVIRONMENT for e in a.get('environments', []))}
        if len(approvers) != 1:
            raise Rejected(f'the {APPROVAL_ENVIRONMENT} environment needs exactly one approving reviewer for this run')
        who = approvers.pop()
    else:
        who = data.get('triggering_actor', {}).get('id')
    if not isinstance(who, int) or who <= 0:
        raise Rejected('GitHub did not return a verified triggering actor')
    return str(who)


class Kube:
    def __init__(self, context):
        self.context = context

    def call(self, *args, **kwargs):
        context = ['--context', self.context] if self.context else []
        return command(['kubectl', *context, '--cache-dir=/tmp/loadtest-kube-cache', '--request-timeout=20s', *args], **kwargs)

    def get(self, resource, name=None, namespace=None, labels=None):
        args = ['get', resource]
        if name:
            # Only for named reads: on a list, kubectl prints nothing instead of empty items.
            args += [name, '--ignore-not-found']
        if namespace:
            args += ['-n', namespace]
        if labels:
            args += ['-l', labels]
        args += ['-o', 'json']
        text = self.call(*args)
        return json.loads(text) if text.strip() else None

    def create(self, value):
        return self.call('create', '-f', '-', input=json.dumps(value))

    def apply(self, values):
        return self.call('apply', '-f', '-', input=json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': values}))

    def cleanup(self, namespace, seconds=120):
        self.call('delete', 'namespace', namespace, '--ignore-not-found', '--wait=true', f'--timeout={seconds}s', timeout=seconds + 30)
        if self.get('namespace', namespace):
            raise RuntimeError('namespace still exists after cleanup')


class SafetyMonitor:
    def __init__(self, url, namespace, policy, now=time.monotonic, query=None):
        self.url, self.ns, self.policy, self.now = url, namespace, policy, now
        self.query_fn = query
        self.since = {'cpu': None, 'errors': None, 'telemetry': None}

    def query(self, expr):
        data = request_json(self.url.rstrip('/') + '/api/v1/query?' + urllib.parse.urlencode({'query': expr}))
        if data.get('status') != 'success' or len(data['data']['result']) != 1:
            raise RuntimeError('missing/ambiguous safety telemetry')
        value = float(data['data']['result'][0]['value'][1])
        if not __import__('math').isfinite(value):
            raise RuntimeError('non-finite safety telemetry')
        return value

    def check(self):
        query = self.query_fn or self.query
        try:
            cpu = query(f'sum(rate(container_cpu_usage_seconds_total{{namespace="{self.ns}",container="service"}}[30s])) / sum(kube_pod_container_resource_limits{{namespace="{self.ns}",container="service",resource="cpu"}})')
            errors = query(f'sum(rate(k6_loadtest_errors_total{{namespace="{self.ns}"}}[30s])) / clamp_min(sum(rate(k6_loadtest_requests_total{{namespace="{self.ns}"}}[30s])), 0.000001)')
            cpu_age = query(f'time() - max(timestamp(container_cpu_usage_seconds_total{{namespace="{self.ns}",container="service"}}))')
            error_age = query(f'time() - max(timestamp(k6_loadtest_requests_total{{namespace="{self.ns}"}}))')
            if max(cpu_age, error_age) > self.policy['metrics_stale_seconds']:
                raise RuntimeError('stale safety telemetry')
            self.since['telemetry'] = None
        except Exception:
            self.since['cpu'] = self.since['errors'] = None
            if self.since['telemetry'] is None:
                self.since['telemetry'] = self.now()
            if self.now() - self.since['telemetry'] >= self.policy['metrics_stale_seconds']:
                return 'missing or stale safety telemetry'
            return None
        for name, breached in [('cpu', cpu > self.policy['cpu_abort_ratio']), ('errors', errors > self.policy['error_abort_ratio'])]:
            if not breached:
                self.since[name] = None
            elif self.since[name] is None:
                self.since[name] = self.now()
            elif self.now() - self.since[name] >= self.policy['abort_for_seconds']:
                return f'{name} exceeded safety threshold for {self.policy["abort_for_seconds"]} seconds'
        return None


def fetch_service(service, ref, local):
    if local:
        return (ROOT / service['local_path']).resolve(), None
    if not re.fullmatch(r'[a-f0-9]{40}', ref):
        raise Rejected('SERVICE_REF must be a complete 40-character commit SHA')
    url = urllib.parse.urlsplit(service['repository'])
    if (url.scheme != 'https' or url.netloc != 'github.com' or url.username or url.password
            or not re.fullmatch(r'/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git', url.path)
            or url.query or url.fragment):
        raise Rejected('catalog repository must use an approved HTTPS GitHub repository URL')
    directory = tempfile.TemporaryDirectory(prefix='loadtest-source-')
    env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null'}
    # Credentials travel in an environment-backed Git setting, never command arguments or artifacts.
    token = os.environ.get('SERVICE_REPOSITORY_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if not token:
        directory.cleanup()
        raise Rejected('a GitHub repository read token is required')
    auth = base64.b64encode(('x-access-token:' + token).encode()).decode()
    env.update({'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'http.https://github.com/.extraheader',
                'GIT_CONFIG_VALUE_0': f'Authorization: Basic {auth}'})
    try:
        command(['git', 'init', directory.name])
        command(['git', '-C', directory.name, 'fetch', '--depth=1', service['repository'], ref], env=env)
        command(['git', '-C', directory.name, 'checkout', '--detach', 'FETCH_HEAD'])
        if command(['git', '-C', directory.name, 'rev-parse', 'HEAD']).strip() != ref:
            raise Rejected('fetched commit does not match requested revision')
        return Path(directory.name), directory
    except BaseException:
        directory.cleanup()
        raise


def prepare(service_name, ref, image, local=False):
    policy = limits()
    catalog = read_yaml(ROOT / 'infra/services.yaml')['services']
    if service_name not in catalog:
        raise Rejected('service is not registered')
    service = catalog[service_name]
    who = actor(local, read_yaml(ROOT / 'infra/platform.yaml'))
    if not local and who not in [str(i) for i in service['owner_ids']]:
        raise Rejected('authenticated user does not own this service')
    source, handle = fetch_service(service, ref, local)
    try:
        config = read_yaml(bounded_path(source, service['config_path']))
        plan = validate(config, service_name, service, policy, source, image or service['local_image'], local)
    finally:
        if handle:
            handle.cleanup()
    run_id = uuid.uuid4().hex[:12]
    plan.update({'run_id': run_id, 'namespace': f'loadtest-{service_name}-{run_id}', 'owner': who, 'source_ref': ref,
                 'created_at': int(time.time()), 'platform_ref': os.environ.get('GITHUB_SHA', 'local')})
    plan['expires_at'] = plan['created_at'] + plan['ttl']
    validate_target(f'http://{service_name}.{plan["namespace"]}.svc.cluster.local:8080', plan['namespace'], service_name)
    return plan, policy


def summary_from_logs(text):
    values = [line.split('LOADTEST_SUMMARY=', 1)[1] for line in text.splitlines() if 'LOADTEST_SUMMARY=' in line]
    if len(values) != 1:
        raise RuntimeError('runner did not publish exactly one summary')
    return json.loads(values[0])


def html_from_logs(text):
    if text.count('\nLOADTEST_HTML_BEGIN\n') != 1 or text.count('\nLOADTEST_HTML_END\n') != 1:
        raise RuntimeError('runner did not publish exactly one HTML report')
    encoded = text.split('\nLOADTEST_HTML_BEGIN\n', 1)[1].split('\nLOADTEST_HTML_END\n', 1)[0]
    if len(encoded) > 6 * 1024 * 1024:
        raise RuntimeError('HTML report exceeds the transport limit')
    html = base64.b64decode(''.join(encoded.split()), validate=True)
    if not html or len(html) > 4 * 1024 * 1024 or b'<html' not in html[:1024].lower():
        raise RuntimeError('runner published an invalid HTML report')
    return html


class Executor:
    def __init__(self, kube, plan, policy, output, local=False):
        self.k, self.plan, self.policy = kube, plan, policy
        self.output, self.local = Path(output), local
        self.report = {k: v for k, v in plan.items() if k not in ('script', 'secrets')}
        self.report.update({'verdict': 'INFRA_ERROR', 'cleanup': 'NOT_STARTED', 'phases': {}})
        self.locked = False

    def collect_target_status(self):
        pods = self.k.get('pods', namespace=self.plan['namespace'], labels='app=' + self.plan['service']) or {}
        observed = {pod['name']: pod for pod in self.report.get('target_pods', [])}
        for pod in pods.get('items', []):
            for status in pod.get('status', {}).get('containerStatuses', []):
                if status['name'] != 'service':
                    continue
                name = pod['metadata']['name']
                current = status.get('state', {}).get('terminated', {})
                previous = status.get('lastState', {}).get('terminated', {})
                before = observed.get(name, {})
                observed[name] = {'name': name, 'restart_count': max(before.get('restart_count', 0), status.get('restartCount', 0)),
                                  'oom_killed': before.get('oom_killed', False) or any(
                                      value.get('reason') == 'OOMKilled' for value in (current, previous)),
                                  'last_termination': current or previous or before.get('last_termination', {})}
        self.report['target_pods'] = list(observed.values())

    def save(self):
        self.output.mkdir(parents=True, exist_ok=True)
        temporary = self.output / 'report.json.tmp'
        temporary.write_text(json.dumps(self.report, indent=2) + '\n')
        temporary.replace(self.output / 'report.json')
        measured = self.report.get('load', {}).get('metrics', {})
        latency = measured.get('http_req_duration', {}).get('values', {})
        errors = measured.get('http_req_failed', {}).get('values', {})
        requests = measured.get('http_reqs', {}).get('values', {})
        lines = [f'# Load test: {self.report["verdict"]}', '',
                 f'Service: `{self.plan["service"]}` · Run: `{self.plan["run_id"]}`', '',
                 f'Cleanup: **{self.report["cleanup"]}**', '',
                 '| Measurement | Value |', '|---|---|',
                 f'| p95 latency (ms) | {latency.get("p(95)", "unavailable")} |',
                 f'| Error ratio | {errors.get("rate", "unavailable")} |',
                 f'| Requests | {requests.get("count", "unavailable")} |',
                 f'| Requests/second | {requests.get("rate", "unavailable")} |', '',
                 f'Profile: {self.plan["profile_name"]}; {self.plan["vus"]} VUs; {self.plan["duration"]} seconds.']
        if self.report.get('reason'):
            lines += ['', 'Reason: ' + self.report['reason']]
        if self.report.get('target_pods'):
            lines += ['', 'Target container observations:', '', '| Pod | Restarts | OOMKilled observed |', '|---|---|---|']
            lines += [f'| {pod["name"]} | {pod["restart_count"]} | {pod["oom_killed"]} |'
                      for pod in self.report['target_pods']]
        if self.report.get('html_reports'):
            lines += ['', 'HTML reports: ' + ', '.join(f'`{name}`' for name in self.report['html_reports']) +
                      '. Download the artifacts and open these files in a browser.']
        (self.output / 'summary.md').write_text('\n'.join(lines) + '\n')

    def acquire(self):
        from .core import object_
        if self.k.get('namespaces', labels=PREFIX + 'managed=true')['items']:
            raise Rejected('a previous environment is still active; wait for verified cleanup')
        self.k.create(object_('ConfigMap', 'execution-slot', 'loadtest-system', data={
            'namespace': self.plan['namespace'], 'run_id': self.plan['run_id'], 'expires_at': str(self.plan['expires_at'])}))
        self.locked = True

    def verify_platform(self):
        caps = self.k.get('configmap', 'limits', 'loadtest-system')
        expected = hashlib.sha256(json.dumps(self.policy, sort_keys=True).encode()).hexdigest()
        if not caps or caps.get('data', {}).get('sha256') != expected:
            raise Rejected('installed platform policy differs; administrator must install the current policy first')
        if not self.local:
            self.k.call('get', 'daemonset', 'ztunnel', '-n', 'istio-system')
            self.k.call('get', 'crd', 'externalsecrets.external-secrets.io')
            self.k.call('get', 'crd', 'authorizationpolicies.security.istio.io')

    def stage(self, name):
        smoke = name == 'smoke'
        self.k.apply(job(self.plan, self.policy, smoke, self.local))
        start = time.monotonic()
        monitor = None if self.local or smoke else SafetyMonitor(read_yaml(ROOT / 'infra/platform.yaml')['prometheus_url'], self.plan['namespace'], self.policy)
        while int(time.time()) < self.plan['expires_at'] - self.policy['cleanup_seconds']:
            namespace = self.k.get('namespace', self.plan['namespace'])
            if not namespace:
                raise RuntimeError('environment was removed before completion')
            self.collect_target_status()
            reason = namespace.get('metadata', {}).get('annotations', {}).get(PREFIX + 'abort-reason')
            if not reason and monitor:
                reason = monitor.check()
            if reason:
                self.k.call('delete', 'job', name, '-n', self.plan['namespace'], '--ignore-not-found', '--wait=false')
                self.report.update({'verdict': 'ABORTED', 'reason': reason})
                return False
            resource = self.k.get('job', name, self.plan['namespace'])
            if not resource:
                raise RuntimeError('runner disappeared')
            conditions = resource.get('status', {}).get('conditions', [])
            finished = any(c['type'] in ('Complete', 'Failed') and c['status'] == 'True' for c in conditions)
            if finished:
                logs = self.k.call('logs', f'job/{name}', '-n', self.plan['namespace'])
                # Export through the existing pod-log permission before namespace teardown.
                plain_logs = re.sub(r'\nLOADTEST_HTML_BEGIN\n.*?\nLOADTEST_HTML_END\n', '\n', logs, flags=re.DOTALL)
                (self.output / f'{name}.log').write_text(plain_logs)
                (self.output / f'{name}.html').write_bytes(html_from_logs(logs))
                self.report.setdefault('html_reports', []).append(f'{name}.html')
                summary = summary_from_logs(logs)
                self.report[name] = summary
                metrics = summary.get('metrics', {})
                if metrics.get('http_reqs', {}).get('values', {}).get('count', 0) <= 0:
                    self.report.update({'verdict': 'INVALID', 'reason': 'no measured requests'})
                    return False
                expected_ms = (self.plan['smoke_seconds'] if smoke else self.plan['duration']) * 1000
                if summary.get('state', {}).get('testRunDurationMs', 0) < expected_ms - 1000:
                    self.report.update({'verdict': 'INVALID', 'reason': 'runner did not complete the requested active duration'})
                    return False
                failed = any(not t.get('ok', False) for m in metrics.values() for t in m.get('thresholds', {}).values())
                if not any(m.get('thresholds') for m in metrics.values()):
                    raise RuntimeError('summary has no evaluated thresholds')
                completed = any(c['type'] == 'Complete' and c['status'] == 'True' for c in conditions)
                self.report['phases'][name] = round(time.monotonic() - start, 3)
                oom = any(pod['oom_killed'] for pod in self.report.get('target_pods', []))
                if failed or oom:
                    self.report.update({'verdict': 'FAIL', 'reason': 'target container OOMKilled' if oom else f'{name} thresholds failed'})
                    return False
                if not completed:
                    raise RuntimeError('runner failed despite a passing summary')
                return True
            time.sleep(2)
        self.report.update({'verdict': 'ABORTED', 'reason': 'environment deadline reached'})
        return False

    def execute(self):
        self.output.mkdir(parents=True, exist_ok=True)
        self.save()
        try:
            self.verify_platform()
            self.acquire()
            if not self.local:
                settings = read_yaml(ROOT / 'infra/platform.yaml')
                token = os.environ.get('PREFLIGHT_TOKEN')
                if not token:
                    raise Rejected('PREFLIGHT_TOKEN is required for the validation webhook')
                request_json(settings['guard_url'] + '/validate', {'Authorization': 'Bearer ' + token}, body={
                    'namespace': self.plan['namespace'], 'service': self.plan['service'],
                    'target': f'http://{self.plan["service"]}.{self.plan["namespace"]}.svc.cluster.local:8080'})
            self.k.apply(environment(self.plan, self.policy, self.local))
            self.k.call('rollout', 'status', f'deployment/{self.plan["service"]}', '-n', self.plan['namespace'],
                        f'--timeout={self.policy["setup_seconds"]}s', timeout=self.policy['setup_seconds'] + 30)
            if self.stage('smoke') and self.stage('load'):
                self.report['verdict'] = 'PASS'
        except (KeyboardInterrupt, SystemExit):
            self.report.update({'verdict': 'ABORTED', 'reason': 'execution cancelled'})
        except Exception as exc:
            # Subprocess stderr can contain credentials/remote URLs; report only the operation type.
            self.report.update({'reason': str(exc) if isinstance(exc, Rejected) else type(exc).__name__})
        finally:
            if self.locked:
                try:
                    self.collect_target_status()
                    if self.report['verdict'] == 'PASS' and any(pod['oom_killed'] for pod in self.report.get('target_pods', [])):
                        self.report.update({'verdict': 'FAIL', 'reason': 'target container OOMKilled'})
                except Exception:
                    self.report['target_status_collection'] = 'UNAVAILABLE'
                try:
                    self.k.cleanup(self.plan['namespace'], self.policy['cleanup_seconds'])
                    self.report['cleanup'] = 'COMPLETE'
                    self.k.call('delete', 'configmap', 'execution-slot', '-n', 'loadtest-system', '--ignore-not-found')
                except Exception:
                    self.report['cleanup'] = 'FAILED'
            else:
                self.report['cleanup'] = 'NOT_REQUIRED'
            self.save()
            if not self.local:
                bucket = read_yaml(ROOT / 'infra/platform.yaml')['results_bucket']
                try:
                    command(['aws', 's3', 'cp', str(self.output), f's3://{bucket}/{self.plan["team"]}/{self.plan["run_id"]}/', '--recursive', '--only-show-errors'], timeout=60)
                    self.report['artifact_upload'] = 'COMPLETE'
                    self.save()
                    command(['aws', 's3', 'cp', str(self.output / 'report.json'), f's3://{bucket}/{self.plan["team"]}/{self.plan["run_id"]}/report.json', '--only-show-errors'], timeout=60)
                except Exception:
                    self.report['artifact_upload'] = 'FAILED'
                self.save()
        return 0 if (self.report['verdict'] == 'PASS' and self.report['cleanup'] == 'COMPLETE'
                     and (self.local or self.report.get('artifact_upload') == 'COMPLETE')) else 1


def reap(kube, policy, now=None):
    now = int(time.time()) if now is None else now
    for namespace in kube.get('namespaces', labels=PREFIX + 'managed=true')['items']:
        import datetime
        created = int(datetime.datetime.fromisoformat(namespace['metadata']['creationTimestamp'].replace('Z', '+00:00')).timestamp())
        label = namespace['metadata']['labels'].get(PREFIX + 'expires-at', '')
        deadline = min(int(label) if label.isdecimal() else created + policy['max_environment_seconds'], created + policy['max_environment_seconds'])
        if now >= deadline:
            kube.cleanup(namespace['metadata']['name'], policy['cleanup_seconds'])
    slot = kube.get('configmap', 'execution-slot', 'loadtest-system')
    if slot and now >= int(slot['data']['expires_at']) and not kube.get('namespace', slot['data']['namespace']):
        kube.call('delete', 'configmap', 'execution-slot', '-n', 'loadtest-system', '--ignore-not-found')


def install_signal_handler():
    def cancelled(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, cancelled)
