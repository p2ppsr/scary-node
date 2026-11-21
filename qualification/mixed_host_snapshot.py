"""Read the post-trial host envelope and Linux cgroup and storage counters from the isolated project."""

import datetime
import json
import platform
import subprocess
from mixed_storage_trial import compose, docker


def snapshot():
    info = json.loads(docker("info", "--format", "{{json .}}"))
    host = {"system": platform.system(), "architecture": platform.machine()}
    if host["system"] == "Darwin":
        for name, key in {
            "model": "hw.model",
            "processor": "machdep.cpu.brand_string",
            "physical_cpu_cores": "hw.physicalcpu",
            "logical_cpus": "hw.logicalcpu",
            "memory_bytes": "hw.memsize",
        }.items():
            val = subprocess.check_output(["sysctl", "-n", key], text=True).strip()
            host[name] = int(val) if val.isdecimal() else val
        host["system_version"] = subprocess.check_output(
            ["sw_vers", "-productVersion"], text=True
        ).strip()
    services = {}
    for name in ("as1", "as2", "tn1", "tn2", "svref", "scary-node", "ingress"):
        cid = compose("ps", "-q", name)
        fields = docker(
            "exec",
            cid,
            "cat",
            "/sys/fs/cgroup/memory.current",
            "/sys/fs/cgroup/memory.peak",
        ).splitlines()
        result = {
            "cgroup_current_bytes": int(fields[0]),
            "cgroup_peak_bytes_since_container_start": int(fields[1]),
        }
        if name in ("as1", "as2"):
            props = dict(
                item.split("=", 1)
                for item in docker("exec", cid, "asinfo", "-v", "namespace/test").split(
                    ";"
                )
                if "=" in item
            )
            result["utxo_namespace"] = {
                k: int(props[k])
                for k in (
                    "objects",
                    "data_total_bytes",
                    "data_used_bytes",
                    "data_used_pct",
                    "data_avail_pct",
                )
            }
            size, blocks, blocksize = map(
                int,
                docker(
                    "exec",
                    cid,
                    "stat",
                    "-c",
                    "%s %b %B",
                    "/opt/aerospike/data/utxostore.dat",
                ).split(),
            )
            result["data_file_logical_bytes"] = size
            result["data_file_allocated_bytes"] = blocks * blocksize
        services[name] = result
    return {
        "sample_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "scope": "Post-trial readings on one physical host. Cgroup peak includes setup, measured workload and post-trial readbacks. Current memory includes charged cache. Data usage is sampled after final readback.",
        "host": host,
        "docker": {
            "vcpus": info["NCPU"],
            "memory_bytes": info["MemTotal"],
            "architecture": info["Architecture"],
            "server_version": info["ServerVersion"],
            "kernel_version": info["KernelVersion"],
        },
        "services": services,
    }


if __name__ == "__main__":
    print(json.dumps(snapshot(), indent=2))
