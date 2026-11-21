#!/bin/sh
set -eu
cd "$(dirname "$0")"
export COMPOSE_PROJECT_NAME=scary-node-scenario SCARY_LAB_SCENARIO=1 SCARY_LAB_PORT_SHIFT=100
compose() { docker compose -f compose.yaml -f scenario.compose.yaml "$@"; }
if [ "${1:-}" = --down ]; then
    compose down -v
    exit
fi
if [ "${1:-}" = --help ]; then
    exec python3 scenario.py --help
fi
if [ -n "$(compose ps -aq)" ] || [ -n "$(docker volume ls -q --filter label=com.docker.compose.project=scary-node-scenario)" ]; then
    echo 'An existing scenario lab is present. Use ./regtest/scenario.sh --down before a fresh run.' >&2
    exit 1
fi
compose build scary-node >&2
compose up -d --no-build tn1 tn2 ingress >&2
exec python3 scenario.py "$@"
