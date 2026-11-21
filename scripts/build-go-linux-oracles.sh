#!/usr/bin/env bash
# Reuse the release's BDK build stage, keeping both additional CMake and Go builds in memory.
set -euo pipefail
cd "$(dirname "$0")/.."
output=${1:?supply an output directory for the four Linux ARM64 oracles and manifest}
mkdir -p "$output"
recipe=$(mktemp)
trap 'rm -f "$recipe"' EXIT
awk '{print} $0 == "RUN scripts/build-bdk.sh" {found=1;exit} END {if(!found) exit 1}' Dockerfile > "$recipe"
cat >> "$recipe" <<'DOCKER'
FROM golang:1.27-bookworm@sha256:648f440f42a0958804efb24df176f806f9d353b41f1c0627f666428e40310f6b AS go_tools
FROM build AS linux_go_oracles
RUN --network=none \
    --mount=type=bind,from=go_tools,source=/usr/local/go,target=/opt/go,ro \
    --mount=type=bind,source=scripts/build-go-oracles.sh,target=/build/scripts/build-go-oracles.sh,ro \
    --mount=type=bind,source=qualification/go,target=/build/qualification/go,ro \
    --mount=type=tmpfs,target=/build/.native/go-oracles,size=2147483648 \
    --mount=type=tmpfs,target=/work,size=2147483648 \
    export PATH=/opt/go/bin:$PATH GOROOT=/opt/go GOPATH=/work/gopath GOCACHE=/work/gocache \
           GOTMPDIR=/work/tmp GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off GOMAXPROCS=2 BDK_BUILD_JOBS=2; \
    mkdir -p /work/tmp && scripts/build-go-oracles.sh && mkdir /out && \
    cp .native/go-oracles/manifest.json .native/go-oracles/distributed-* .native/go-oracles/source-* /out/
FROM scratch
COPY --from=linux_go_oracles /out/ /
DOCKER
docker buildx build --platform linux/arm64 --file "$recipe" --output "type=local,dest=$output" .
