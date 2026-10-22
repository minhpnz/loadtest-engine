import argparse
import json
import os
import sys

from .core import ROOT, Rejected, environment, job, limits
from .policies import platform
from .runtime import Executor, Kube, install_signal_handler, prepare, reap


def main():
    parser = argparse.ArgumentParser(description='Self-service EKS load testing')
    parser.add_argument('action', choices=['validate', 'render', 'run', 'install', 'reap', 'serve'])
    parser.add_argument('--service', default=os.environ.get('SERVICE', 'payment-service'))
    parser.add_argument('--ref', default=os.environ.get('SERVICE_REF', ''))
    parser.add_argument('--image', default=os.environ.get('SERVICE_IMAGE', ''))
    parser.add_argument('--context', default=os.environ.get('KUBE_CONTEXT', ''))
    parser.add_argument('--output', default='artifacts')
    parser.add_argument('--local', action='store_true')
    parser.add_argument('--in-cluster', action='store_true')
    parser.add_argument('--engine-image', default=os.environ.get('ENGINE_IMAGE'))
    args = parser.parse_args()
    if args.local and (os.environ.get('CI') or not args.context.startswith('kind-')):
        raise Rejected('local mode requires a kind-* context and cannot run in CI')
    if args.in_cluster:
        # Empty context uses the mounted in-cluster credential explicitly, never a laptop context.
        args.context = ''
    elif args.action not in ('validate', 'render') and not args.context:
        raise Rejected('explicit --context is required')
    kube = Kube(args.context)
    if args.action == 'install':
        if not args.engine_image:
            raise Rejected('--engine-image required for the independent reaper/guard')
        kube.apply(platform(limits(), args.engine_image, args.local))
        return 0
    if args.action == 'reap':
        reap(kube, limits())
        return 0
    if args.action == 'serve':
        from .webhook import serve
        serve(kube)
        return 0
    plan, policy = prepare(args.service, args.ref, args.image, args.local)
    if args.action == 'validate':
        print(json.dumps({k: v for k, v in plan.items() if k not in ('script', 'secrets')}, indent=2))
        return 0
    if args.action == 'render':
        print(json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': environment(plan, policy, args.local) + job(plan, policy, local=args.local)}, indent=2))
        return 0
    install_signal_handler()
    executor = Executor(kube, plan, policy, args.output, args.local)
    code = executor.execute()
    print(json.dumps({'verdict': executor.report['verdict'], 'cleanup': executor.report['cleanup'], 'report': str(executor.output / 'report.json')}))
    return code


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Rejected as error:
        print(f'rejected: {error}', file=sys.stderr)
        sys.exit(2)
