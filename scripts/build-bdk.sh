#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p .native
stage=$(cd .native && pwd -P)
bdk=8a4e1d3ddb16cbb2b3a98f82ea1ee6ff19a297e9
sv=6504a3aff65ba97c0f6c80962b033e35ecbfed4b
checkout() {
    local name=$1 pin=$2
    if [[ ! -d "$stage/$name/.git" ]]; then
        git init -q "$stage/$name"
        git -C "$stage/$name" fetch -q --depth=1 "https://github.com/bitcoin-sv/$name" "$pin"
        git -C "$stage/$name" checkout -q --detach "$pin"
    fi
    test "$(git -C "$stage/$name" rev-parse HEAD)" = "$pin"
    git -C "$stage/$name" diff --quiet HEAD --
}
checkout bdk "$bdk"
checkout bitcoin-sv "$sv"
case "$(uname -s)-$(uname -m)" in
    Darwin-arm64) platform=darwin_arm64; digest=50696015ff6b77d452bbd5098969e80ed562fe82eb5f8826651f3075d3538ce8 ;;
    Linux-aarch64) platform=linux_aarch64; digest=ba52bbaea6b8c31f44679dad53ca57d3021654779d9e4d9d71da25c3dece8578 ;;
    Linux-x86_64) platform=linux_x86_64; digest=79faefafcbe56f9ef129856dd3cbc65a842db7e7b30f9e1d51f1d0c6607f82c7 ;;
    *) echo "Unsupported upstream build platform" >&2; exit 1 ;;
esac
bundle=dependancies_$platform
if [[ ! -f "$stage/$bundle.tar.gz" ]]; then
    curl --fail --silent --show-error --location --retry 3 "https://github.com/bitcoin-sv/bdk/releases/download/depcy/$bundle.tar.gz" -o "$stage/$bundle.part"
    mv "$stage/$bundle.part" "$stage/$bundle.tar.gz"
fi
(cd "$stage" && echo "$digest  $bundle.tar.gz" | shasum -a 256 -c - && tar -xzf "$bundle.tar.gz")
export SOURCE_DATE_EPOCH
SOURCE_DATE_EPOCH=$(git -C "$stage/bdk" show -s --format=%ct HEAD)
cmake -S "$stage/bdk" -B "$stage/release" -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_PROJECT_INCLUDE="$PWD/native/bdk.cmake" -DSCARY_NATIVE_DIR="$PWD/native" \
    -DBSV_ROOT="$stage/bitcoin-sv" -DBOOST_ROOT="$stage/$bundle/boost_1.85.0" \
    -DOPENSSL_ROOT_DIR="$stage/$bundle/openssl_3.4.0" \
    -DBDK_LOG_BSV_FILES=OFF -DBDK_BUILD_CORE_TESTS=OFF \
    -DBUILD_MODULE_GOLANG=OFF -DBUILD_MODULE_RUST=ON -DBUILD_MODULE_RUST_INSTALL_INSOURCE=ON
cmake --build "$stage/release" --target MergeBDKFFI --parallel "${BDK_BUILD_JOBS:-4}"
archive="$stage/bdk/module/rustbdk/bdk-sys/lib/libbdkffi_$platform.a"
python3 - "$stage" "$archive" "$platform" "$digest" "$bdk" "$sv" <<'PY'
import hashlib,json,pathlib,subprocess,sys
stage,archive,platform,digest,bdk,sv=sys.argv[1:]
cache=dict(line.rstrip().split("=",1) for line in (pathlib.Path(stage)/"release/CMakeCache.txt").read_text().splitlines() if "=" in line and not line.startswith(("#","//")))
compiler=cache["CMAKE_CXX_COMPILER:FILEPATH"]
receipt={"bdk_commit":bdk,"sv_node_commit":sv,"platform":platform,"dependencies_sha256":digest,
         "native_archive_sha256":hashlib.sha256(pathlib.Path(archive).read_bytes()).hexdigest(),
         "type":"Release","cxx_flags":cache["CMAKE_CXX_FLAGS_RELEASE:STRING"],"cxx_standard":20,
         "compiler":subprocess.check_output([compiler,"--version"],text=True).splitlines()[0],
         "cmake":subprocess.check_output(["cmake","--version"],text=True).splitlines()[0]}
(pathlib.Path(stage)/"provenance.json").write_text(json.dumps(receipt,indent=2)+"\n")
PY
