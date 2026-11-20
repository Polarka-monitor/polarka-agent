#!/bin/bash
# Builds the wheel for macOS on Apple silicon, the proof-of-work library inside it, for macOS 11 and later.
#   tools/wheels/build-macos.sh RANDOMX OUT
# RANDOMX is src/randomx of the TrueNorth sources (v30.5.4); OUT gets the wheel and a log. Needs python3 and the
# network for pip (cmake and build come from PyPI into a throwaway environment).
set -euo pipefail
RANDOMX=$(cd "$1" && pwd)
OUT=$(mkdir -p "$2" && cd "$2" && pwd)
[ "$(uname -m)" = arm64 ] || { echo "this builds for arm64 only" >&2; exit 1; }
SRC=$(cd "$(dirname "$0")/../.." && pwd)
WORK=$(mktemp -d)
export MACOSX_DEPLOYMENT_TARGET=11.0
python3 -m venv "$WORK/venv"
"$WORK/venv/bin/pip" install -q cmake==3.31.6 build==1.2.2.post1
git -C "$SRC" archive HEAD | tar -x -C "$WORK" --one-top-level=src 2>/dev/null || { mkdir -p "$WORK/src"; git -C "$SRC" archive HEAD | tar -x -C "$WORK/src"; }
CMAKE="$WORK/venv/bin/cmake"
"$CMAKE" -S "$WORK/src/polarka_agent/native" -B "$WORK/rx" -DRANDOMX_DIR="$RANDOMX" -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_OSX_ARCHITECTURES=arm64 -DCMAKE_OSX_DEPLOYMENT_TARGET=11.0 > "$OUT/build-macos.log" 2>&1
"$CMAKE" --build "$WORK/rx" --target rxnorth -j 8 >> "$OUT/build-macos.log" 2>&1
mkdir -p "$WORK/src/polarka_agent/lib"
cp "$WORK/rx/librxnorth.dylib" "$WORK/src/polarka_agent/lib/"
cp "$RANDOMX/LICENSE" "$WORK/src/polarka_agent/lib/LICENSE.RandomX"
LIB="$WORK/src/polarka_agent/lib/librxnorth.dylib"
otool -l "$LIB" | grep -A3 LC_BUILD_VERSION | tee -a "$OUT/build-macos.log"
# nothing but the system's own libraries may be linked
if otool -L "$LIB" | tail -n +2 | grep -v -E '^\s+(/usr/lib/|/System/Library/|@rpath/librxnorth)'; then
  echo "the library links something that is not part of macOS" >&2; exit 1
fi
(cd "$WORK/src" && POLARKA_WHEEL_PLATFORM=macosx_11_0_arm64 "$WORK/venv/bin/python" -m build --wheel --outdir "$WORK/dist" >> "$OUT/build-macos.log" 2>&1)
cp "$WORK"/dist/*.whl "$OUT/"
echo "built $(basename "$WORK"/dist/*.whl)"
