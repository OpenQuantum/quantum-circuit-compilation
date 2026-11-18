import sys
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from invsqrt_loopback_relay import make_handler


class UpstreamHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def _reply(self):
        size = int(self.headers.get('Content-Length', 0))
        self.server.observed.append((self.command, self.path, self.headers.get('Authorization'), self.rfile.read(size)))
        self.send_response(201 if self.command == 'POST' else 200)
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    do_GET = _reply
    do_POST = _reply


@contextmanager
def listening(handler):
    with ThreadingHTTPServer(('127.0.0.1', 0), handler) as server:
        worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01})
        worker.start()
        try:
            yield server, f'http://127.0.0.1:{server.server_port}'
        finally:
            server.shutdown()
            worker.join(timeout=1)


class InvsqrtRelayTests(unittest.TestCase):
    def test_proxies_research_and_world_result_routes_only(self):
        with listening(UpstreamHandler) as (upstream, upstream_url):
            upstream.observed = []
            with listening(make_handler(upstream_url)) as (_, relay_url):
                paths = ['/api/agent/research?task=invsqrt', '/api/world/optimization/v1/history?task=invsqrt']
                for path in paths:
                    with urlopen(Request(relay_url + path, headers={'Authorization': 'Bearer reader'})) as response:
                        self.assertEqual((response.status, response.read()), (200, b'{"ok":true}'))
                result_path = '/api/world/optimization/v1/experiments'
                request = Request(relay_url + result_path, data=b'{"task":"invsqrt"}',
                                  headers={'Authorization': 'Bearer writer', 'Content-Type': 'application/json'})
                with urlopen(request) as response:
                    self.assertEqual(response.status, 201)

                with self.assertRaises(HTTPError) as unsupported:
                    urlopen(Request(relay_url + '/api/crawl', data=b'{}'))
                self.assertEqual(unsupported.exception.code, 404)
                with self.assertRaises(HTTPError) as oversized:
                    urlopen(Request(relay_url + result_path, data=b'x' * (8192 + 1)))
                self.assertEqual(oversized.exception.code, 413)

            self.assertEqual(upstream.observed, [
                ('GET', paths[0], 'Bearer reader', b''),
                ('GET', paths[1], 'Bearer reader', b''),
                ('POST', result_path, 'Bearer writer', b'{"task":"invsqrt"}'),
            ])


if __name__ == '__main__':
    unittest.main()
