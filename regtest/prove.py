"""Drive two real regression nodes and a Scary Node worker. Python 3 + OpenSSL."""
import argparse
import base64
import hashlib
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

PORT_SHIFT = int(os.environ.get("SCARY_LAB_PORT_SHIFT", "0"))
NODES = [(19441 + PORT_SHIFT, 19451 + PORT_SHIFT), (19442 + PORT_SHIFT, 19452 + PORT_SHIFT)]
API = f"http://127.0.0.1:{19480 + PORT_SHIFT}"
AUTH = "Basic " + base64.b64encode(b"regtest:disposable-regtest-rpc").decode()
ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


def dsha(data):
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def txid(raw):
    return dsha(raw)[::-1].hex()


def request(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{url}: HTTP {exc.code}: {exc.read().decode(errors='replace')[:500]}") from exc


def rpc(node, method, *params):
    raw = request(f"http://127.0.0.1:{node[0]}/", json.dumps({
        "id": 1, "method": method, "params": list(params),
    }).encode(), {"Authorization": AUTH, "Content-Type": "application/json"})
    msg = json.loads(raw)
    if msg.get("error"):
        raise RuntimeError(f"{method}: {msg['error']}")
    return msg["result"]


def rejected_reason(raw, expected):
    req = urllib.request.Request(API + "/transactions", data=raw,
                                 headers={"Content-Type": "application/octet-stream"})
    try:
        with urllib.request.urlopen(req, timeout=10):
            raise AssertionError("entrant accepted a spend outside its observation boundary")
    except urllib.error.HTTPError as exc:
        reason = json.loads(exc.read()).get("error")
        if exc.code != 422 or reason != expected:
            raise RuntimeError(f"unexpected admission decision: {exc.code}, {reason}") from exc
        return reason


def status():
    s = json.loads(request(API + "/status"))
    if s["network"] != "regtest":
        raise RuntimeError("proof driver requires a regtest entrant")
    return s


def wait(label, action, seconds=120):
    deadline = time.monotonic() + seconds
    last = "condition false"
    while time.monotonic() < deadline:
        try:
            result = action()
            if result:
                return result
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            last = str(exc)
        time.sleep(0.5)
    raise RuntimeError(f"timeout waiting for {label}: {last}")


def read_varint(raw, offset):
    lead = raw[offset]
    length = {253: 2, 254: 4, 255: 8}.get(lead, 0)
    if length:
        return int.from_bytes(raw[offset + 1:offset + 1 + length], "little"), offset + 1 + length
    return lead, offset + 1


def parse_tx(raw, start):
    count, pos = read_varint(raw, start + 4)
    for _ in range(count):
        size, pos = read_varint(raw, pos + 36)
        pos += size + 4
    count, pos = read_varint(raw, pos)
    outputs = []
    for _ in range(count):
        amount = struct.unpack_from("<Q", raw, pos)[0]
        size, pos = read_varint(raw, pos + 8)
        outputs.append((amount, raw[pos:pos + size]))
        pos += size
    pos += 4
    if pos > len(raw):
        raise ValueError("truncated transaction from reference node")
    return raw[start:pos], outputs, pos


def block(node, block_hash):
    raw = request(f"http://127.0.0.1:{node[1]}/api/v1/block_legacy/{block_hash}?wire=1")
    return decode_block(raw, block_hash)


def decode_block(raw, block_hash):
    if txid(raw[:80]) != block_hash:
        raise ValueError("reference header hash mismatch")
    count, pos = read_varint(raw, 80)
    txs, outputs = [], []
    for _ in range(count):
        tx, vout, pos = parse_tx(raw, pos)
        txs.append(tx)
        outputs.append(vout)
    if pos != len(raw) or not txs:
        raise ValueError("reference returned an incomplete block")
    layer = [dsha(tx) for tx in txs]
    while len(layer) > 1:
        if len(layer) % 2:
            layer.append(layer[-1])
        layer = [dsha(layer[i] + layer[i + 1]) for i in range(0, len(layer), 2)]
    if layer[0] != raw[36:68]:
        raise ValueError("reference body does not match its Merkle root")
    return raw, txs, outputs


def openssl(*args, data=None):
    return subprocess.run(["openssl", *args], input=data, capture_output=True, check=True).stdout


class DisposableWallet:
    def __init__(self, path):
        self.path = str(path)
        openssl("ecparam", "-name", "secp256k1", "-genkey", "-noout", "-out", self.path)
        self.pubkey = openssl("ec", "-in", self.path, "-pubout", "-conv_form", "compressed", "-outform", "DER")[-33:]
        if self.pubkey[0] not in (2, 3):
            raise ValueError("unsupported public key encoding")
        pkh = hashlib.new("ripemd160", hashlib.sha256(self.pubkey).digest()).digest()
        self.script = b"\x76\xa9\x14" + pkh + b"\x88\xac"
        payload = b"\x6f" + pkh
        v = int.from_bytes(payload + dsha(payload)[:4], "big")
        address = ""
        while v:
            v, n = divmod(v, 58)
            address = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"[n] + address
        self.address = address

    def spend(self, parent_txid, value, fee):
        version = struct.pack("<I", 1)
        outpoint = bytes.fromhex(parent_txid)[::-1] + bytes(4)
        sequence = b"\xff" * 4
        script_code = bytes([len(self.script)]) + self.script
        output = struct.pack("<Q", value - fee) + script_code
        hash_type = 0x41  # SIGHASH_ALL | FORKID
        digest = dsha(version + dsha(outpoint) + dsha(sequence) + outpoint + script_code
                      + struct.pack("<Q", value) + sequence + dsha(output) + bytes(4)
                      + struct.pack("<I", hash_type))
        unlocking = self.unlocking_script(digest, hash_type)
        return version + b"\x01" + outpoint + bytes([len(unlocking)]) + unlocking + sequence + b"\x01" + output + bytes(4)

    def unlocking_script(self, digest, hash_type=0x41):
        sig = openssl("pkeyutl", "-sign", "-inkey", self.path, data=digest)
        if sig[:1] != b"\x30" or sig[2] != 2 or sig[4 + sig[3]] != 2:
            raise ValueError("OpenSSL returned an unexpected DER signature")
        r = int.from_bytes(sig[4:4 + sig[3]], "big")
        s = int.from_bytes(sig[6 + sig[3]:], "big")
        def der_int(i):
            b = i.to_bytes((i.bit_length() + 7) // 8, "big")
            if b[0] & 128:
                b = b"\x00" + b
            return b"\x02" + bytes([len(b)]) + b
        pair = der_int(r) + der_int(min(s, ORDER - s))
        signature = b"\x30" + bytes([len(pair)]) + pair + bytes([hash_type])
        return bytes([len(signature)]) + signature + bytes([len(self.pubkey)]) + self.pubkey


def compose(*args, capture=False):
    base = ["docker", "compose", "-f", str(Path(__file__).with_name("compose.yaml"))]
    if os.environ.get("SCARY_LAB_SCENARIO") == "1":
        base += ["-f", str(Path(__file__).with_name("scenario.compose.yaml"))]
    if os.environ.get("SCARY_LAB_RACES") == "1":
        base += ["-f", str(Path(__file__).with_name("races.compose.yaml"))]
    result = subprocess.run([*base, *args],
                            stdout=subprocess.PIPE if capture else sys.stderr, text=True, check=True)
    return result.stdout


def assert_tip(node, expected):
    return rpc(node, "getblockchaininfo")["bestblockhash"] == expected


def solve(worker, parent, child):
    with socket.create_connection(("127.0.0.1", 19433 + PORT_SHIFT), timeout=60) as sock, sock.makefile("rwb") as stream:
        def send(i, method, params):
            stream.write(json.dumps({"id": i, "method": method, "params": params}).encode() + b"\n")
            stream.flush()
        def receive():
            msg = json.loads(stream.readline())
            if msg.get("error"):
                raise RuntimeError(f"Stratum refused request: {msg['error']}")
            return msg
        send(1, "mining.subscribe", ["scary-node-regtest"])
        send(2, "mining.authorize", [worker, "regtest"])
        subscription, authorized, job = None, False, None
        while subscription is None or not authorized or job is None:
            msg = receive()
            if msg.get("method") == "mining.notify":
                job = msg["params"]
            elif msg.get("id") == 1:
                subscription = msg["result"]
            elif msg.get("id") == 2:
                authorized = msg["result"] is True
                if not authorized:
                    raise RuntimeError("Stratum worker unauthorized")
        job_id, prev_words, cb1, cb2, branches, version, bits, ntime, _ = job
        prev = bytes.fromhex(prev_words)
        prev = b"".join(prev[i:i + 4][::-1] for i in range(0, 32, 4))
        if prev[::-1].hex() != parent:
            raise ValueError("job parent differs from witnessed tip")
        # Rust's ordered pending map uses raw SHA digest bytes as its key.
        ids = [child] if isinstance(child, str) else child
        layer = [bytes(32)] + sorted(bytes.fromhex(t)[::-1] for t in ids)
        expected_branches = []
        while len(layer) > 1:
            if len(layer) % 2:
                layer.append(layer[-1])
            expected_branches.append(layer[1].hex())
            layer = [dsha(layer[i] + layer[i + 1]) for i in range(0, len(layer), 2)]
        if branches != expected_branches:
            raise ValueError(f"unexpected job Merkle branch: {branches}")
        _, extranonce1, size = subscription
        extra = bytes(size).hex()
        coinbase = bytes.fromhex(cb1 + extranonce1 + extra + cb2)
        root = dsha(coinbase)
        for branch in branches:
            root = dsha(root + bytes.fromhex(branch))
        prefix = (bytes.fromhex(version)[::-1] + prev + root
                  + bytes.fromhex(ntime)[::-1] + bytes.fromhex(bits)[::-1])
        compact = int(bits, 16)
        target = (compact & 0x7fffff) << (8 * ((compact >> 24) - 3))
        if compact != 0x207fffff:
            raise ValueError("refusing to run CPU worker outside ordinary regtest difficulty")
        for nonce in range(2**32):
            header = prefix + struct.pack("<I", nonce)
            if int.from_bytes(dsha(header), "little") <= target:
                break
        else:
            raise RuntimeError("job nonce space exhausted")
        send(3, "mining.submit", [worker, job_id, extra, ntime, f"{nonce:08x}"])
        while True:
            msg = receive()
            if msg.get("id") == 3:
                if msg.get("result") is not True:
                    raise RuntimeError(f"Stratum solution rejected: {msg}")
                return job_id, header, coinbase


def ready():
    for node in NODES:
        info = wait(f"reference RPC {node[0]}", lambda: rpc(node, "getblockchaininfo"))
        if info.get("chain") != "regtest":
            raise RuntimeError(f"expected regtest reference RPC, got {info.get('chain')}")
    wait("entrant connects to both native peers", lambda: status()["peer"]["connected_peer_ids_seen"] >= 2)
    native_ids = ("12D3KooWAFXWuxgdJoRsaA4J4RRRr8yu6WCrAPf8FaS7UfZg3ceG",
                  "12D3KooWG6aCkDmi5tqx4G4AvVDTQdSVvTSzzQvk1vh9CtSR8KEW")
    for node, other in zip(NODES, reversed(native_ids)):
        wait(f"reference {node[0]} sees peer {other}",
             lambda: any(peer.get("peerid") == other for peer in rpc(node, "getpeerinfo")))
    print("Both reference RPC services and entrant API are ready.", file=sys.stderr)


def prove():
    a, b = NODES
    with tempfile.TemporaryDirectory() as temp:
        wallet = DisposableWallet(Path(temp) / "key.pem")
        # The restarted entrant keeps its transport identity; every chain observation
        # lives in memory, so it starts with no UTXO set or prior block observations.
        compose("stop", "scary-node")
        try:
            funding_coin = rpc(a, "generatetoaddress", 1, wallet.address)[0]
            _, txs, outputs = wait("coinbase data", lambda: block(a, funding_coin))
            coin = txid(txs[0])
            value, lock = outputs[0][0]
            if lock != wallet.script:
                raise ValueError("funding coinbase paid a different script")
            # Advance only after both independent stores have each preceding block.
            for _ in range(100):
                matured_tip = rpc(a, "generate", 1)[0]
                wait("reference maturity blocks", lambda: assert_tip(b, matured_tip))
        finally:
            compose("start", "scary-node")
        wait("fresh entrant native peers", lambda: status()["peer"]["connected_peer_ids_seen"] >= 2)
        wait("entrant joins at existing tip", lambda: status()["mining"]["tip_hash"] == matured_tip)
        joined = status()["mining"]
        # The first observed block itself pays one P2PKH coinbase. Its output is
        # in the fresh observed set; the much older funding coinbase is absent.
        if joined["headers"] != 1 or joined["witnessed_outputs"] != 1:
            raise RuntimeError(f"unexpected entrant initial observation window: {joined}")
        print(f"Entrant joined at pre-existing height {joined['height']} with one observed header.", file=sys.stderr)
        funding_tx = wallet.spend(coin, value, 1000)
        historical_rejection = rejected_reason(funding_tx, "input missing from witnessed spendable set")
        funding_id = rpc(a, "sendrawtransaction", funding_tx.hex())
        if funding_id != txid(funding_tx):
            raise ValueError("reference RPC funding txid mismatch")
        wait("reference assembled funding transaction", lambda: rpc(a, "getminingcandidate")["num_tx"] >= 1)
        funding_block = rpc(a, "generate", 1)[0]
        funding_raw, txs, _ = wait("funding block body", lambda: block(a, funding_block))
        if funding_tx not in txs:
            raise ValueError("funding transaction was not mined")
        wait("entrant sees funding", lambda: status()["mining"]["tip_hash"] == funding_block)
        child = wallet.spend(funding_id, value - 1000, 1000)
        child_id = txid(child)
        before_successor = rejected_reason(child, "creation block has no observed successor")
        wait("second reference adopts funding block", lambda: assert_tip(b, funding_block))
        wait("second reference ready to extend funding",
             lambda: rpc(b, "getminingcandidate")["prevhash"] == funding_block)
        parent = rpc(b, "generate", 1)[0]
        wait("entrant sees successor", lambda: status()["mining"]["tip_hash"] == parent)
        wait("first reference adopts successor", lambda: assert_tip(a, parent))
        parent_raw, _, _ = wait("creating block successor body", lambda: block(b, parent))
        if parent_raw[4:36][::-1].hex() != funding_block:
            raise ValueError("funding successor extends a different block")
        admitted = json.loads(request(API + "/transactions", child, {"Content-Type": "application/octet-stream"}))
        if admitted["txid"] != child_id:
            raise ValueError("entrant admission txid mismatch")
        print(f"Worker received admitted child {child_id}.", file=sys.stderr)
        job, header, cb = solve("regtest-worker", parent, child_id)
        solved_hash = txid(header)
        native_publish = wait("entrant native block announcement",
                              lambda: status()["peer"]["last_published"] == solved_hash)
        results = []
        candidate = header + b"\x02" + cb + child
        for name, node in zip(("tn1", "tn2"), NODES):
            wait(f"{name} adopts Rust block {solved_hash}", lambda: assert_tip(node, solved_hash))
            raw, txs, _ = wait(f"{name} serves solved body", lambda: block(node, solved_hash))
            if raw != candidate or txs != [cb, child]:
                raise ValueError(f"{name} body differs from Stratum candidate")
            info = rpc(node, "getblock", solved_hash, 1)
            marker = f"Processing block {solved_hash} from peer "
            lines = compose("logs", "--no-color", "--no-log-prefix", name, capture=True).splitlines()
            sources = sorted({line.split(marker, 1)[1].split(" at ", 1)[1].strip()
                              for line in lines if marker in line and " at " in line.split(marker, 1)[1]})
            results.append({"node": name, "height": info["height"], "hash": solved_hash,
                            "native_block_sources": sources,
                            "body_sha256": hashlib.sha256(raw).hexdigest(), "transaction_count": len(txs)})
        print(f"Both references adopted {solved_hash} and served the identical body.", file=sys.stderr)
        wait("second reference ready to extend candidate",
             lambda: rpc(b, "getminingcandidate")["prevhash"] == solved_hash)
        descendant = rpc(b, "generate", 1)[0]
        wait("both references adopt successor", lambda: all(assert_tip(n, descendant) for n in NODES))
        next_block, _, _ = wait("successor body", lambda: block(b, descendant))
        if next_block[4:36][::-1].hex() != solved_hash:
            raise ValueError("successor did not extend exact Rust block")
        requests = set()
        for line in compose("logs", "--no-color", "--no-log-prefix", "scary-node", capture=True).splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if item.get("event") == "asset_request" and item.get("block") == solved_hash:
                requests.add(item["asset"])
        if not requests:
            raise RuntimeError("no candidate Asset fetch observed at entrant")
        print(json.dumps({"observed_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                          "network": "regtest", "teranode_release": "v0.16.1", "funding_coinbase": coin,
                          "entrant_join_height": joined["height"], "entrant_join_tip": matured_tip,
                          "entrant_join_observed_headers": joined["headers"],
                          "entrant_join_witnessed_outputs": joined["witnessed_outputs"],
                          "prejoin_funding_input_rejection": historical_rejection,
                          "child_before_successor_rejection": before_successor,
                          "funding_transaction_hex": funding_tx.hex(), "funding_output_index": 0,
                          "funding_output_value_satoshis": value - 1000,
                          "funding_tx": funding_id, "funding_block": funding_block, "parent": parent,
                          "funding_block_hex": funding_raw.hex(), "parent_header_hex": parent_raw[:80].hex(),
                          "funding_block_miner": "tn1", "parent_miner": "tn2",
                          "child_transaction_hex": child.hex(), "child_fee_satoshis": 1000,
                          "child": child_id, "stratum_job": job, "solved_header": header.hex(),
                          "solved_hash": solved_hash, "native_announcement_verified": native_publish,
                          "candidate_block_hex": candidate.hex(), "candidate_asset_requests": sorted(requests),
                          "successor_header_hex": next_block[:80].hex(), "independent_receipts": results,
                          "successor": descendant}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ready", action="store_true", help="check reference RPC and entrant API without mining")
    args = parser.parse_args()
    ready()
    if not args.ready:
        prove()
