"""Forward only the invsqrt research interfaces from Docker to the local Workspace."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


ROUTES = {
    ('GET', '/api/agent/research'),
    ('GET', '/api/world/optimization/v1/history'),
    ('POST', '/api/world/optimization/v1/experiments'),
}
REQUEST_BYTES = 8192
RESPONSE_BYTES = 80000


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def make_handler(upstream_origin):
    opener = build_opener(ProxyHandler({}), NoRedirect())

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def _forward(self):
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc or (self.command, parsed.path) not in ROUTES:
                self.send_error(404)
                return

            body = None
            headers = {'Authorization': self.headers.get('Authorization', '')}
            if self.command == 'POST':
                raw_size = self.headers.get('Content-Length', '')
                if not raw_size.isascii() or not raw_size.isdecimal():
                    self.send_error(411)
                    return
                if len(raw_size) > 4 or int(raw_size) > REQUEST_BYTES:
                    self.send_error(413)
                    return
                self.connection.settimeout(6)
                body = self.rfile.read(int(raw_size))
                headers['Content-Type'] = self.headers.get('Content-Type', '')

            target = upstream_origin + parsed.path
            if parsed.query:
                target += '?' + parsed.query
            request = Request(target, data=body, headers=headers, method=self.command)
            try:
                response = opener.open(request, timeout=6)
            except HTTPError as exc:
                response = exc
            except OSError:
                self.send_error(502)
                return
            with response:
                data = response.read(RESPONSE_BYTES + 1)
                if len(data) > RESPONSE_BYTES:
                    self.send_error(502)
                    return
                self.send_response(response.status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        do_GET = _forward
        do_POST = _forward

    return Handler


if __name__ == '__main__':
    with ThreadingHTTPServer(('127.0.0.1', 8787), make_handler('http://host.docker.internal:8787')) as server:
        server.serve_forever()
