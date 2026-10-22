"""Administrator-only installation of pinned cluster dependencies."""
import argparse
import json
import subprocess

from infra.engine.core import ROOT, limits, read_yaml
from infra.engine.policies import safety_rules

parser = argparse.ArgumentParser()
parser.add_argument('--context', required=True)
parser.add_argument('--token-file', required=True, help='File containing the shared guard token; never commit this file')
args = parser.parse_args()
versions = json.loads((ROOT / 'infra/addons/versions.json').read_text())


def run(command):
    subprocess.run(command, check=True)


for ns in ('loadtest-system', 'monitoring', 'istio-system', 'external-secrets'):
    value = {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': ns}}
    subprocess.run(['kubectl', '--context', args.context, 'apply', '-f', '-'], input=json.dumps(value), text=True, check=True)
for ns in ('loadtest-system', 'monitoring'):
    # Token bytes are passed via stdin, not printed or embedded in command arguments.
    import base64
    from pathlib import Path
    token = Path(args.token_file).read_bytes().strip()
    if len(token) < 32:
        raise SystemExit('guard token must be at least 32 bytes')
    secret = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'guard-auth', 'namespace': ns},
              'data': {'token': base64.b64encode(token).decode()}}
    subprocess.run(['kubectl', '--context', args.context, 'apply', '-f', '-'], input=json.dumps(secret), text=True, check=True)
for repo, url in [('istio', 'https://istio-release.storage.googleapis.com/charts'),
                  ('prometheus-community', 'https://prometheus-community.github.io/helm-charts'),
                  ('external-secrets', 'https://charts.external-secrets.io')]:
    run(['helm', 'repo', 'add', repo, url, '--force-update'])
run(['helm', 'repo', 'update'])
releases = [
    ('istio-base', 'istio/base', 'istio-system', versions['istio'], []),
    ('istiod', 'istio/istiod', 'istio-system', versions['istio'], ['--set', 'profile=ambient']),
    ('istio-cni', 'istio/cni', 'istio-system', versions['istio'], ['--set', 'profile=ambient']),
    ('ztunnel', 'istio/ztunnel', 'istio-system', versions['istio'], []),
    ('external-secrets', 'external-secrets/external-secrets', 'external-secrets', versions['external_secrets'], ['--set', 'serviceAccount.name=external-secrets']),
    ('monitoring', 'prometheus-community/kube-prometheus-stack', 'monitoring', versions['prometheus_stack'], ['-f', str(ROOT / 'infra/addons/monitoring.yaml'), '--set', f'prometheus.prometheusSpec.retention={limits()["retention_days"]}d'])]
for name, chart, namespace, version, extra in releases:
    run(['helm', '--kube-context', args.context, 'upgrade', '--install', name, chart, '--namespace', namespace,
         '--version', version, '--wait', '--timeout', '10m', *extra])
subprocess.run(['kubectl', '--context', args.context, 'apply', '-f', '-'], input=json.dumps(safety_rules(limits())), text=True, check=True)
store = read_yaml(ROOT / 'infra/addons/secret-store.yaml')
store['spec']['provider']['aws']['region'] = read_yaml(ROOT / 'infra/platform.yaml')['aws_region']
subprocess.run(['kubectl', '--context', args.context, 'apply', '-f', '-'], input=json.dumps(store), text=True, check=True)
