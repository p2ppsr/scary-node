"""Authenticate the two local go-alert-system reads and its node RPC receipts."""

import argparse
import base64
import hmac
import datetime
import json
import os
import re
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class ReceiverSync:
    """Read the maintained receiver's confirmed peer-sync events from its UTC log."""

    event = re.compile(
        rb"(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}).*successfully synced up to (\d+) from peer ([A-Za-z0-9]+)"
    )

    def __init__(self, path):
        self.path = path
        self.position = 0
        self.inode = None
        self.latest = {}
        self.lock = threading.Lock()

    def read(self):
        with self.lock:
            try:
                with self.path.open("rb") as stream:
                    stat = os.fstat(stream.fileno())
                    identity = (stat.st_dev, stat.st_ino)
                    if self.inode != identity or stat.st_size < self.position:
                        self.position = max(0, stat.st_size - (2 << 20))
                        self.inode = identity
                        self.latest = {}
                    stream.seek(self.position)
                    for line in stream:
                        if not line.endswith(b"\n"):
                            break
                        self.position += len(line)
                        hit = self.event.search(line)
                        if hit:
                            try:
                                when = datetime.datetime.strptime(
                                    hit[1].decode(), "%Y/%m/%d %H:%M:%S"
                                ).replace(tzinfo=datetime.timezone.utc)
                                sequence = int(hit[2])
                                if sequence > 0xffffffff:
                                    raise ValueError("sequence outside receiver range")
                            except ValueError:
                                self.latest = {}
                                continue
                            self.latest = {
                                "sequence": sequence,
                                "unix_s": int(when.timestamp()),
                                "peer": hit[3].decode(),
                            }
            except OSError:
                return {}
            return dict(self.latest)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Handler(BaseHTTPRequestHandler):
    def forward(self, expected, target, data=None, token=None):
        supplied = self.headers.get("Authorization", "")
        if not supplied.isascii() or not hmac.compare_digest(supplied, expected):
            self.send_error(401)
            return
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        try:
            request = urllib.request.Request(target, data=data, headers=headers)
            with self.server.upstream.open(request, timeout=6) as reply:
                result = reply.read()
                if self.path == "/health" and self.server.sync:
                    payload = json.loads(result)
                    if not isinstance(payload, dict):
                        raise ValueError("invalid receiver health object")
                    payload["peer_sync"] = self.server.sync.read()
                    result = json.dumps(payload).encode()
        except urllib.error.HTTPError as error:
            error.close()
            self.send_error(503, "local service unavailable")
            return
        except (OSError, ValueError):
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
    p.add_argument(
        "--receiver-sync-log",
        type=Path,
        help="upstream UTC log containing successful peer synchronization events",
    )
    p.add_argument("--token-file", type=Path, required=True)
    p.add_argument("--operator-token-file", type=Path, required=True)
    p.add_argument("--port", type=int, default=19366)
    args = p.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.upstream = urllib.request.build_opener(NoRedirect())
    server.sync = (
        ReceiverSync(args.receiver_sync_log) if args.receiver_sync_log else None
    )
    server.receiver = args.receiver.rstrip("/")
    server.node = args.node.rstrip("/")
    server.operator_token = args.operator_token_file.read_text().rstrip("\r\n")
    password = args.token_file.read_text().strip()
    if any(not value or not value.isascii() or not value.isprintable() or value.strip() != value
           for value in (password, server.operator_token)):
        raise ValueError("expected a printable ASCII credential")
    server.bearer = "Bearer " + password
    server.basic = "Basic " + base64.b64encode(("scary:" + password).encode()).decode()
    server.serve_forever()
