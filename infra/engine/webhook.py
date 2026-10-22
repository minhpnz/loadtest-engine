"""Authenticated preflight and Alertmanager stop endpoints."""
import hmac
import json
import os
import re
from http.server import BaseHTTPRequestHandler, HTTPServer

from .core import PREFIX, Rejected, validate_target


def abort(kube, alert):
    if alert.get('status') != 'firing' or alert.get('labels', {}).get('alertname') != 'LoadTestSafetyAbort':
        return False
    ns = alert['labels'].get('namespace', '')
    run = alert['labels'].get('run_id', '')
    if not re.fullmatch(r'loadtest-[a-z0-9-]+-[a-f0-9]{12}', ns) or not re.fullmatch(r'[a-f0-9]{12}', run):
        raise Rejected('invalid alert identity')
    namespace = kube.get('namespace', ns)
    if not namespace or namespace['metadata']['labels'].get(PREFIX + 'managed') != 'true' or namespace['metadata']['labels'].get(PREFIX + 'run') != run:
        raise Rejected('alert does not belong to the current managed run')
    kube.call('annotate', 'namespace', ns, PREFIX + 'abort-reason=safety alert', '--overwrite')
    for job in kube.get('jobs', namespace=ns)['items']:
        kube.call('delete', 'job', job['metadata']['name'], '-n', ns, '--ignore-not-found', '--wait=false')
    return True


def serve(kube):
    token = os.environ.get('PREFLIGHT_TOKEN', '')
    if len(token) < 32:
        raise Rejected('PREFLIGHT_TOKEN must be at least 32 characters')

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if self.path == '/health' else 404)
            self.end_headers()

        def do_POST(self):
            if not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token):
                self.send_error(401)
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 65536:
                    raise Rejected('invalid request size')
                data = json.loads(self.rfile.read(length))
                if self.path == '/validate':
                    if not re.fullmatch(r'loadtest-[a-z0-9-]+-[a-f0-9]{12}', data['namespace']):
                        raise Rejected('invalid namespace')
                    validate_target(data['target'], data['namespace'], data['service'])
                elif self.path == '/alerts':
                    for alert in data.get('alerts', []):
                        abort(kube, alert)
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok": true}')
            except (ValueError, KeyError, TypeError):
                self.send_error(400, 'request rejected')
            except Exception:
                self.send_error(503, 'guard action failed')

        def log_message(self, _format, *_args):
            pass  # Never emit authentication headers or alert payloads.

    HTTPServer(('0.0.0.0', 8080), Handler).serve_forever()
