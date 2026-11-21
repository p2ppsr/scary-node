#!/bin/sh
set -eu
cd "$(dirname "$0")"
export COMPOSE_PROJECT_NAME=scary-node-reliability SCARY_LAB_RELIABILITY=1 SCARY_LAB_PORT_SHIFT=300
compose() { docker compose -f compose.yaml -f reliability.compose.yaml "$@"; }
if [ "${1:-}" = --down ]; then
    compose down -v
    exit
fi
if [ -n "$(compose ps -aq)" ] || [ -n "$(docker volume ls -q --filter label=com.docker.compose.project=scary-node-reliability)" ]; then
    echo 'The previous reliability session exists. Inspect it and tear it down before running a fresh proof.' >&2
    exit 1
fi
compose up -d --build >&2
exec python3 reliability_proof.py --reorg
