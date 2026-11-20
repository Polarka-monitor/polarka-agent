#!/bin/bash
# Builds the wheel for one Linux platform inside an official manylinux image, the proof-of-work library inside it.
#   docker run --rm [--platform linux/amd64] -v SRC:/src:ro -v RANDOMX:/randomx:ro -v OUT:/out \
#       quay.io/pypa/manylinux_2_28_x86_64 bash /src/tools/wheels/build-linux.sh
# SRC is this repository, RANDOMX is src/randomx of the TrueNorth sources (v30.5.4), OUT gets the wheel and a log.
# The library links libstdc++ and libgcc statically, so it needs nothing but glibc 2.28; auditwheel checks that.
set -euo pipefail
ARCH=$(uname -m)
PLATFORM=manylinux_2_28_$ARCH
PY=/opt/python/cp312-cp312/bin/python
WORK=$(mktemp -d)
cp -a /src/. "$WORK/src"
rm -rf "$WORK/src/polarka_agent/lib" "$WORK/src/dist"
$PY -m pip install -q cmake==3.31.6 build==1.2.2.post1
CMAKE=$($PY -c 'import cmake, os; print(os.path.join(cmake.CMAKE_BIN_DIR, "cmake"))')
"$CMAKE" -S "$WORK/src/polarka_agent/native" -B "$WORK/rx" -DRANDOMX_DIR=/randomx -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_SHARED_LINKER_FLAGS="-static-libstdc++ -static-libgcc" > /out/build-$ARCH.log 2>&1
"$CMAKE" --build "$WORK/rx" --target rxnorth -j "$(nproc)" >> /out/build-$ARCH.log 2>&1
mkdir -p "$WORK/src/polarka_agent/lib"
cp "$WORK/rx/librxnorth.so" "$WORK/src/polarka_agent/lib/"
cp /randomx/LICENSE "$WORK/src/polarka_agent/lib/LICENSE.RandomX"
strip --strip-unneeded "$WORK/src/polarka_agent/lib/librxnorth.so"
(cd "$WORK/src" && POLARKA_WHEEL_PLATFORM=$PLATFORM $PY -m build --wheel --outdir "$WORK/dist" >> /out/build-$ARCH.log 2>&1)
WHEEL=$(ls "$WORK"/dist/*.whl)
auditwheel show "$WHEEL" | tee -a /out/build-$ARCH.log
# the policy auditwheel names must be manylinux_2_28 or lower, or the wheel claims more than it is
auditwheel show "$WHEEL" | grep -q -E 'manylinux_2_(1[0-9]|2[0-8])_' || { echo "the library needs more than glibc 2.28" >&2; exit 1; }
cp "$WHEEL" /out/
echo "built $(basename "$WHEEL")"
