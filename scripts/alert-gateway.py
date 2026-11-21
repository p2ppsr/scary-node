"""Authenticate the two local go-alert-system reads and its node RPC receipts."""

import argparse
import base64
import hmac
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class Handler(BaseHTTPRequestHandler):
    def forward(self, expected, target, data=None, token=None):
        if not hmac.compare_digest(self.headers.get("Authorization", ""), expected):
            self.send_error(401)
            return
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        try:
            request = urllib.request.Request(target, data=data, headers=headers)
            with urllib.request.urlopen(request, timeout=6) as reply:
                result = reply.read()
        except OSError:
            self.send_error(503, "local service unavailable")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(result)))
        self.end_headers()
        self.wfile.write(result)

    def do_GET(self):
        if self.path not in ("/alerts", "/health"):
            self.send_error(404)
            return
        self.forward(self.server.bearer, self.server.receiver + self.path)

    def do_POST(self):
        if self.path != "/":
            self.send_error(404)
            return
        try:
            n = int(self.headers.get("Content-Length", ""))
            if not 0 < n <= 4_000_000:
                raise ValueError()
        except ValueError:
            self.send_error(400)
            return
        self.forward(
            self.server.basic,
            self.server.node + "/alert-receipt",
            self.rfile.read(n),
            self.server.operator_token,
        )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--receiver", required=True)
    p.add_argument("--node", required=True)
    p.add_argument("--token-file", type=Path, required=True)
    p.add_argument("--operator-token-file", type=Path, required=True)
    p.add_argument("--port", type=int, default=19366)
    args = p.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.receiver = args.receiver.rstrip("/")
    server.node = args.node.rstrip("/")
    server.operator_token = args.operator_token_file.read_text().strip()
    password = args.token_file.read_text().strip()
    if not password or not server.operator_token:
        raise ValueError("empty credential")
    server.bearer = "Bearer " + password
    server.basic = "Basic " + base64.b64encode(("scary:" + password).encode()).decode()
    server.serve_forever()
