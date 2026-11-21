#!/bin/sh
set -eu
cd "$(dirname "$0")"
export COMPOSE_PROJECT_NAME=scary-node-races SCARY_LAB_PORT_SHIFT=200 SCARY_LAB_RACES=1
compose() { docker compose -f compose.yaml -f races.compose.yaml "$@"; }
case "${1:-}" in
    --down) compose down -v; exit ;;
    --help) exec python3 races.py --help ;;
esac
if [ -n "$(compose ps -aq)" ] || [ -n "$(docker volume ls -q --filter label=com.docker.compose.project=scary-node-races)" ]; then
    echo 'A race session already exists. Inspect its evidence and use ./regtest/races.sh --down before starting a fresh session.' >&2
    exit 1
fi
compose build scary-node >&2
compose up -d --no-build tn1 tn2 judge ingress >&2
exec python3 races.py "$@"
