"""Drop one local Stratum response after forwarding a real regtest submission."""

import json
import socket
from concurrent.futures import ThreadPoolExecutor

from sustained_native import stratum
from verify_receipt import require


def relay(listener, upstream_port):
    def frame(stream):
        raw = stream.readline(8193)
        require(len(raw) <= 8192 and raw.endswith(b"\n"), "incomplete Stratum frame")
        return raw, json.loads(raw)

    client, _ = listener.accept()
    with (
        client,
        socket.create_connection(("127.0.0.1", upstream_port), timeout=10) as node,
        client.makefile("rb") as requests,
        node.makefile("rb") as replies,
    ):
        client.settimeout(10)
        for method in ("mining.subscribe", "mining.authorize"):
            raw, request = frame(requests)
            require(request["method"] == method, "unexpected setup request")
            node.sendall(raw)
        expected = {1, 2, "mining.set_difficulty", "mining.notify"}
        while expected:
            raw, reply = frame(replies)
            client.sendall(raw)
            expected.discard(reply.get("method") or reply.get("id"))
        raw, submission = frame(requests)
        require(submission["method"] == "mining.submit", "missing worker submission")
        node.sendall(raw)
        client.shutdown(socket.SHUT_WR)
        while True:
            _, reply = frame(replies)
            if reply.get("id") == submission["id"]:
                return submission, reply


def submit_without_reply(parent, upstream_port):
    slot = {
        "index": 0,
        "phase": "offer",
        "miner": "scary",
        "ready": True,
        "parent": parent,
    }
    with socket.socket() as listener, ThreadPoolExecutor(max_workers=1) as pool:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(10)
        exchange = pool.submit(relay, listener, upstream_port)
        try:
            stratum(parent, slot, port=listener.getsockname()[1])
        except EOFError as error:
            slot.update(outcome="error", error=str(error))
        else:
            raise ValueError("worker received a response after the relay closed")
        submission, reply = exchange.result()
    require(
        reply.get("result") is True and reply.get("error") is None,
        "node rejected submission",
    )
    return {"slot": slot, "forwarded_submit": submission, "upstream_reply": reply}
