"""Build the isolated native synchronization harness using the release's unmodified receiver functions."""

import json, subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
src = (ROOT / ".native/alert").resolve()
pin = "c19f9959fb979f9def944807bd4e58ecadf25717"
if not src.exists():
    subprocess.run(
        ["git", "clone", "https://github.com/bsv-blockchain/go-alert-system", str(src)],
        check=True,
    )
    subprocess.run(["git", "checkout", "--detach", pin], cwd=src, check=True)
if (
    subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=src, text=True).strip()
    != pin
):
    raise RuntimeError("upstream receiver pin differs")
here = Path(__file__).resolve().parent
overlay = ROOT / ".native/receiver-overlay.json"
overlay.write_text(
    json.dumps(
        {
            "Replace": {
                str(src / "cmd/go-alert-system/main.go"): str(here / "main.go.in"),
                str(src / "app/p2p/qualification_sync.go"): str(
                    here / "loopback.go.in"
                ),
            }
        }
    )
)
subprocess.run(
    [
        "go",
        "build",
        "-mod=readonly",
        "-overlay",
        str(overlay),
        "-o",
        str(ROOT / ".native/receiver-lab"),
        "./cmd/go-alert-system",
    ],
    cwd=src,
    check=True,
)
