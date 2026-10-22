"""Prepare a dedicated local cluster without modifying existing clusters."""
import json
import secrets
import subprocess

from infra.engine.core import ROOT, limits
from infra.engine.runtime import Kube, command
from infra.engine.policies import platform

name = 'loadtest-engine'
if name not in command(['kind', 'get', 'clusters']).split():
    subprocess.run(['kind', 'create', 'cluster', '--name', name, '--config', str(ROOT / 'infra/kind/cluster.yaml')], check=True)
for image, context, dockerfile in [
    ('loadtest/payment-service:local', ROOT / 'developer/examples/payment-service', ROOT / 'developer/examples/payment-service/Dockerfile'),
    ('loadtest/engine:local', ROOT, ROOT / 'infra/Dockerfile')]:
    subprocess.run(['docker', 'build', '-t', image, '-f', str(dockerfile), str(context)], check=True)
    subprocess.run(['kind', 'load', 'docker-image', '--name', name, image], check=True)
image = limits()['k6_image']
subprocess.run(['docker', 'pull', image], check=True)
digest = image.split('@')[1]
tag = image.split('@')[0]
subprocess.run(['docker', 'tag', image, tag], check=True)
digests = json.loads(command(['docker', 'image', 'inspect', tag]))[0]['RepoDigests']
if not any(value.endswith('@' + digest) for value in digests):
    raise SystemExit('k6 image digest mismatch')
subprocess.run(['kind', 'load', 'docker-image', '--name', name, tag], check=True)
kube = Kube('kind-' + name)
objects = platform(limits(), 'loadtest/engine:local', local=True)
kube.apply(objects[:1])
kube.apply([{'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'guard-auth', 'namespace': 'loadtest-system'},
             'stringData': {'token': secrets.token_hex(32)}}])
kube.apply(objects[1:])
kube.call('rollout', 'status', 'deployment/guard', '-n', 'loadtest-system', '--timeout=120s', timeout=150)
print('Ready: make run-local')
