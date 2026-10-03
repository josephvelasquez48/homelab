"""focusd: the camera's focus over HTTP, for the cam app's slider.

    GET /focus                        {"auto": false, "focus": 120, "min": 0, "max": 250, "step": 5}
    PUT /focus  {"auto": true}        autofocus on
    PUT /focus  {"focus": 120}        autofocus off at that focus

A thin wrapper around camctl (camctl.c), which does the USB side and
remembers the setting; this only turns HTTP into its command line. Same
login as MediaMTX's viewer (cam-app + ~/.config/homelab-cam/viewer-password),
since the cam app already holds it, and only from the addresses MediaMTX
lets that user in from (mediamtx.yml): the cluster's nodes and this Mac.

Runs as a LaunchAgent (local.homelab.camfocus, install.py) next to MediaMTX.
Standard library only: it runs on Homebrew's python3 with nothing installed.
"""
import base64
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socketserver
from pathlib import Path
import subprocess
import threading

PORT = 8890
USER = 'cam-app'
ALLOWED = {'192.168.1.253', '192.168.1.63', '127.0.0.1', '::1'}

config = Path(__file__).resolve().parent
camctl = str(config / 'camctl')
password = (config / 'viewer-password').read_text().strip()
expected = 'Basic ' + base64.b64encode(f'{USER}:{password}'.encode()).decode()
# One USB conversation at a time: a slider dragged quickly sends a burst.
lock = threading.Lock()


def run(*args: str) -> tuple[int, bytes]:
    with lock:
        done = subprocess.run([camctl, *args, '--json'], capture_output=True, timeout=10)
    return done.returncode, done.stdout if done.returncode == 0 else done.stderr


class Handler(BaseHTTPRequestHandler):
    def reply(self, status: int, body: bytes) -> None:
        if status != 200:
            body = json.dumps({'error': body.decode(errors='replace').strip()}).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def allowed(self) -> bool:
        if self.client_address[0] not in ALLOWED:
            self.reply(403, b'not from this address')
            return False
        if not hmac.compare_digest(self.headers.get('Authorization', ''), expected):
            self.reply(401, b'wrong login')
            return False
        if self.path != '/focus':
            self.reply(404, b'only /focus')
            return False
        return True

    def do_GET(self) -> None:
        if self.allowed():
            code, out = run('status')
            self.reply(200 if code == 0 else 503, out)

    def do_PUT(self) -> None:
        if not self.allowed():
            return
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get('Content-Length', 0)), 1024)))
        except ValueError:
            return self.reply(400, b'expected JSON')
        if not isinstance(body, dict):
            return self.reply(400, b'expected a JSON object')
        if body.get('auto') is True:
            value = 'auto'
        elif type(body.get('focus')) is int:
            value = str(body['focus'])
        else:
            return self.reply(400, b'expected {"auto": true} or {"focus": <number>}')
        code, out = run('set', value)
        # camctl: 2 is a value out of range, 1 the camera missing or refusing.
        self.reply({0: 200, 2: 400}.get(code, 503), out)

    def log_message(self, format: str, *args) -> None:
        pass  # a slider drag is dozens of lines of nothing; errors reach the app


class Server(ThreadingHTTPServer):
    def server_bind(self) -> None:
        # HTTPServer's own also looks up this machine's name (getfqdn), which
        # on this Mac waits ~40 s on mDNS before it starts listening.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


Server(('0.0.0.0', PORT), Handler).serve_forever()
