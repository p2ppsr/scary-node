#!/usr/bin/env bash
# Build both native implementations used for Go qualification without replacing the upstream release archive.
set -euo pipefail
cd "$(dirname "$0")/.."
stage=$(cd .native && pwd -P)
pins="$stage/provenance.json"
bdk=8a4e1d3ddb16cbb2b3a98f82ea1ee6ff19a297e9
sv=6504a3aff65ba97c0f6c80962b033e35ecbfed4b
test -f "$pins" || { echo 'Run scripts/build-bdk.sh first.' >&2; exit 1; }
test "$(git -C "$stage/bdk" rev-parse HEAD)" = "$bdk"
test "$(git -C "$stage/bitcoin-sv" rev-parse HEAD)" = "$sv"
git -C "$stage/bdk" diff --quiet HEAD --
git -C "$stage/bitcoin-sv" diff --quiet HEAD --
case "$(uname -s)-$(uname -m)" in
    Darwin-arm64) platform=darwin_arm64 ;;
    Linux-aarch64) platform=linux_aarch64 ;;
    Linux-x86_64) platform=linux_x86_64 ;;
    *) echo 'Unsupported upstream build platform' >&2; exit 1 ;;
esac
root="$stage/go-oracles"
cm="$root/cmake"
bundle="$stage/dependancies_$platform"
archive="libGoBDK_$platform.a"
mkdir -p "$root"
export SOURCE_DATE_EPOCH
SOURCE_DATE_EPOCH=$(git -C "$stage/bdk" show -s --format=%ct HEAD)
cmake -S "$stage/bdk" -B "$cm" -DCMAKE_BUILD_TYPE=Release \
    -DBSV_ROOT="$stage/bitcoin-sv" -DBOOST_ROOT="$bundle/boost_1.85.0" \
    -DOPENSSL_ROOT_DIR="$bundle/openssl_3.4.0" -DBDK_LOG_BSV_FILES=OFF -DBDK_BUILD_CORE_TESTS=OFF \
    -DBUILD_MODULE_RUST=OFF -DBUILD_MODULE_GOLANG=ON -DBUILD_MODULE_GOLANG_INSTALL_INSOURCE=OFF \
    -DCUSTOM_GOBDK_OS_ARCH="$platform"
cmake --build "$cm" --target MergeGoBDK --parallel "${BDK_BUILD_JOBS:-4}"
build=$(mktemp -d "$root/go-build.XXXXXXXX")
trap 'rm -rf "$build"' EXIT
git -C "$stage/bdk" archive HEAD module/gobdk | tar -x -C "$build"
mkdir -p "$build/driver/mainnet"
cp qualification/go/main.go qualification/go/go.mod "$build/driver/"
cp qualification/go/mainnet/main.go "$build/driver/mainnet/"
(
    cd "$build/driver"
    go mod edit -replace="github.com/bitcoin-sv/bdk/module/gobdk=../module/gobdk"
    for role in distributed source; do
        if [[ "$role" = source ]]; then
            cp "$cm/x64/release/$archive" "$build/module/gobdk/bdkcgo/$archive"
        fi
        # cgo's package cache does not key an external static library's contents.
        # Distinct module roots make both independent link operations explicit.
        role_dir="$build/$role"
        mkdir "$role_dir"
        cp -R "$build/module/gobdk" "$role_dir/gobdk"
        go mod edit -replace="github.com/bitcoin-sv/bdk/module/gobdk=../$role/gobdk"
        for tool in corpus mainnet; do
            package=.
            if [[ "$tool" = mainnet ]]; then package=./mainnet; fi
            CGO_CPPFLAGS="-DSCARY_GO_${role}=1" go build -trimpath -buildvcs=false -ldflags=-buildid= \
                -o "$root/$role-$tool.next" "$package"
            mv "$root/$role-$tool.next" "$root/$role-$tool"
        done
    done
)
python3 - "$root" "$stage" "$platform" "$bdk" "$sv" <<'PY'
import hashlib,json,pathlib,subprocess,sys
out,stage=map(pathlib.Path,sys.argv[1:3]);platform,bdk,sv=sys.argv[3:]
def need(ok, message):
    if not ok: raise SystemExit(message)
def sha(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
rust=json.loads((stage/'provenance.json').read_text())
need((rust['bdk_commit'],rust['sv_node_commit'])==(bdk,sv),'Rust manifest source pins differ')
deps=sha(stage/f'dependancies_{platform}.tar.gz')
need(deps==rust['dependencies_sha256'],'dependencies differ from pinned native build')
root=stage/'bdk'; cm=out/'cmake'
cache=dict(line.split('=',1) for line in (cm/'CMakeCache.txt').read_text().splitlines() if '=' in line and not line.startswith(('#','//')))
manifest={'bdk_commit':bdk,'sv_node_commit':sv,'source_dependency_bundle_sha256':deps,'platform':platform,
    'go':subprocess.check_output(['go','version',out/'source-corpus'],text=True).split()[-1],
    'go_launcher':subprocess.check_output(['go','version'],text=True).strip(),'source_native_compiler':subprocess.check_output([cache['CMAKE_CXX_COMPILER:FILEPATH'],'--version'],text=True).splitlines()[0],
    'source_native_cmake':subprocess.check_output(['cmake','--version'],text=True).splitlines()[0],'source_native_build_type':cache['CMAKE_BUILD_TYPE:STRING'],
    'source_native_cxx_flags':cache['CMAKE_CXX_FLAGS_RELEASE:STRING'],'variants':{}}
name=f'libGoBDK_{platform}.a'
for role, native in [('distributed',root/'module/gobdk/bdkcgo'/name),('source',cm/'x64/release'/name)]:
    identity=json.loads(subprocess.check_output([out/f'{role}-mainnet'],input='',text=True))
    for tool in ('mainnet','corpus'):
        need(subprocess.check_output(['go','version',out/f'{role}-{tool}'],text=True).split()[-1]==manifest['go'],'Go compiler differs across oracles')
    expected=bdk if role=='source' else '9d34cf20a727185286a686a7800f964c34a55525'
    for key,commit in [('source',expected),('sv_source',sv)]:
        v=identity[key];need(len(v)>=7 and commit.startswith(v),f'{role} binary reports unexpected {key} {v}')
    need((identity['bdk_version'],identity['sv_version'],identity['network'])==('1.4.0','1.2.3','main'),'native versions or network differ')
    manifest['variants'][role]={'linked':identity,'native_archive_sha256':sha(native),
        'corpus_oracle_sha256':sha(out/f'{role}-corpus'),'mainnet_oracle_sha256':sha(out/f'{role}-mainnet')}
tmp=out/'manifest.next';tmp.write_text(json.dumps(manifest,indent=2)+'\n');tmp.replace(out/'manifest.json')
print(json.dumps(manifest,indent=2))
PY
