"""Reference checkout service for comparing two commits under the same load."""
import json
import os
import time
from threading import local
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# The regression patch changes only this constant from 1 to 32.
REQUEST_MEMORY_MIB = 32
WORKSPACE = local()


def checkout():
    # Reuse a working buffer on this connection's worker thread. Touch every page
    # so its memory is resident. Closing the connection releases the thread cache.
    if not hasattr(WORKSPACE, 'buffer'):
        WORKSPACE.buffer = bytearray(REQUEST_MEMORY_MIB * 1024 * 1024)
        for offset in range(0, len(WORKSPACE.buffer), 4096):
            WORKSPACE.buffer[offset] = 1
    time.sleep(int(os.environ.get('RESPONSE_DELAY_MS', '150')) / 1000)
    return {'status': 'ok', 'payment': 'simulated', 'working_set_mib': len(WORKSPACE.buffer) // (1024 * 1024)}


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def do_GET(self):
        if self.path not in ('/health', '/checkout'):
            self.send_error(404)
            return
        payload = json.dumps(checkout() if self.path == '/checkout' else {'status': 'ok'}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format, *_args):
        pass


if __name__ == '__main__':
    # Each HTTP/1.1 keep-alive connection has a worker and its own reusable buffer.
    server = ThreadingHTTPServer(('0.0.0.0', 8080), Handler)
    server.daemon_threads = True
    server.serve_forever()
