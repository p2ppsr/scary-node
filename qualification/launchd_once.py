"""Run one finite lab driver under the logged-in macOS user's service manager."""

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
import uuid
from pathlib import Path


def submit(directory, command):
    executable = shutil.which(command[0])
    if executable is None:
        raise ValueError(f"cannot resolve executable {command[0]}")
    label = "org.p2ppsr.scary-node.lab." + uuid.uuid4().hex
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    config = {
        "Label": label,
        "WorkingDirectory": str(Path.cwd()),
        "ProgramArguments": [os.path.abspath(executable), *command[1:]],
        "EnvironmentVariables": {"PATH": os.environ["PATH"]},
        "StandardOutPath": str(directory / "stdout.log"),
        "StandardErrorPath": str(directory / "stderr.log"),
        "RunAtLoad": True,
        "KeepAlive": False,
    }
    service = directory / "driver.plist"
    service.write_bytes(plistlib.dumps(config))
    domain = f"gui/{os.getuid()}"
    target = domain + "/" + label
    (directory / "service-target").write_text(target + "\n")
    subprocess.run(["launchctl", "bootstrap", domain, service], check=True)
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "directory", type=Path, help="fresh directory for service metadata and logs"
    )
    parser.add_argument(
        "command", nargs=argparse.REMAINDER, help="driver followed by its arguments"
    )
    args = parser.parse_args()
    program = args.command
    if program[:1] == ["--"]:
        program = program[1:]
    if sys.platform != "darwin" or not program:
        parser.error("supply a lab command on macOS")
    print(submit(args.directory, program))
