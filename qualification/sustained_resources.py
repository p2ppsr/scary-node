"""Sample resource use of the seven services in the registered native regtest project."""

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

SERVICES = ("as1", "as2", "tn1", "tn2", "svref", "scary-node", "ingress")
GIB = 1024**3
HOST_MIN = 20 * GIB
DOCKER_MIN = 12 * GIB
# Keep one shell per service. A new Docker exec per metric per second would
# unnecessarily load the same daemon whose native workload is under measurement.
PROBE = r"""
n=0
while :; do
    cpu=$(awk '$1 == "usage_usec" {print $2}' /sys/fs/cgroup/cpu.stat) || exit 1
    mem=$(cat /sys/fs/cgroup/memory.current) || exit 1
    peak=$(cat /sys/fs/cgroup/memory.peak) || exit 1
    free=-
    objects=-
    used=-
    case "$1" in
        as1|as2)
            free=$(df -Pk /opt/aerospike/data | awk 'END {print $4}')
            if [ $((n % 30)) -eq 0 ]; then
                properties=$(asinfo -v namespace/test)
                objects=$(printf '%s' "$properties" | tr ';' '\n' | awk -F= '$1 == "objects" {print $2}')
                used=$(printf '%s' "$properties" | tr ';' '\n' | awk -F= '$1 == "data_used_bytes" {print $2}')
            fi
            ;;
        svref)
            free=$(df -Pk /data | awk 'END {print $4}')
            ;;
    esac
    printf '%s %s %s %s %s %s\n' "$cpu" "$mem" "$peak" "$free" "$objects" "$used" || exit 0
    n=$((n+1))
    sleep 1
done
"""


def service_ids():
    project = os.getenv("COMPOSE_PROJECT_NAME", "scary-node-throughput")
    if project not in ("scary-node-throughput", "scary-node-sustained-production"):
        raise ValueError("unexpected sustained regtest project")
    lines = subprocess.check_output(
        [
            "docker",
            "ps",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--format",
            '{{.Label "com.docker.compose.service"}} {{.ID}}',
        ],
        text=True,
        timeout=6,
    )
    ids = dict(line.split() for line in lines.splitlines())
    if not set(SERVICES) <= ids.keys():
        raise RuntimeError(
            "registered resource sampler cannot locate all seven services"
        )
    return ids


def fields(line, elapsed):
    cpu, mem, peak, free_kib, objects, used = line.split()
    row = {
        "read_ns": elapsed,
        "cpu_usage_usec": int(cpu),
        "memory_bytes": int(mem),
        "memory_peak_bytes": int(peak),
    }
    if free_kib != "-":
        row["filesystem_free_bytes"] = int(free_kib) * 1024
    if objects != "-" and used != "-":
        row["namespace_objects"] = int(objects)
        row["namespace_data_bytes"] = int(used)
    return row


def sample_resources(
    output_path: Path,
    start_ns: int,
    stop_event: threading.Event,
    resource_stop: threading.Event,
):
    output_path = Path(output_path)
    latest = {}
    lock = threading.Lock()
    probes = {}
    readers = []

    def consume(name, proc):
        try:
            for line in proc.stdout:
                row = fields(line, time.monotonic_ns() - start_ns)
                with lock:
                    latest[name] = row
        except (ValueError, OSError) as error:
            with lock:
                latest[name] = {
                    "error": type(error).__name__,
                    "read_ns": time.monotonic_ns() - start_ns,
                }

    with output_path.open("x", buffering=1) as file:
        try:
            ids = service_ids()
            for name in SERVICES:
                p = subprocess.Popen(
                    ["docker", "exec", ids[name], "sh", "-c", PROBE, "sample", name],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
                probes[name] = p
                th = threading.Thread(target=consume, args=(name, p), daemon=True)
                th.start()
                readers.append(th)
            tick = max(0, (time.monotonic_ns() - start_ns) // 1_000_000_000)
            while not stop_event.is_set():
                if stop_event.wait(
                    max(
                        0, (start_ns + tick * 1_000_000_000 - time.monotonic_ns()) / 1e9
                    )
                ):
                    break
                elapsed = time.monotonic_ns() - start_ns
                host_free = shutil.disk_usage(output_path.parent).free
                with lock:
                    values = dict(latest)
                free = [
                    v["filesystem_free_bytes"]
                    for v in values.values()
                    if "filesystem_free_bytes" in v
                    and elapsed - v["read_ns"] <= 3_000_000_000
                ]
                docker_free = min(free, default=None)
                reason = None
                if host_free < HOST_MIN:
                    reason = "host data volume below 20 GiB free"
                elif docker_free is not None and docker_free < DOCKER_MIN:
                    reason = "Docker data volume below 12 GiB free"
                row = {
                    "elapsed_ns": elapsed,
                    "host_filesystem_free_bytes": host_free,
                    "docker_filesystem_free_bytes": docker_free,
                    "services": values,
                    "stop_reason": reason,
                }
                file.write(json.dumps(row, separators=(",", ":")) + "\n")
                if reason:
                    file.flush()
                    os.fsync(file.fileno())
                    resource_stop.set()
                    break
                tick = max(
                    tick + 1, (time.monotonic_ns() - start_ns) // 1_000_000_000 + 1
                )
        except Exception as error:
            # No private host paths or Docker configuration enter the public receipt.
            file.write(
                json.dumps(
                    {
                        "elapsed_ns": time.monotonic_ns() - start_ns,
                        "sampler_error": type(error).__name__,
                    }
                )
                + "\n"
            )
            resource_stop.set()
            raise
        finally:
            for p in probes.values():
                if p.poll() is None:
                    p.terminate()
            for p in probes.values():
                try:
                    p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
                p.stdout.close()
            for th in readers:
                th.join(timeout=2)
