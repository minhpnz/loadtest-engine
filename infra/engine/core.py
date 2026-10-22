"""Validate untrusted requests and render platform-owned Kubernetes resources."""
import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import yaml

ROOT = Path(__file__).resolve().parents[2]
PREFIX = 'loadtest.engine/'
GENERATOR_COMMAND = ['/bin/sh', '/scripts/run.sh']
GENERATOR_RUNNER = '''#!/bin/sh
k6 run /scripts/test.js
result=$?
if [ -s /tmp/report.html ] && [ "$(wc -c < /tmp/report.html)" -le 4194304 ]; then
  printf '\\nLOADTEST_HTML_BEGIN\\n'
  base64 /tmp/report.html
  printf '\\nLOADTEST_HTML_END\\n'
fi
exit "$result"
'''


class Rejected(ValueError):
    pass


class UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node, deep=False):
    result = {}
    for key, value in node.value:
        key = loader.construct_object(key, deep=deep)
        if key in result:
            raise Rejected(f'duplicate YAML key: {key}')
        result[key] = loader.construct_object(value, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def read_yaml(path):
    path = Path(path)
    if path.stat().st_size > 128 * 1024:
        raise Rejected('configuration exceeds 128 KiB')
    try:
        value = yaml.load(path.read_text(), Loader=UniqueLoader)
    except (yaml.YAMLError, TypeError) as exc:
        raise Rejected(f'invalid YAML: {exc}') from exc
    if not isinstance(value, dict):
        raise Rejected('configuration must be a mapping')
    return value


def limits():
    policy = json.loads((ROOT / 'infra/limits.tfvars.json').read_text())['limits']
    if not (1 <= policy['max_vus'] <= 5000 and 1 <= policy['max_duration_seconds'] <= 2700
            and 1 <= policy['max_environment_seconds'] <= 3600 and policy['max_concurrent_runs'] == 1):
        raise Rejected('platform ceilings cannot exceed 5000 VUs, 45 minutes, 60 minutes or one active run')
    return policy


def cpu_value(value):
    return float(value[:-1]) / 1000 if value.endswith('m') else float(value)


def memory_value(value):
    return int(value[:-2]) * (1024 if value.endswith('Gi') else 1)


def bounded_path(root, name):
    root = Path(root).resolve()
    relative = Path(name)
    directory = root / relative.parent
    path = (root / relative).resolve()
    if (relative.is_absolute() or '..' in relative.parts or relative.parent.name != 'load-tests'
            or not path.is_relative_to(directory) or not directory.resolve().is_relative_to(root) or not path.is_file()):
        raise Rejected('test files must resolve inside load-tests/')
    return path


def validate(config, service_name, service, policy, source, image, local=False):
    if set(config) != {'endpoints', 'thresholds'}:
        raise Rejected('developer configuration accepts only endpoints and thresholds')
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,30}', service_name):
        raise Rejected('invalid service name')
    profile_name = service['profile']
    if profile_name not in policy['profiles']:
        raise Rejected('profile is not enabled by the platform')
    profile = policy['profiles'][profile_name]
    if profile.get('parallelism') != 1:
        raise Rejected('only one generator is supported in this release')
    for key in ('generator_cpu', 'generator_request_cpu', 'service_cpu', 'service_request_cpu', 'namespace_cpu'):
        if not isinstance(profile[key], str) or not re.fullmatch(r'(?:[1-9][0-9]*m|[1-9][0-9]*(?:\.[0-9]+)?)', profile[key]):
            raise Rejected(f'invalid positive CPU quantity: {key}')
    for key in ('generator_memory', 'generator_request_memory', 'service_memory', 'service_request_memory', 'namespace_memory'):
        if not isinstance(profile[key], str) or not re.fullmatch(r'[1-9][0-9]*(?:Mi|Gi)', profile[key]):
            raise Rejected(f'invalid positive memory quantity: {key}')
    for component in ('generator', 'service'):
        if cpu_value(profile[f'{component}_request_cpu']) > cpu_value(profile[f'{component}_cpu']) or memory_value(profile[f'{component}_request_memory']) > memory_value(profile[f'{component}_memory']):
            raise Rejected('resource requests must not exceed limits')
    if cpu_value(profile['generator_cpu']) + cpu_value(profile['service_cpu']) > cpu_value(profile['namespace_cpu']) or memory_value(profile['generator_memory']) + memory_value(profile['service_memory']) > memory_value(profile['namespace_memory']):
        raise Rejected('namespace quota cannot fit the generator and service')
    vus = profile['vus']
    if type(vus) is not int or not 1 <= vus <= min(policy['max_vus'], profile['max_vus']):
        raise Rejected('VUs exceed the approved profile/platform ceiling')
    duration, ramp = profile['duration_seconds'], profile['ramp_up_seconds']
    if type(duration) is not int or type(ramp) is not int or not 0 < ramp < duration <= min(policy['max_duration_seconds'], profile['max_duration_seconds']):
        raise Rejected('duration exceeds the profile or ramp-up is not shorter than duration')
    ttl = policy['setup_seconds'] + policy['smoke_seconds'] + duration + policy['cleanup_seconds'] + 30
    if ttl > policy['max_environment_seconds']:
        raise Rejected('execution and cleanup budget exceed the environment deadline')
    endpoints = config['endpoints']
    if not isinstance(endpoints, list) or not 1 <= len(endpoints) <= 20:
        raise Rejected('provide between 1 and 20 endpoints')
    for endpoint in endpoints:
        if not isinstance(endpoint, dict) or set(endpoint) != {'path', 'method'} or endpoint['method'] != 'GET':
            raise Rejected('each endpoint must contain a path and method GET')
        if not isinstance(endpoint['path'], str) or endpoint['path'] not in service['allowed_endpoints'] or not re.fullmatch(r'/[a-zA-Z0-9/_-]*', endpoint['path']):
            raise Rejected('endpoint is not allowlisted for this service')
    thresholds = config['thresholds']
    if not isinstance(thresholds, dict) or set(thresholds) != {'http_req_duration', 'http_req_failed'}:
        raise Rejected('latency and error thresholds are required')
    for metric, values in thresholds.items():
        pattern = r'p\((90|95|99)\)<[1-9][0-9]{0,5}' if metric == 'http_req_duration' else r'rate<0\.[0-9]{1,6}'
        if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], str) or not re.fullmatch(pattern, values[0]):
            raise Rejected(f'invalid threshold: {metric}')
    if not local and not re.fullmatch(re.escape(service['image_repository']) + r'@sha256:[a-f0-9]{64}', image):
        raise Rejected('image must be a digest from the service-approved repository')
    if local and image != service['local_image']:
        raise Rejected('local image must match the catalog')
    import hashlib
    return {'service': service_name, 'team': service['team'], 'profile': profile, 'profile_name': profile_name,
            'config_sha256': hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest(),
            'vus': vus, 'duration': duration, 'ramp': ramp, 'ttl': ttl, 'smoke_seconds': policy['smoke_seconds'], 'thresholds': thresholds,
            'image': image, 'endpoints': endpoints,
            'port': service['port'], 'health_path': service['health_path'], 'secrets': service.get('secrets', [])}


def validate_target(target, namespace, service):
    url = urlsplit(target)
    expected = f'{service}.{namespace}.svc.cluster.local'
    if url.scheme != 'http' or url.hostname != expected or url.username or url.password or url.port != 8080 or url.query or url.fragment or url.path not in ('', '/'):
        raise Rejected('target must be the exact run-owned sandbox service endpoint')


def wrapper(plan, smoke=False):
    vus, duration = (1, plan['smoke_seconds']) if smoke else (plan['vus'], plan['duration'])
    stages = [{'duration': f'{duration}s', 'target': 1}] if smoke else [
        {'duration': f"{plan['ramp']}s", 'target': vus},
        {'duration': f"{duration - plan['ramp']}s", 'target': vus}]
    options = {'scenarios': {'main': {'executor': 'ramping-vus', 'startVUs': 1, 'stages': stages, 'gracefulRampDown': '0s', 'gracefulStop': '5s'}},
               'thresholds': {**plan['thresholds'], 'checks': ['rate>0.99']},
               'tags': {'run_id': plan['run_id'], 'namespace': plan['namespace']}}
    return ("import http from 'k6/http';\nimport {check} from 'k6';\nimport {Counter} from 'k6/metrics';\n"
            "const requests = new Counter('loadtest_requests');\nconst errors = new Counter('loadtest_errors');\n"
            f"const endpoints = {json.dumps(plan['endpoints'])};\n"
            f'export const options = {json.dumps(options)};\n'
            'export default function () {\n'
            " const endpoint = endpoints[__ITER % endpoints.length];\n const response = http.get(__ENV.TARGET + endpoint.path, {timeout: '5s', redirects: 0});\n"
            ' requests.add(1);\n errors.add(response && response.status >= 200 && response.status < 300 ? 0 : 1);\n'
            " check(response, {'business response is successful': r => r && r.status >= 200 && r.status < 300});\n}\n"
            "export function handleSummary(data) { return {'stdout': 'LOADTEST_SUMMARY=' + JSON.stringify(data) + '\\n'}; }\n")


def object_(kind, name, namespace=None, spec=None, api='v1', **extra):
    value = {'apiVersion': api, 'kind': kind, 'metadata': {'name': name}}
    if namespace:
        value['metadata']['namespace'] = namespace
    if spec is not None:
        value['spec'] = spec
    return {**value, **extra}


def environment(plan, policy, local=False):
    ns, svc = plan['namespace'], plan['service']
    profile = plan['profile']
    labels = {PREFIX + 'managed': 'true', PREFIX + 'run': plan['run_id'], PREFIX + 'team': plan['team'],
              PREFIX + 'expires-at': str(plan['expires_at']), 'pod-security.kubernetes.io/enforce': 'restricted'}
    if not local:
        labels['istio.io/dataplane-mode'] = 'ambient'
    resources = [object_('Namespace', ns), object_('ServiceAccount', 'workload', ns, automountServiceAccountToken=False)]
    resources[0]['metadata']['labels'] = labels
    resources += [object_('ResourceQuota', 'run', ns, {'hard': {'limits.cpu': profile['namespace_cpu'], 'limits.memory': profile['namespace_memory'],
                   'pods': str(profile['pods']), 'count/jobs.batch': '2', 'services.loadbalancers': '0', 'services.nodeports': '0'}}),
                  object_('LimitRange', 'run', ns, {'limits': [{'type': 'Container', 'default': {'cpu': '250m', 'memory': '128Mi'},
                    'defaultRequest': {'cpu': '50m', 'memory': '64Mi'}, 'max': {'cpu': max((profile['generator_cpu'], profile['service_cpu']), key=cpu_value), 'memory': max((profile['generator_memory'], profile['service_memory']), key=memory_value)}}]}),
                  object_('NetworkPolicy', 'deny', ns, {'podSelector': {}, 'policyTypes': ['Ingress', 'Egress']}, 'networking.k8s.io/v1'),
                  object_('NetworkPolicy', 'within-run', ns, {'podSelector': {}, 'policyTypes': ['Ingress', 'Egress'],
                    'ingress': [{'from': [{'podSelector': {}}]}], 'egress': [{'to': [{'podSelector': {}}]},
                     {'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'kube-system'}}, 'podSelector': {'matchLabels': {'k8s-app': 'kube-dns'}}}],
                      'ports': [{'protocol': 'UDP', 'port': 53}, {'protocol': 'TCP', 'port': 53}]}]}, 'networking.k8s.io/v1')]
    security = {'runAsNonRoot': True, 'runAsUser': 65534, 'seccompProfile': {'type': 'RuntimeDefault'}}
    container = {'name': 'service', 'image': plan['image'], 'imagePullPolicy': 'IfNotPresent',
                 'ports': [{'containerPort': plan['port']}], 'resources': {'requests': {'cpu': profile['service_request_cpu'], 'memory': profile['service_request_memory']}, 'limits': {'cpu': profile['service_cpu'], 'memory': profile['service_memory']}},
                 'securityContext': {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']}},
                 'readinessProbe': {'httpGet': {'path': plan['health_path'], 'port': plan['port']}, 'periodSeconds': 2}}
    if plan['secrets']:
        if local:
            raise Rejected('AWS secret injection requires the cloud profile')
        resources.append(object_('ExternalSecret', 'service', ns, {'refreshInterval': '1h', 'secretStoreRef': {'name': plan['team'], 'kind': 'ClusterSecretStore'},
             'target': {'name': 'service', 'creationPolicy': 'Owner'}, 'data': [{'secretKey': s['key'], 'remoteRef': {'key': s['arn']}} for s in plan['secrets']]}, 'external-secrets.io/v1'))
        container['envFrom'] = [{'secretRef': {'name': 'service'}}]
    resources += [object_('Deployment', svc, ns, {'replicas': 1, 'selector': {'matchLabels': {'app': svc}},
                   'template': {'metadata': {'labels': {'app': svc}}, 'spec': {'serviceAccountName': 'workload', 'automountServiceAccountToken': False,
                    'securityContext': security, 'terminationGracePeriodSeconds': 5, 'containers': [container]}}}, 'apps/v1'),
                  object_('Service', svc, ns, {'selector': {'app': svc}, 'ports': [{'port': 8080, 'targetPort': plan['port']}]})]
    if not local:
        resources.append(object_('NetworkPolicy', 'ambient-health', ns, {'podSelector': {'matchLabels': {'app': svc}}, 'policyTypes': ['Ingress'],
            'ingress': [{'from': [{'ipBlock': {'cidr': '169.254.7.127/32'}}], 'ports': [{'protocol': 'TCP', 'port': plan['port']}]}]}, 'networking.k8s.io/v1'))
        resources.append(object_('AuthorizationPolicy', 'run-only', ns, {'selector': {'matchLabels': {'app': svc}},
             'action': 'ALLOW', 'rules': [{'from': [{'source': {'namespaces': [ns]}}]}]}, 'security.istio.io/v1'))
        resources.append(object_('NetworkPolicy', 'observability', ns, {'podSelector': {}, 'policyTypes': ['Ingress', 'Egress'],
            'ingress': [{'from': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'monitoring'}}}]}],
            'egress': [{'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'monitoring'}}}], 'ports': [{'protocol': 'TCP', 'port': 9090}]},
                       {'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'istio-system'}}}], 'ports': [{'protocol': 'TCP', 'port': 15012}]}]}, 'networking.k8s.io/v1'))
    return resources


def job(plan, policy, smoke=False, local=False):
    ns = plan['namespace']
    name = 'smoke' if smoke else 'load'
    data = {'test.js': wrapper(plan, smoke), 'run.sh': GENERATOR_RUNNER}
    cm = object_('ConfigMap', f'script-{name}', ns, data=data)
    container = {'name': 'k6', 'image': policy['k6_image'].split('@')[0] if local else policy['k6_image'], 'imagePullPolicy': 'IfNotPresent',
       'command': GENERATOR_COMMAND.copy(), 'env': [{'name': 'TARGET', 'value': f'http://{plan["service"]}.{ns}.svc.cluster.local:8080'},
       {'name': 'K6_NEW_MACHINE_READABLE_SUMMARY', 'value': 'false'},
       {'name': 'K6_WEB_DASHBOARD', 'value': 'true'}, {'name': 'K6_WEB_DASHBOARD_EXPORT', 'value': '/tmp/report.html'},
       {'name': 'K6_WEB_DASHBOARD_PORT', 'value': '-1'}, {'name': 'K6_WEB_DASHBOARD_PERIOD', 'value': '1s'},
       {'name': 'VUS', 'value': str(1 if smoke else plan['vus'])}, {'name': 'DURATION_S', 'value': str(plan['smoke_seconds'] if smoke else plan['duration'])}],
       'resources': {'requests': {'cpu': plan['profile']['generator_request_cpu'], 'memory': plan['profile']['generator_request_memory']}, 'limits': {'cpu': plan['profile']['generator_cpu'], 'memory': plan['profile']['generator_memory']}},
       'securityContext': {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']}},
       'volumeMounts': [{'name': 'scripts', 'mountPath': '/scripts', 'readOnly': True}, {'name': 'tmp', 'mountPath': '/tmp'}]}
    pod = {'restartPolicy': 'Never', 'serviceAccountName': 'workload', 'automountServiceAccountToken': False,
       'terminationGracePeriodSeconds': 5, 'securityContext': {'runAsNonRoot': True, 'runAsUser': 12345, 'seccompProfile': {'type': 'RuntimeDefault'}},
       'containers': [container], 'volumes': [{'name': 'scripts', 'configMap': {'name': f'script-{name}'}}, {'name': 'tmp', 'emptyDir': {'sizeLimit': '32Mi'}}]}
    if not local:
        container['env'] += [{'name': 'K6_OUT', 'value': 'experimental-prometheus-rw'},
            {'name': 'K6_PROMETHEUS_RW_SERVER_URL', 'value': 'http://prometheus-operated.monitoring.svc.cluster.local:9090/api/v1/write'},
            {'name': 'K6_PROMETHEUS_RW_PUSH_INTERVAL', 'value': '5s'}]
        pod['nodeSelector'] = {'role': 'loadtest', 'eks.amazonaws.com/capacityType': 'SPOT'}
        pod['tolerations'] = [{'key': 'dedicated', 'operator': 'Equal', 'value': 'loadtest', 'effect': 'NoSchedule'}]
    deadline = min(plan['expires_at'] - int(time.time()) - policy['cleanup_seconds'], (plan['smoke_seconds'] if smoke else plan['duration']) + policy['setup_seconds'] + 10)
    if deadline <= 0:
        raise Rejected('environment has insufficient remaining time')
    resource = object_('Job', name, ns, {'backoffLimit': 0, 'activeDeadlineSeconds': deadline, 'ttlSecondsAfterFinished': 300,
        'template': {'metadata': {'labels': {PREFIX + 'run': plan['run_id']}}, 'spec': pod}}, 'batch/v1')
    return [cm, resource]
