import http.client
import importlib.util
import json
import tempfile
import threading
import unittest
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

path = Path(__file__).resolve().parents[1] / "scripts" / "alert-gateway.py"
spec = importlib.util.spec_from_file_location("alert_gateway", path)
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


@contextmanager
def serving(handler):
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join()


class AlertGatewayTest(unittest.TestCase):
    def test_sync_requires_a_complete_record_from_the_current_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "receiver.log"
            event = b"2026/11/22 06:01:00 successfully synced up to 5 from peer TestPeer"
            log.write_bytes(event)
            sync = gateway.ReceiverSync(log)
            self.assertEqual({}, sync.read())
            with log.open("ab") as file:
                file.write(b"\n")
            self.assertEqual(5, sync.read()["sequence"])
            log.rename(log.with_suffix(".old"))
            log.write_bytes(event.replace(b"06:01", b"29:01") + b"\n")
            self.assertEqual({}, sync.read())
            with log.open("ab") as file:
                file.write(event.replace(b"to 5", b"to 6") + b"\n")
            self.assertEqual(6, sync.read()["sequence"])

    def test_forwarding_uses_the_configured_endpoint(self):
        class Upstream(BaseHTTPRequestHandler):
            def do_GET(self):
                self.server.destinations.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(self.server.health)

            def do_POST(self):
                self.server.destinations.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/redirected")
                self.end_headers()

            def log_message(self, *_):
                pass

        class QuietGateway(gateway.Handler):
            def log_message(self, *_):
                pass

        with serving(Upstream) as upstream, serving(QuietGateway) as local:
            local.receiver = local.node = f"http://127.0.0.1:{upstream.server_port}"
            local.upstream = urllib.request.build_opener(gateway.NoRedirect())
            local.bearer, local.basic, local.operator_token = "Bearer lab", "Basic fixture", "operator"
            local.sync = gateway.ReceiverSync(Path("/unavailable/scary-receiver-log"))
            upstream.destinations = []
            client = http.client.HTTPConnection(*local.server_address, timeout=3)
            self.addCleanup(client.close)
            for body, expected in [(b'{"sequence":4}', 200), (b"[]", 503), (b"{", 503)]:
                upstream.health = body
                client.request("GET", "/health", headers={"Authorization": local.bearer})
                response = client.getresponse()
                self.assertEqual(expected, response.status)
                payload = response.read()
                if expected == 200:
                    self.assertEqual({"sequence": 4, "peer_sync": {}}, json.loads(payload))
            upstream.destinations.clear()
            client.request("POST", "/", body=b"{}", headers={"Authorization": local.basic})
            response = client.getresponse()
            self.assertEqual(503, response.status)
            response.read()
            self.assertEqual(["/alert-receipt"], upstream.destinations)
