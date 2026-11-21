#!/bin/sh
set -eu
cd "$(dirname "$0")"
docker compose -f compose.yaml up -d --build
python3 prove.py --ready
