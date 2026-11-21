"""Check the native cadence sender and independent reader on one small positive stock block."""

import argparse
import hashlib
import json
import tempfile
import time
from pathlib import Path

from cadence_transport import offer, read_stock
from native_workload import solve
from run_cohort import session_lock
from sv_reference import IMAGE, Reference


def run():
    with (
        session_lock(),
        Reference().running(peer=True, fast_rpc=True) as node,
        tempfile.TemporaryDirectory() as tmp,
    ):
        parent = node.rpc("generate", 105)[-1]
        parent_header = node.rpc("getblockheader", parent)
        block = solve(parent, 106, parent_header["time"] + 1, [])
        data = block.serialize()
        digest = hashlib.sha256(data).hexdigest()
        body = Path(tmp) / "stock-probe.block"
        body.write_bytes(data)
        endpoint = node.docker("port", node.container, "18444/tcp").splitlines()[0]
        port = int(endpoint.rsplit(":", 1)[1])
        events = []
        offer(body, digest, port, 106, events.append)
        deadline = time.monotonic() + 30
        while node.rpc("getbestblockhash") != block.hash:
            if time.monotonic() >= deadline:
                raise ValueError("stock did not select fixture")
            time.sleep(0.1)
        read = read_stock(port, 106, block.hash, len(data), digest)
        return {
            "regtest": True,
            "reference": IMAGE,
            "header_hex": data[:80].hex(),
            "parent": parent,
            "selected": block.hash,
            "wire_bytes": len(data),
            "sent_sha256": digest,
            "independent_read_sha256": read,
            "transport_events": events,
        }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("output", type=Path)
    args = p.parse_args()
    with args.output.open("x") as output:
        json.dump(run(), output, indent=2)
        output.write("\n")
