# syntax=docker/dockerfile:1
FROM rust:1.98.1-slim-bookworm@sha256:ff521445a372125ed4f76e1453a1f8098f2d05332d1601d30db1c1f62757e730 AS build
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends \
    autoconf automake build-essential ca-certificates cmake curl git libtool perl pkg-config python3 \
    && rm -rf /var/lib/apt/lists/*
COPY scripts/build-bdk.sh scripts/build-bdk.sh
COPY native native
RUN scripts/build-bdk.sh
COPY Cargo.toml Cargo.lock ./
COPY src ./src
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/usr/local/cargo/git \
    --mount=type=cache,target=/build/target \
    cargo build --locked --release && install -m 755 target/release/scary-node /usr/local/bin/scary-node

FROM debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc
COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=build /usr/local/bin/scary-node /usr/local/bin/scary-node
WORKDIR /var/lib/scary-node
STOPSIGNAL SIGINT
ENTRYPOINT ["scary-node"]
