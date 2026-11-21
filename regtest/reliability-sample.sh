#!/bin/sh
set -eu
cd "$(dirname "$0")"
if [ "$#" -ne 1 ]; then
    echo 'Usage: ./regtest/reliability-sample.sh NEW_OUTPUT_DIRECTORY' >&2
    exit 2
fi
mkdir "$1"
out=$(cd "$1" && pwd)
for seed in 2701 2702 2703 2704 2705 2706 2707 2708; do
    ./races.sh --seed "$seed" --trials 128 > "$out/$seed.json" 2> "$out/$seed.log"
    python3 reliability_status.py "$out/$seed.json" > "$out/$seed-settlement.json"
    ./races.sh --down >> "$out/$seed.log" 2>&1
done
