#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
out=${1:?supply a receipt directory}
mkdir -p "$out"
recipe=$(mktemp)
trap 'rm -f "$recipe"' EXIT
cat Dockerfile > "$recipe"
cat >> "$recipe" <<'DOCKER'

FROM build AS measured
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/usr/local/cargo/git \
    --mount=type=cache,target=/build/target \
    cargo test --release --locked measure_signed_body_commit -- --ignored --nocapture > /measure.log \
    && python3 -c 'import json; rows=[json.loads(l[l.index("{"):]) for l in open("/measure.log") if "{\"checkpoint_bytes\"" in l]; assert len(rows)==5; json.dump(rows,open("/throughput.json","w"),indent=2)'
FROM scratch AS workload-receipt
COPY --from=measured /throughput.json /
COPY --from=measured /build/.native/provenance.json /
DOCKER
docker build --target workload-receipt -f "$recipe" --output "type=local,dest=$out" .
