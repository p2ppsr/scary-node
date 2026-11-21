#!/bin/sh
set -eu
cd "$(dirname "$0")"
export SCARY_LAB_MIXED=1 COMPOSE_PROJECT_NAME=scary-node-mixed
compose() { docker compose -f compose.yaml -f mixed.compose.yaml "$@"; }
if [ "${1:-}" = --down ]; then compose down -v; exit; fi
if [ "${1:-}" = --workload ]; then
    case "${2:-}" in 128|1024|4096) export SCARY_MIXED_WORKLOAD=$2;; *) echo 'Select 128, 1024 or 4096 signed payments.' >&2; exit 2;; esac
fi
if [ -n "$(compose ps -aq)" ] || [ -n "$(docker volume ls -q --filter label=com.docker.compose.project=scary-node-mixed)" ]; then
    echo 'This mixed run requires fresh reference volumes.' >&2
    exit 1
fi
compose up -d --build >&2
exec python3 prove.py
