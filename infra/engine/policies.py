"""Server-side admission, permissions and independent namespace expiration."""
import hashlib
import json

from .core import GENERATOR_COMMAND, PREFIX, object_


def safety_rules(policy):
    cpu = f'(sum by(namespace) (rate(container_cpu_usage_seconds_total{{container="service",namespace=~"loadtest-.+"}}[30s])) / sum by(namespace) (kube_pod_container_resource_limits{{container="service",resource="cpu",namespace=~"loadtest-.+"}}) > {policy["cpu_abort_ratio"]})'
    cpu += ' * on(namespace) group_left(run_id) label_replace(kube_namespace_labels{label_loadtest_engine_managed="true"}, "run_id", "$1", "label_loadtest_engine_run", "(.+)")'
    error = f'sum by(namespace,run_id) (rate(k6_loadtest_errors_total[30s])) / clamp_min(sum by(namespace,run_id) (rate(k6_loadtest_requests_total[30s])), 0.000001) > {policy["error_abort_ratio"]}'
    return object_('PrometheusRule', 'loadtest-safety', 'monitoring', {'groups': [{'name': 'loadtest-safety', 'rules': [
        {'alert': 'LoadTestSafetyAbort', 'expr': expression, 'for': str(policy['abort_for_seconds']) + 's',
         'annotations': {'summary': summary}} for expression, summary in [(cpu, 'Target CPU safety limit exceeded'), (error, 'Test error safety limit exceeded')]]}]}, 'monitoring.coreos.com/v1')


def platform(policy, image=None, local=False):
    namespace = object_('Namespace', 'loadtest-system')
    caps = object_('ConfigMap', 'limits', 'loadtest-system', data={
        'sha256': hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest(),
        'maxVUs': str(policy['max_vus']), 'maxDuration': str(policy['max_duration_seconds']),
        'maxTTL': str(policy['max_environment_seconds']), 'k6Image': policy['k6_image']})
    group = 'loadtest-engine-executor'
    role = object_('ClusterRole', 'loadtest-engine-executor', api='rbac.authorization.k8s.io/v1', rules=[
        {'apiGroups': [''], 'resources': ['namespaces'], 'verbs': ['create', 'get', 'list', 'watch', 'delete']},
        {'apiGroups': [''], 'resources': ['configmaps', 'services', 'serviceaccounts', 'resourcequotas', 'limitranges'], 'verbs': ['get', 'list', 'create', 'patch', 'update', 'delete']},
        {'apiGroups': [''], 'resources': ['pods', 'pods/log'], 'verbs': ['get', 'list']},
        {'apiGroups': ['apps'], 'resources': ['deployments', 'replicasets'], 'verbs': ['create', 'get', 'list', 'watch', 'patch']},
        {'apiGroups': ['apps'], 'resources': ['daemonsets'], 'resourceNames': ['ztunnel'], 'verbs': ['get']},
        {'apiGroups': ['apiextensions.k8s.io'], 'resources': ['customresourcedefinitions'], 'resourceNames': ['externalsecrets.external-secrets.io', 'authorizationpolicies.security.istio.io'], 'verbs': ['get']},
        {'apiGroups': ['batch'], 'resources': ['jobs'], 'verbs': ['create', 'get', 'list', 'delete', 'patch']},
        {'apiGroups': ['networking.k8s.io'], 'resources': ['networkpolicies'], 'verbs': ['create', 'get', 'patch']},
        {'apiGroups': ['security.istio.io'], 'resources': ['authorizationpolicies'], 'verbs': ['create', 'get', 'patch']},
        {'apiGroups': ['external-secrets.io'], 'resources': ['externalsecrets'], 'verbs': ['create', 'get', 'patch']}])
    binding = object_('ClusterRoleBinding', group, api='rbac.authorization.k8s.io/v1',
        roleRef={'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'ClusterRole', 'name': group},
        subjects=[{'kind': 'Group', 'name': group, 'apiGroup': 'rbac.authorization.k8s.io'}])
    # Only the trusted platform job receives this identity. Human users have no kubeconfig.
    scope = object_('ValidatingAdmissionPolicy', 'engine-executor-scope', api='admissionregistration.k8s.io/v1', spec={
        'failurePolicy': 'Fail',
        'matchConstraints': {'resourceRules': [{'apiGroups': ['*'], 'apiVersions': ['*'], 'operations': ['CREATE', 'UPDATE', 'DELETE'], 'resources': ['*']}]},
        'matchConditions': [{'name': 'executor', 'expression': f"'{group}' in request.userInfo.groups"}],
        'variables': [{'name': 'item', 'expression': "request.operation == 'DELETE' ? oldObject : object"}],
        'validations': [
            {'expression': f"request.resource.resource == 'namespaces' ? (variables.item.metadata.name.startsWith('loadtest-') && has(variables.item.metadata.labels) && variables.item.metadata.labels['{PREFIX}managed'] == 'true') : (request.namespace == 'loadtest-system' ? (request.resource.resource == 'configmaps' && variables.item.metadata.name == 'execution-slot') : (namespaceObject != null && has(namespaceObject.metadata.labels) && namespaceObject.metadata.labels['{PREFIX}managed'] == 'true'))", 'message': 'executor may only manage run resources and the execution slot'},
            {'expression': f"request.resource.resource != 'namespaces' || request.operation == 'DELETE' || (has(object.metadata.labels) && object.metadata.labels['pod-security.kubernetes.io/enforce'] == 'restricted' && object.metadata.labels['{PREFIX}expires-at'].matches('^[0-9]{{10}}$'))", 'message': 'namespace must have restricted security and an expiration timestamp'},
            {'expression': f"request.operation != 'UPDATE' || request.resource.resource != 'namespaces' || object.metadata.labels == oldObject.metadata.labels", 'message': 'namespace ownership/expiry labels are immutable'},
            {'expression': "request.namespace != 'loadtest-system' || request.operation != 'UPDATE' || object.data == oldObject.data", 'message': 'execution slot cannot be extended'}]})
    pod = 'object.spec.template.spec'
    generator_image = policy['k6_image'].split('@')[0] if local else policy['k6_image']
    checks = [
        {'expression': f"{pod}.containers.size() == 1 && !has({pod}.initContainers)", 'message': 'one generator container only'},
        {'expression': f"{pod}.containers[0].image == '{generator_image}' && has({pod}.containers[0].command) && {pod}.containers[0].command == {json.dumps(GENERATOR_COMMAND)}", 'message': 'generator image/entrypoint are fixed'},
        {'expression': f"!has({pod}.containers[0].args) || {pod}.containers[0].args.size() == 0", 'message': 'generator arguments are fixed'},
        {'expression': f"!has({pod}.containers[0].envFrom) && {pod}.containers[0].env.all(e, e.name in ['TARGET','VUS','DURATION_S','K6_NEW_MACHINE_READABLE_SUMMARY','K6_OUT','K6_PROMETHEUS_RW_SERVER_URL','K6_PROMETHEUS_RW_PUSH_INTERVAL','K6_WEB_DASHBOARD','K6_WEB_DASHBOARD_EXPORT','K6_WEB_DASHBOARD_PORT','K6_WEB_DASHBOARD_PERIOD'])", 'message': 'unsupported generator environment override'},
        {'expression': f"{pod}.containers[0].env.exists_one(e, e.name == 'K6_NEW_MACHINE_READABLE_SUMMARY' && has(e.value) && e.value == 'false')", 'message': 'summary output contract is fixed for the pinned k6 version'},
        {'expression': f"{pod}.containers[0].env.exists_one(e, e.name == 'VUS' && has(e.value) && e.value.matches('^[0-9]{{1,4}}$') && int(e.value) >= 1 && int(e.value) <= {policy['max_vus']})", 'message': 'VU ceiling exceeded'},
        {'expression': f"{pod}.containers[0].env.exists_one(e, e.name == 'DURATION_S' && has(e.value) && e.value.matches('^[0-9]{{1,4}}$') && int(e.value) >= 1 && int(e.value) <= {policy['max_duration_seconds']})", 'message': 'duration ceiling exceeded'},
        {'expression': f"has(object.spec.activeDeadlineSeconds) && object.spec.activeDeadlineSeconds <= {policy['max_environment_seconds']} && object.spec.backoffLimit == 0", 'message': 'bounded deadline/no retries required'},
        {'expression': f"{pod}.automountServiceAccountToken == false && {pod}.serviceAccountName == 'workload'", 'message': 'generator must not receive Kubernetes credentials'}]
    for name, value in {'K6_WEB_DASHBOARD': 'true', 'K6_WEB_DASHBOARD_EXPORT': '/tmp/report.html',
                        'K6_WEB_DASHBOARD_PORT': '-1', 'K6_WEB_DASHBOARD_PERIOD': '1s'}.items():
        checks.append({'expression': f"{pod}.containers[0].env.exists_one(e, e.name == '{name}' && has(e.value) && e.value == '{value}')",
                       'message': 'HTML report configuration is fixed'})
    if not local:
        checks.append({'expression': f"has({pod}.nodeSelector) && {pod}.nodeSelector['eks.amazonaws.com/capacityType'] == 'SPOT' && {pod}.nodeSelector['role'] == 'loadtest'", 'message': 'generators must run on the dedicated Spot pool'})
    run_policy = object_('ValidatingAdmissionPolicy', 'engine-job-caps', api='admissionregistration.k8s.io/v1', spec={
        'failurePolicy': 'Fail', 'matchConstraints': {'resourceRules': [{'apiGroups': ['batch'], 'apiVersions': ['v1'], 'operations': ['CREATE', 'UPDATE'], 'resources': ['jobs']}]}, 'validations': checks})
    objects = [namespace, caps, role, binding, scope, run_policy]
    for name in ('engine-executor-scope', 'engine-job-caps'):
        spec = {'policyName': name, 'validationActions': ['Deny']}
        if name == 'engine-job-caps':
            spec['matchResources'] = {'namespaceSelector': {'matchLabels': {PREFIX + 'managed': 'true'}}}
        objects.append(object_('ValidatingAdmissionPolicyBinding', name, api='admissionregistration.k8s.io/v1', spec=spec))
    if image:
        for account in ('reaper', 'guard'):
            objects.append(object_('ServiceAccount', account, 'loadtest-system'))
        reaper_role = object_('ClusterRole', 'engine-reaper', api='rbac.authorization.k8s.io/v1', rules=[
            {'apiGroups': [''], 'resources': ['namespaces'], 'verbs': ['list', 'get', 'delete']},
            {'apiGroups': [''], 'resources': ['configmaps'], 'verbs': ['get', 'delete']}])
        objects += [reaper_role, object_('ClusterRoleBinding', 'engine-reaper', api='rbac.authorization.k8s.io/v1',
            roleRef={'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'ClusterRole', 'name': 'engine-reaper'}, subjects=[{'kind': 'ServiceAccount', 'namespace': 'loadtest-system', 'name': 'reaper'}])]
        guard_role = object_('ClusterRole', 'engine-guard', api='rbac.authorization.k8s.io/v1', rules=[
            {'apiGroups': [''], 'resources': ['namespaces'], 'verbs': ['get', 'patch']},
            {'apiGroups': ['batch'], 'resources': ['jobs'], 'verbs': ['get', 'list', 'delete']}])
        objects += [guard_role, object_('ClusterRoleBinding', 'engine-guard', api='rbac.authorization.k8s.io/v1',
            roleRef={'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'ClusterRole', 'name': 'engine-guard'}, subjects=[{'kind': 'ServiceAccount', 'namespace': 'loadtest-system', 'name': 'guard'}])]
        internal = object_('ValidatingAdmissionPolicy', 'engine-system-scope', api='admissionregistration.k8s.io/v1', spec={
            'failurePolicy': 'Fail', 'matchConstraints': {'resourceRules': [{'apiGroups': ['*'], 'apiVersions': ['*'], 'operations': ['UPDATE', 'DELETE'], 'resources': ['*']}]},
            'matchConditions': [{'name': 'system-account', 'expression': "request.userInfo.username in ['system:serviceaccount:loadtest-system:reaper', 'system:serviceaccount:loadtest-system:guard']"}],
            'variables': [{'name': 'item', 'expression': "request.operation == 'DELETE' ? oldObject : object"}],
            'validations': [{'expression': f"request.resource.resource == 'namespaces' ? (has(oldObject.metadata.labels) && oldObject.metadata.labels['{PREFIX}managed'] == 'true') : (request.namespace == 'loadtest-system' ? (request.resource.resource == 'configmaps' && variables.item.metadata.name == 'execution-slot') : (namespaceObject != null && has(namespaceObject.metadata.labels) && namespaceObject.metadata.labels['{PREFIX}managed'] == 'true'))", 'message': 'guard/reaper may only mutate managed runs or the execution slot'}]})
        objects += [internal, object_('ValidatingAdmissionPolicyBinding', 'engine-system-scope', api='admissionregistration.k8s.io/v1', spec={'policyName': 'engine-system-scope', 'validationActions': ['Deny']})]
        base = {'restartPolicy': 'Never', 'serviceAccountName': 'reaper', 'securityContext': {'runAsNonRoot': True, 'runAsUser': 65534, 'seccompProfile': {'type': 'RuntimeDefault'}},
            'containers': [{'name': 'reaper', 'image': image, 'args': ['reap', '--in-cluster'], 'resources': {'requests': {'cpu': '25m', 'memory': '64Mi'}, 'limits': {'cpu': '250m', 'memory': '128Mi'}},
            'securityContext': {'allowPrivilegeEscalation': False, 'capabilities': {'drop': ['ALL']}}}]}
        if not local:
            base['nodeSelector'] = {'role': 'system'}
        objects.append(object_('CronJob', 'reaper', 'loadtest-system', {'schedule': '* * * * *', 'concurrencyPolicy': 'Forbid',
            'successfulJobsHistoryLimit': 1, 'failedJobsHistoryLimit': 2, 'jobTemplate': {'spec': {'backoffLimit': 0, 'activeDeadlineSeconds': 150, 'template': {'spec': base}}}}, 'batch/v1'))
        guard = json.loads(json.dumps(base))
        guard.update({'serviceAccountName': 'guard', 'restartPolicy': 'Always'})
        guard['containers'][0].update({'name': 'guard', 'args': ['serve', '--in-cluster'], 'ports': [{'containerPort': 8080}],
            'env': [{'name': 'PREFLIGHT_TOKEN', 'valueFrom': {'secretKeyRef': {'name': 'guard-auth', 'key': 'token'}}}],
            'readinessProbe': {'httpGet': {'path': '/health', 'port': 8080}}})
        objects += [object_('Deployment', 'guard', 'loadtest-system', {'replicas': 1, 'selector': {'matchLabels': {'app': 'guard'}},
                    'template': {'metadata': {'labels': {'app': 'guard'}}, 'spec': guard}}, 'apps/v1'),
                    object_('Service', 'guard', 'loadtest-system', {'selector': {'app': 'guard'}, 'ports': [{'port': 8080, 'targetPort': 8080}]})]
    return objects
